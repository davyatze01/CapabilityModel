# CapabilityModel

This repository computes spatial capability scores for a study area by combining:

- POIs extracted from OpenStreetMap
- walk, bike, drive, and transit routing
- decay-based accessibility functions
- service-level aggregation
- capability-level aggregation

The current implementation is configured around **Cagliari, Sardinia, Italy** and produces node-level outputs for three capabilities:

- `restorativeness`
- `nutrition`
- `care`

## Main Workflow

The pipeline entry point is [`main.py`](main.py). Its execution flow is:

1. Build configuration and shared context.
2. Snap every POI to the relevant transport graphs.
3. Build transit routing inputs and compute bus travel times.
4. Compute walk, bike, and drive accessibility ingredients for every origin node.
5. Merge modal results into POI-level accessibility values and group them by service.
6. Aggregate POI accessibilities into service scores.
7. Aggregate service scores into capability scores and write CSV outputs.

At a high level, the data flow is:

`config/context -> snapping -> bus routing + non-bus routing -> accessibility -> services -> capabilities -> outputs/*.csv`

## Core Entry Files

### [`main.py`](main.py)

`main()` orchestrates the whole pipeline. It creates a `PipelineConfig`, builds a `PipelineContext`, runs six stages in sequence, and prints the output paths returned by the final stage.

### [`config.py`](config.py)

Defines `PipelineConfig`, which controls:

- worker count and retry behavior for multiprocessing
- debug sampling of graph nodes and POIs
- cache directories and cache schema versions
- fixed departure datetime for transit routing
- expected paths for transit input/output artifacts

Important current defaults:

- transit departure time is fixed to `2025-10-15 12:00:00`
- non-bus cache lives in `cache/non_bus`
- POI snapping cache lives in `cache/poi_snap_cache`

### [`context.py`](context.py)

Builds the shared runtime context used by every stage:

- loads the walk graph and extracts graph nodes with coordinates
- optionally subsamples nodes for debugging
- creates output directories
- defines output CSV paths for each capability
- computes worker count
- injects the service lists associated with each capability

### [`pipeline_types.py`](pipeline_types.py)

Defines the dataclasses passed between stages, such as:

- `PipelineContext`
- `SnappingStageResult`
- `BusRoutingStageResult`
- `NonBusRoutingStageResult`
- `AccessibilityStageResult`
- `ServiceStageResult`
- `CapabilityStageResult`

These types make the stage interfaces explicit.

## Detailed Pipeline

### 1. POI and Aggregation Definitions

Before any routing is done, the repo builds the semantic model of the pipeline.

#### [`config/poi_types.csv`](config/poi_types.csv)

This CSV is the main domain configuration. Each row defines:

- a `poi_type`
- a decay constant
- one or more services that the POI contributes to
- Choquet weights for those service contributions
- contribution constants used later in accessibility aggregation
- OSM tags used to fetch the POI geometry

A single POI type can contribute to more than one service.

#### [`utils/services.py`](utils/services.py)

This module parses `config/poi_types.csv` and turns it into runtime structures:

- `SERVICE_POI_QUERIES`: service -> ordered list of POI queries
- `SERVICE_SINGLETON_M`: service -> singleton Choquet capacities
- `POI_DECAY_CONSTANTS`
- `SERVICE_CONTRIBUTION_CONSTANTS`
- `SERVICE_KEYS`

It also provides:

- `unique_query_keys()` to deduplicate POI fetch/snap work across services
- `get_service_queries(service)` to enumerate the POIs behind a service
- `choquet_integral()` to aggregate POI-level accessibilities into one service score

#### [`utils/capabilities.py`](utils/capabilities.py)

Defines the next aggregation level:

- which services belong to each capability
- singleton Choquet weights for capability aggregation
- `choquet_integral()` for combining service scores into final capability scores

This is where the three top-level capabilities are formalized.

#### [`services_capabilities.txt`](services_capabilities.txt)

This file is a human-readable reference listing the conceptual mapping from:

- capability -> service
- service -> OSM tag patterns

The code uses `config/poi_types.csv` and `utils/capabilities.py` directly; this text file is documentation/supporting material.

### 2. Snapping Stage

Implemented in [`snapping_stage.py`](snapping_stage.py).

Goal: convert each POI geometry into one or more graph-aligned candidate nodes for later routing.

#### Inputs

- `PipelineContext`
- POI definitions from [`utils/services.py`](utils/services.py)
- graph and POI loaders from [`utils/graphml.py`](utils/graphml.py)
- geometry helpers and caches from [`utils/delta_g.py`](utils/delta_g.py)

#### What happens

1. `run_snapping_stage()` asks `utils.services` for all unique POI queries.
2. It loads three mode-specific graphs with `graphml.get_mode_graph()`:
   - `walk`
   - `bike`
   - `drive`
