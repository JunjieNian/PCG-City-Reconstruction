# PCG City Reconstruction

This project reconstructs a real urban area around Jiangwan Stadium from OpenStreetMap data in Unreal Engine 5.7. The goal is to translate OSM building and road geometry into UE-friendly data tables, then generate a procedural city scene in `CityLevel`.

The final version uses a PCG Graph as the main generation controller. The graph loads translated DataTables and spawns procedural Blueprint actors for buildings and roads. Each spawned actor then parses its geometry string and builds the corresponding mesh using Blueprint and Dynamic Mesh / Geometry Script nodes.

## Main Features

* OSM-based reconstruction of buildings and roads.
* Procedural generation through `PCG_CityGraph`.
* One generated building actor per building metadata row.
* One generated road actor per road metadata row.
* Building footprints are extruded into simple 3D blocks.
* Road centerlines are converted into flat road slabs.
* A ground block is included for the full reconstructed area.
* A demo video is provided in `demo/demo.mp4`.

## Project Structure

```text
PCGCity/
├─ Config/
│  └─ Unreal project configuration files.
│
├─ Content/
│  ├─ Maps/
│  │  └─ CityLevel.umap
│  │     Main level containing Ground, PCGVolume, and generated city output.
│  │
│  ├─ PCG/
│  │  └─ PCG_CityGraph.uasset
│  │     Final PCG graph. It loads metadata DataTables and spawns procedural actors.
│  │
│  ├─ Blueprints/
│  │  ├─ BP_PCG_Building.uasset
│  │  │  Procedural building actor spawned by PCG_CityGraph.
│  │  ├─ BP_PCG_Road.uasset
│  │  │  Procedural road actor spawned by PCG_CityGraph.
│  │  └─ BP_CityGenerator.uasset
│  │     Earlier direct-Blueprint prototype that reads normalized vertex tables.
│  │
│  ├─ Structs/
│  │  DataTable row structs for building and road metadata/vertices.
│  │
│  ├─ data/
│  │  Exported CSV files, including buildings_meta, buildings_verts,
│  │  roads_meta, and roads_verts.
│  │
│  ├─ OSMData/
│  │  Raw and intermediate OSM-related files.
│  │
│  └─ pipeline/
│     Python scripts for translating OSM data into CSV/DataTable-ready format.
│
├─ demo/
│  └─ demo.mp4
│     Fly-through and overhead demo video.
│
├─ report.md
│  Project report with implementation details and visual comparison.
│
├─ report_assets/
│  Images used in the report, including OSM and generated UE screenshots.
│
└─ PCGCity.uproject
   Unreal Engine project file.
```

## Generation Workflow

The data pipeline exports four main tables:

* `buildings_meta`: one row per building, including height and pre-grouped footprint string.
* `buildings_verts`: normalized building footprint vertices.
* `roads_meta`: one row per road, including width and pre-grouped centerline string.
* `roads_verts`: normalized road centerline vertices.

The final PCG version mainly consumes `buildings_meta` and `roads_meta`, because these tables already contain grouped geometry strings. The `*_verts` tables are kept for validation and for the earlier `BP_CityGenerator` prototype.

In Unreal:

1. `PCG_CityGraph` loads `buildings_meta` and spawns `BP_PCG_Building`.
2. Each building actor parses its `footprint` string and extrudes the polygon by `height_cm`.
3. `PCG_CityGraph` loads `roads_meta` and spawns `BP_PCG_Road`.
4. Each road actor parses its `centerline` string and creates road slabs between adjacent points.

## How to Regenerate

1. Open `PCGCity.uproject` in Unreal Engine 5.7.
2. Open `Content/Maps/CityLevel`.
3. Select `PCGVolume` in the World Outliner.
4. Confirm that its graph is set to `PCG_CityGraph`.
5. Click `Cleanup` to remove previous generated output.
6. Click `Generate` to rebuild the city.

Generated actors will appear under `PCGVolume_Generated`.

