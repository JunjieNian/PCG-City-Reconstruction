#!/usr/bin/env python3
"""
OSM -> PCG translation pipeline (Part A).

Fetches OpenStreetMap data (buildings + road network) for a chosen area via
osmnx / the Overpass API, projects lon/lat to a metric local frame centred on
the bbox, converts metres -> centimetres (Unreal Engine units), derives building
heights from OSM tags, and exports everything in formats a UE PCG graph can
consume (CSV + GeoJSON + a manifest for reproducibility).

Reproducible: re-running this script with the same --bbox / --place regenerates
all artifacts. Re-running the PCG graph on the regenerated data rebuilds the
scene with no stale actors.

Usage:
    python pipeline/osm_to_pcg.py --bbox 121.510 31.302 121.520 31.311 \
        --name wujiaochang --out data
    python pipeline/osm_to_pcg.py --place "Wujiaochang, Yangpu, Shanghai"

Coordinate convention exported to UE:
    origin   = bbox centre (lon0, lat0) -> UE world (0, 0)
    X_cm     = easting  relative to origin, in centimetres  (UE +X = East)
    Y_cm     = northing relative to origin, in centimetres  (UE +Y = North)
  UE is left-handed; flip Y on import (negate Y, or set actor scale Y = -1 on the
  georef root) so the overhead view matches a north-up map. See docs/UE_PCG_setup.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import osmnx as ox
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

M_TO_CM = 100.0
LEVEL_HEIGHT_M = 3.0  # metres per storey when only building:levels is tagged

# Plausible per-highway-type carriageway widths (metres), used when OSM has no
# explicit width/lanes. Only a hint for road slab/spline-mesh authoring in UE.
ROAD_WIDTH_M = {
    "motorway": 16.0, "trunk": 14.0, "primary": 12.0, "secondary": 10.0,
    "tertiary": 8.0, "residential": 6.0, "living_street": 5.0,
    "service": 4.0, "unclassified": 6.0, "pedestrian": 4.0, "footway": 2.0,
    "path": 2.0, "cycleway": 2.5,
}
DEFAULT_ROAD_WIDTH_M = 6.0


def log(msg: str) -> None:
    print(f"[osm_to_pcg] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Height derivation
# ---------------------------------------------------------------------------
def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        # OSM height can be "12", "12 m", "12.5", lists from multi-tag ways
        if isinstance(val, (list, tuple)):
            val = val[0]
        s = str(val).strip().lower().replace("m", "").replace("'", "").strip()
        f = float(s)
        return None if math.isnan(f) else f  # treat OSM NaN/empty as missing
    except (ValueError, TypeError):
        return None


def _clean_str(val) -> str:
    """OSM string tag -> str, mapping NaN/None/'nan' to empty (NaN is truthy)."""
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    s = str(val).strip()
    return "" if s.lower() == "nan" else s


def derive_height_m(row, default_height_m: float) -> tuple[float, str]:
    """Return (height_m, source) using OSM height -> levels*3 -> default."""
    h = _to_float(row.get("height"))
    if h and h > 0:
        return h, "osm:height"
    levels = _to_float(row.get("building:levels"))
    if levels and levels > 0:
        return levels * LEVEL_HEIGHT_M, "osm:levels"
    return default_height_m, "estimate:default"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_osm(bbox: tuple[float, float, float, float]):
    """bbox = (west, south, east, north). Returns (buildings_gdf, roads_gdf)."""
    west, south, east, north = bbox
    log(f"fetching buildings for bbox W{west} S{south} E{east} N{north} ...")
    buildings = ox.features.features_from_bbox(bbox, tags={"building": True})
    buildings = buildings[buildings.geometry.type.isin(["Polygon", "MultiPolygon"])]
    log(f"  {len(buildings)} building features")

    log("fetching drivable+walkable road network ...")
    graph = ox.graph.graph_from_bbox(bbox, network_type="all", simplify=True)
    # osmnx returns a directed graph: two-way streets appear twice (u->v and
    # v->u, identical geometry reversed). Collapse to undirected so each real
    # centerline is exported once — no missing roads, no doubled/overlapping
    # splines in UE. (Intersections still split ways into multiple edges.)
    graph = ox.convert.to_undirected(graph)
    roads = ox.convert.graph_to_gdfs(graph, nodes=False)
    log(f"  {len(roads)} unique road edges (deduplicated from directed graph)")
    return buildings, roads


# ---------------------------------------------------------------------------
# Projection: lon/lat -> local metric frame (cm), origin at bbox centre
# ---------------------------------------------------------------------------
def make_projector(lon0: float, lat0: float):
    """Tangent-plane (azimuthal equidistant) projection centred on origin.

    Accurate to <0.1% over a few km — ample for a district-scale scene, and
    fully self-contained (no UTM zone bookkeeping). Returns a fn lon,lat->(x_cm,y_cm).
    """
    aeqd = f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs"
    tf = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)

    def project(lon: float, lat: float) -> tuple[float, float]:
        x_m, y_m = tf.transform(lon, lat)  # x=easting, y=northing in metres
        return x_m * M_TO_CM, y_m * M_TO_CM

    return project


def project_ring(coords, project) -> list[tuple[float, float]]:
    return [tuple(round(v, 2) for v in project(lon, lat)) for lon, lat in coords]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_buildings(buildings, project, default_height_m: float, out: Path):
    """Write buildings_meta.csv, buildings_verts.csv, buildings.geojson."""
    meta_rows, vert_rows, features = [], [], []
    bid = 0
    for _, row in buildings.iterrows():
        geom: BaseGeometry = row.geometry
        polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        height_m, src = derive_height_m(row, default_height_m)
        for poly in polys:
            if not isinstance(poly, Polygon) or poly.is_empty:
                continue
            ring = list(poly.exterior.coords)
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring = ring[:-1]  # drop closing dup; PCG closes the loop itself
            if len(ring) < 3:
                continue
            xy = project_ring(ring, project)
            cx = round(sum(p[0] for p in xy) / len(xy), 2)
            cy = round(sum(p[1] for p in xy) / len(xy), 2)
            # packed footprint "x0 y0;x1 y1;..." — lets a Blueprint read ONE table
            # and Parse-Into-Array straight into the extrude node (no vert grouping)
            footprint = ";".join(f"{x} {y}" for x, y in xy)
            meta_rows.append({
                "building_id": bid,
                "height_m": round(height_m, 2),
                "height_cm": round(height_m * M_TO_CM, 1),
                "height_source": src,
                "num_verts": len(xy),
                "centroid_x_cm": cx,
                "centroid_y_cm": cy,
                "osm_name": _clean_str(row.get("name")),
                "footprint": footprint,
            })
            for i, (x, y) in enumerate(xy):
                vert_rows.append({"building_id": bid, "vert_index": i,
                                  "x_cm": x, "y_cm": y})
            features.append({
                "type": "Feature",
                "properties": {"building_id": bid, "height_m": round(height_m, 2),
                               "height_source": src},
                "geometry": {"type": "Polygon",
                             "coordinates": [[[x, y] for x, y in xy] +
                                             [[xy[0][0], xy[0][1]]]]},
            })
            bid += 1

    _write_csv(out / "buildings_meta.csv", meta_rows,
               ["building_id", "height_m", "height_cm", "height_source",
                "num_verts", "centroid_x_cm", "centroid_y_cm", "osm_name",
                "footprint"])
    _write_csv(out / "buildings_verts.csv", vert_rows,
               ["building_id", "vert_index", "x_cm", "y_cm"])
    _write_geojson(out / "buildings.geojson", features)
    log(f"exported {len(meta_rows)} buildings -> buildings_meta.csv / _verts.csv / .geojson")
    return meta_rows


def export_roads(roads, project, out: Path):
    """Write roads_meta.csv, roads_verts.csv, roads.geojson."""
    meta_rows, vert_rows, features = [], [], []
    rid = 0
    for _, row in roads.iterrows():
        geom = row.geometry
        if not isinstance(geom, LineString) or geom.is_empty:
            continue
        hwy = row.get("highway")
        if isinstance(hwy, (list, tuple)):
            hwy = hwy[0]
        hwy = str(hwy) if hwy is not None else "unclassified"
        lanes = _to_float(row.get("lanes"))
        width = _to_float(row.get("width"))
        if width is None:
            width = (lanes * 3.5) if lanes else ROAD_WIDTH_M.get(hwy, DEFAULT_ROAD_WIDTH_M)
        xy = project_ring(list(geom.coords), project)
        if len(xy) < 2:
            continue
        centerline = ";".join(f"{x} {y}" for x, y in xy)
        meta_rows.append({
            "road_id": rid, "highway": hwy, "width_m": round(width, 2),
            "width_cm": round(width * M_TO_CM, 1), "num_verts": len(xy),
            "name": _clean_str(row.get("name")), "centerline": centerline,
        })
        for i, (x, y) in enumerate(xy):
            vert_rows.append({"road_id": rid, "vert_index": i, "x_cm": x, "y_cm": y})
        features.append({
            "type": "Feature",
            "properties": {"road_id": rid, "highway": hwy, "width_m": round(width, 2)},
            "geometry": {"type": "LineString", "coordinates": [[x, y] for x, y in xy]},
        })
        rid += 1

    _write_csv(out / "roads_meta.csv", meta_rows,
               ["road_id", "highway", "width_m", "width_cm", "num_verts", "name",
                "centerline"])
    _write_csv(out / "roads_verts.csv", vert_rows,
               ["road_id", "vert_index", "x_cm", "y_cm"])
    _write_geojson(out / "roads.geojson", features)
    log(f"exported {len(meta_rows)} roads -> roads_meta.csv / _verts.csv / .geojson")
    return meta_rows


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    # UE DataTable CSV import treats the first column as the (unique) RowName.
    # Prepend an always-unique "Name" key; the semantic id columns (building_id,
    # road_id, …) stay as real struct fields for lookups inside the graph.
    fields = ["Name"] + fields
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, r in enumerate(rows):
            w.writerow({"Name": f"{path.stem}_{i}", **r})


def _write_geojson(path: Path, features: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def resolve_bbox(args) -> tuple[float, float, float, float]:
    if args.bbox:
        west, south, east, north = args.bbox
        return (west, south, east, north)
    if args.place:
        log(f"geocoding place: {args.place!r}")
        gdf = ox.geocoder.geocode_to_gdf(args.place)
        miny, maxy = gdf.total_bounds[1], gdf.total_bounds[3]
        minx, maxx = gdf.total_bounds[0], gdf.total_bounds[2]
        return (minx, miny, maxx, maxy)
    raise SystemExit("Provide --bbox W S E N or --place NAME")


def main() -> int:
    p = argparse.ArgumentParser(description="OSM -> PCG translation pipeline")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                   help="bounding box: west south east north (lon/lat degrees)")
    p.add_argument("--place", type=str, help="place name (geocoded via Nominatim)")
    p.add_argument("--name", type=str, default="area", help="area label for manifest")
    p.add_argument("--out", type=str, default="data", help="output directory")
    p.add_argument("--default-height", type=float, default=12.0,
                   help="fallback building height in metres (no tag)")
    args = p.parse_args()

    bbox = resolve_bbox(args)
    west, south, east, north = bbox
    lon0 = (west + east) / 2.0
    lat0 = (south + north) / 2.0
    project = make_projector(lon0, lat0)

    out = Path(args.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)

    buildings, roads = fetch_osm(bbox)

    # save raw parsed geodata for provenance / re-inspection
    try:
        buildings.to_file(out / "raw" / "buildings_raw.geojson", driver="GeoJSON")
        roads.to_file(out / "raw" / "roads_raw.geojson", driver="GeoJSON")
    except Exception as e:  # geometry columns with list values can trip the writer
        log(f"  (raw geojson dump skipped: {type(e).__name__})")

    b_rows = export_buildings(buildings, project, args.default_height, out)
    r_rows = export_roads(roads, project, out)

    # scene extent in cm, for camera framing / sanity
    xs = [r["centroid_x_cm"] for r in b_rows] or [0]
    ys = [r["centroid_y_cm"] for r in b_rows] or [0]
    corners = [project(west, south), project(east, north)]
    manifest = {
        "name": args.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "data_source": "OpenStreetMap via Overpass API (osmnx)",
        "osm_query": {"bbox_west_south_east_north": [west, south, east, north],
                      "place": args.place},
        "projection": {
            "method": "azimuthal equidistant (aeqd), tangent plane at origin",
            "datum": "WGS84",
            "origin_lonlat": [lon0, lat0],
            "units": "centimetres (metres * 100) — UE units",
            "axes": "X_cm = East, Y_cm = North (flip Y on UE import for north-up)",
        },
        "height_rules": {
            "priority": ["osm:height", "osm:levels * 3.0m", f"default {args.default_height}m"],
            "level_height_m": LEVEL_HEIGHT_M,
            "default_height_m": args.default_height,
        },
        "counts": {"buildings": len(b_rows), "roads": len(r_rows)},
        "scene_extent_cm": {
            "x_min": round(corners[0][0], 1), "x_max": round(corners[1][0], 1),
            "y_min": round(corners[0][1], 1), "y_max": round(corners[1][1], 1),
        },
        "outputs": ["buildings_meta.csv", "buildings_verts.csv", "buildings.geojson",
                    "roads_meta.csv", "roads_verts.csv", "roads.geojson"],
    }
    with (out / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    log(f"wrote manifest.json — {len(b_rows)} buildings, {len(r_rows)} roads")
    log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