3. For each POI query, it loads the POI geometries from OSM or local GeoJSON cache.
4. If a geometry is a point, it gets one candidate coordinate.
5. If a geometry is a line, polygon, or collection, `_extract_geom_vertices()` creates multiple candidate coordinates from its vertices.
6. `_snap_coords_batch()` snaps all coordinates in bulk to the nearest graph nodes with `osmnx.distance.nearest_nodes`.
7. Results are cached under `cache/poi_snap_cache`.

#### Why there can be multiple snapped candidates

For non-point POIs, the code does not force a single snapped node immediately. Instead, it stores all candidate snapped vertices for that POI. Later, for a specific origin, the pipeline selects the candidate that is best for that origin.

This logic is implemented by:

- `_select_best_snap_candidate_for_origin()`
- `_build_selected_routing_destinations()`

The returned `SnappingStageResult` contains:

- `query_by_key`: deduplicated POI queries
- `poi_bus_snap_info_by_type`: bus snap candidates, reusing walk snaps
- `poi_mode_snap_info_by_type`: walk/bike/drive snap candidates
- `shared_mode_graphs`: already-loaded mode graphs

### 3. Bus Routing Stage

Implemented in [`bus_routing_stage.py`](bus_routing_stage.py).

Goal: compute transit travel times from every origin node to every relevant snapped POI destination.

#### Supporting files

- [`utils/r5_routing.r`](utils/r5_routing.r)
- GTFS / network files under [`gtfs`](gtfs)
- transit inputs/outputs under [`outputs`](outputs)

#### What happens in Python

1. The stage collects all snapped bus destination candidates from the snapping result.
2. If any POI has multiple snapped candidates, it reduces the destination set with `build_selected_routing_destinations()` from the snapping stage.
3. It computes signatures of the ordered origin and destination coordinate lists.
4. It writes:
   - `outputs/r5r_origins.csv`
   - `outputs/r5r_dest.csv`
5. If `skip_routing=True`, it reuses the existing expanded routing CSV and does not call R.
6. Otherwise, it launches `Rscript utils/r5_routing.r`.
7. The expanded travel-time matrix CSV is the single reusable transit artifact.

#### What happens in R

[`utils/r5_routing.r`](utils/r5_routing.r) does the transit work with `r5r`:

- loads the GTFS/network bundle from `gtfs`
- reads origin and destination CSVs
- builds the R5 network
- computes an expanded travel time matrix in chunks
- keeps the best route per origin/destination pair
- writes `outputs/r5r_expanded_travel_time_matrix.csv`

The accessibility stage reads this CSV directly (in-memory OD lookup per worker).

The stored route record includes:

- `travel_time`
- `wait_time`
- `impedance` (`total_time`)
- route description if available

### 4. Non-Bus Routing Stage

Implemented in [`non_bus_routing_stage.py`](non_bus_routing_stage.py).

Goal: for every origin node, compute the walk/bike/drive accessibility ingredients needed later for each POI type.

#### Main dependencies

- [`utils/delta_g.py`](utils/delta_g.py)
- [`utils/get_impedance.py`](utils/get_impedance.py)
- [`utils/decay.py`](utils/decay.py)
- [`utils/services.py`](utils/services.py)

#### What happens

1. For each graph node in `ctx.nodes_with_coords`, the stage checks whether `cache/non_bus/<node_id>.pkl` already exists and matches the configured schema version.
2. Nodes without a valid cache are processed in parallel with `multiprocessing.Pool`.
3. Each worker iterates through every service and every POI query belonging to that service.
4. For each `(origin, poi_type)`, it calls `delta_g.accessibility_non_bus_from_snap_map(...)`.

#### What `delta_g.accessibility_non_bus_from_snap_map()` does

Inside [`utils/delta_g.py`](utils/delta_g.py), this function:

- builds or reuses an RRA cache path in `rra_cache`
- if a full non-bus RRA cache already exists, returns the final accessibility value immediately
- otherwise:
  - gathers all snapped source coordinates for the POI across walk/bike/drive
  - selects the best snapped candidate for the current origin in each mode
  - snaps origin and destination coordinates to graph nodes
  - computes shortest-path distances with `networkx.single_source_dijkstra_path_length`
  - converts metric distances to modal impedance using `utils/get_impedance.py`
  - converts impedance to decay using `utils/decay.py`

The non-bus stage does **not** finish the final accessibility score itself. It stores intermediate modal decay arrays and the snapped POI coordinates needed to later join bus routing results.

#### Non-bus cache contents

Each per-node cache file stores:

- `origin`
- `services`
- per-service entries for each POI type
- either:
  - a direct cached accessibility value, or
  - `decay_walk`, `decay_bike`, `decay_drive`, and `poi_coords`

This design avoids recomputing shortest-path work for unchanged nodes.

### 5. Accessibility Stage

Implemented in [`accessibility_stage.py`](accessibility_stage.py).

Goal: combine non-bus modal decays with bus impedances, compute POI-level accessibility, and organize the result by service.

#### Inputs

- non-bus per-node cache files from `cache/non_bus`
- bus routing CSV artifacts:
  - `outputs/r5r_expanded_travel_time_matrix.csv`
  - `outputs/r5r_origins.csv`
  - `outputs/r5r_dest.csv`
- decay and aggregation logic from [`utils/decay.py`](utils/decay.py) and [`utils/delta_g.py`](utils/delta_g.py)

#### What happens

1. Workers load each node's non-bus cache file.
2. Each worker loads `r5r_origins.csv`, `r5r_dest.csv`, and `r5r_expanded_travel_time_matrix.csv` once into an in-memory `(from_id,to_id) -> impedance` lookup.
3. For each origin/destination lookup, they resolve IDs and retrieve bus impedance from that lookup.
5. For each POI type entry:
   - compute the decay parameter `beta = log(2) / decay_constant`
   - convert each bus impedance into a bus decay value
   - merge walk, bike, drive, and bus decays into an RRA value with `delta_g.build_rra()`
   - convert the RRA list into one accessibility score with `delta_g.accessibility_from_rra()`
6. The computed RRA is saved to disk so later runs can reuse it.
7. Results are grouped back into `accessibility_by_service`.

#### What the helper functions mean

In [`utils/decay.py`](utils/decay.py):

- `distance_decay(beta, imp)` applies exponential distance decay
- `calculate_rra(...)` merges the four modal decay values into one route/resource availability score

In [`utils/delta_g.py`](utils/delta_g.py):

- `build_rra()` constructs one RRA value per POI candidate
- `accessibility_from_rra()` aggregates the list of RRA values for a POI type into a single accessibility value using a contribution curve
- `merge_rra_and_accessibility()` packages both steps together

The output of this stage is one `AccessibilityNodeResult` per origin node, containing service-grouped POI accessibility values.

### 6. Service Aggregation Stage

Implemented in [`service_stage.py`](service_stage.py).

Goal: aggregate the accessibility values of all POI types that contribute to the same service.

For each node:

1. Read `node.accessibility_by_service`.
2. For each service, collect the accessibility values of all its POI types.
3. Apply `utils.services.choquet_integral(values, service)`.
4. Store the resulting `service_scores` in a `ServiceNodeResult`.

This is the stage where multiple POI types such as `restaurant`, `fast_food`, and `bakery` become a single service score such as `eating_out`.

### 7. Capability Aggregation Stage

Implemented in [`capability_stage.py`](capability_stage.py).

Goal: aggregate service scores into final capability scores and write the final CSVs.

For each node:

1. Read the service scores produced by the service stage.
2. Split them into the service sets for:
   - `restorativeness`
   - `nutrition`
   - `care`
3. Apply `utils.capabilities.choquet_integral(...)` for each capability.
4. Write one row per node to each output CSV.

Generated files:

- [`outputs/capability_restorativeness.csv`](outputs/capability_restorativeness.csv)
- [`outputs/capability_nutrition.csv`](outputs/capability_nutrition.csv)
- [`outputs/capability_care.csv`](outputs/capability_care.csv)

Each file contains:

- `node_id`
- `lat`
- `lon`
- the final capability score
- the contributing service scores used to compute that capability

## Data, Cache, and Output Folders

### [`graph`](graph)

Mode-specific GraphML files used by OSMnx and NetworkX.

### [`poi`](poi)

Cached POI extracts saved as GeoJSON, usually one file per OSM tag query.

### [`cache`](cache)

Runtime caches, especially:

- non-bus per-node caches
- POI snap caches

### [`poi_geom_cache`](poi_geom_cache)

Cached geometry-level POI extraction artifacts reused during snapping and accessibility work.

### [`rra_cache`](rra_cache)

Cached RRA/accessibility artifacts for origin/POI combinations.

### [`gtfs`](gtfs)

Transit and street network files consumed by `r5r`.

### [`outputs`](outputs)

Final capability CSVs plus intermediate routing artifacts such as:

- `r5r_origins.csv`
- `r5r_dest.csv`
- `r5r_expanded_travel_time_matrix.csv`
- `r5r_chunks/`

## Running the Pipeline

The repository entry point is:

```bash
python main.py
```

Operational requirements inferred from the codebase:

- Python packages used by the pipeline include `osmnx`, `networkx`, `geopandas`, `pandas`, `shapely`, `tqdm`, and `shutup`
- transit routing requires `Rscript`
- the R environment must include `r5r` and `data.table`
- Java is required by `r5r`

## Summary

The pipeline is organized as a layered aggregation process:

1. fetch and define POIs
2. align POIs to transport graphs
3. compute modal routing/access costs
4. convert costs to decayed access values
5. aggregate POI access into services
6. aggregate services into capabilities

If you want to understand the repo quickly, start with:

1. [`main.py`](main.py)
2. [`config/poi_types.csv`](config/poi_types.csv)
3. [`snapping_stage.py`](snapping_stage.py)
4. [`non_bus_routing_stage.py`](non_bus_routing_stage.py)
5. [`accessibility_stage.py`](accessibility_stage.py)


