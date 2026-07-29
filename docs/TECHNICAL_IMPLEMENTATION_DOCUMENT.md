# Technical Implementation Document

## Purpose and Scope

This repository implements a geospatial capability model. Its purpose is to estimate how well different places in a study area can access opportunities that support everyday life and wellbeing.

The final results are three capability scores:

- `restorativeness`: access to places that support recovery, leisure, nature contact, quietness, and cultural experience.
- `nutrition`: access to food-related opportunities.
- `care`: access to health, emergency, medicine, and care-related opportunities.

The model works at the level of transport-network nodes, which are points in the street or path network, often road junctions or path intersections. For each node, the model asks: "From this location, how accessible are the relevant opportunities?" The pipeline answers that question by loading mode graphs and places of interest, computing travel costs across multiple modes, converting those costs into normalized access values through a decay function, and aggregating the results up through a hierarchy of services into final capability scores.

The main entry point is [main.py](main.py). Configuration is defined in [config.py](config.py), which already contains presets for Cagliari and Paris. Switching between those study areas requires only selecting the corresponding preset key.

This document describes the workflow currently implemented in the repository. Future features planned to implement are described in the "Future Work" section.

## System Overview

At a high level, the system is a staged analysis pipeline:

```text
Study area and domain configuration
  -> mode graphs and POIs
  -> multimodal travel cost
  -> decay-based access values
  -> POI accessibility
  -> service scores
  -> capability scores
  -> CSV, plots, shapefiles, GeoPackage, optional QGIS project
```

The pipeline is mainly written in Python, with the exception of public transport routing which is handled by an R script. Each step in the sequence above corresponds to a dedicated stage. The stages are orchestrated by [main.py](main.py), which decides which stages to run and in what order, and by [pipeline_runner.py](pipeline_runner.py), which provides shared helpers for running those stages and assembling their outputs. All stages read from a common configuration object defined in [config.py](config.py), which controls everything from which city to analyse to where outputs are saved. Before any stage runs, [context.py](context.py) uses that configuration to load the transport graph, resolve the study area boundary, and prepare the service groupings that the later aggregation steps depend on, therefore setting up the shared state that every stage will need. The data structures passed between stages, such as what an accessibility value looks like and what a service score contains, are defined in [pipeline_types.py](pipeline_types.py), making the contract between stages explicit. Public transport travel times are computed outside Python entirely: [bus_routing_stage.py](bus_routing_stage.py) prepares the inputs, calls [utils/r5_routing.r](utils/r5_routing.r), and brings the results back into the pipeline as an impedance matrix.

The model is explicitly multimodal. For each origin and destination, it considers walking, cycling, driving, and public transport where data are available. Each mode produces an impedance value (a travel cost) that is converted into a 0-to-1 decay value and then combined with the other modes to estimate accessibility.

Once all impedances have been computed, they are saved together in a compressed artifact bundle. Any subsequent run that finds a valid bundle can skip the routing stages entirely and jump straight to the decay and aggregation steps. This is possible because impedances reflect the physical urban structure, such as the road network and transit timetables, which does not change between experiments. Parameters that researchers are more likely to adjust, such as decay constants, service weights, or capability definitions, sit entirely downstream of the routing stages and can therefore be modified and re-evaluated without ever recomputing travel costs.

## Domain Model: POIs, Services, and Capabilities

The model contains not just routing logic but also a conceptual mapping from real-world places to human capabilities. Places of interest (POIs) are the raw inputs: sport facilities, food retail, healthcare services, natural areas, cultural venues, and so on. These are grouped first into services (meaningful categories of opportunity such as food access or emergency care) and then into the three final capabilities.

This hierarchy matters because it keeps the model interpretable. A low care score can be traced back through the relevant services to the specific POI types behind them. Keeping the domain definitions in CSV files ([config/poi_types.csv](config/poi_types.csv) and [config/services.csv](config/services.csv)) means the conceptual assumptions are visible and editable without touching the analysis code. [utils/services.py](utils/services.py) reads and validates these files, and [utils/capabilities.py](utils/capabilities.py) defines the grouping from services into the three final capabilities.

## Domain Configuration Reference

The three CSV files below are the primary place to extend or adjust the model's domain assumptions.

### poi_types.csv

Each row defines one conceptual type of place. The pipeline reads this file to know which OSM features to collect, how sensitive access to that type of place is to travel time, and how it interacts with other place types in the same service.

- `poi_type`: A unique name for the place type. This is the identifier used everywhere else in the pipeline to refer to this row.
- `decay_coefficient`: Controls how quickly accessibility to this type of place drops off with travel time. It is the travel time in minutes at which the decay value reaches 0.5, meaning the place is considered half as useful. A larger value makes the model more tolerant of longer journeys for that type (for example, a hospital is given a large value because people will travel further for it, while a convenience store is given a small value because a faraway one is largely irrelevant).
- `choquet_interactions`: A JSON array of interaction coefficients between this POI type and every other POI type in the same service. The values appear in the same order as the `poi_types` list in [config/services.csv](config/services.csv). The entry corresponding to this type itself is `None`. Positive values indicate synergy (having both types nearby is more valuable than the sum of their individual contributions); negative values indicate substitution (having one largely covers the need for the other). These values feed the Choquet integral used during service aggregation.
- `tags`: The OpenStreetMap key-value tags used to identify and query places of this type. When POIs are loaded from shapefiles rather than OSM, the `labels` column is used instead.
- `labels`: Identifiers used when loading POIs from a shapefile source instead of OSM. These can be OSM-derived tag strings or codes specific to the shapefile dataset.

### services.csv

Each row defines one service, which is a meaningful grouping of POI types. The pipeline aggregates POI-type accessibility values into service scores using a Choquet integral, where the weights and saturation behaviour are controlled by this file.

- `service`: A unique name for the service. This name is referenced in [config/capability.csv](config/capability.csv).
- `poi_types`: The ordered list of POI types that make up this service. The order must match the order used in `choquet_capacity`, `contribution_coefficient`, and the `choquet_interactions` arrays in [config/poi_types.csv](config/poi_types.csv).
- `choquet_capacity`: The Choquet singleton capacity for each POI type, in the same order as `poi_types`. These values represent the relative importance of each type within the service and are used as weights in the Choquet integral. They should sum to approximately 1.
- `contribution_coefficient`: Controls how quickly each POI type's accessibility saturates as more instances are reachable. A higher value means that even a single nearby instance of this type yields a strong contribution; a lower value means the contribution grows more gradually as more instances are accessible. The values are in the same order as `poi_types`.

### capability.csv

Each row defines one of the three final capability scores. This file is generated automatically from [utils/capabilities.py](utils/capabilities.py) when the module is imported, so manual edits may be overwritten. The weights and structure should be changed in [utils/capabilities.py](utils/capabilities.py) directly.

- `capability`: The name of the capability score.
- `services`: The list of services aggregated into this capability.
- `electre_weight`: The weight assigned to each service in the ELECTRE III aggregation, in the same order as `services`. Equal weights mean all services contribute equally to the final score.
- `enabled`: Whether this capability is computed in the current run. Setting this to `False` skips the capability entirely.

## Study-Area Configuration

The same model can run for different cities without rewriting any analysis logic. [config.py](config.py) defines named presets that bundle city name, shapefile boundary, POI source paths, cache locations, output locations, routing settings, and QGIS options together. When switching cities, selecting the correct preset is the only required change. This also reduces the risk of accidentally mixing input files from different study areas.

[context.py](context.py) consumes the configuration at startup to load the base transport graph, create output folders, identify valid graph nodes, and prepare the service groups that later stages depend on.

## Mode Graph and POI Loading

Mode graphs define where movement is possible. The pipeline loads or builds three separate graphs for walking, cycling, and driving, stored as GraphML files under `graph`. Each graph is a street network extracted from OpenStreetMap and represents the traversable edges and nodes for that mode.

There are two ways to download the graphs, controlled by the `use_shapefile` setting in [config.py](config.py). When a shapefile boundary is provided, the graph is extracted from OSM data clipped to the polygon defined by that shapefile. When no shapefile is used, the graph is downloaded by city name using OSMnx's `graph_from_place`, which queries the Nominatim geocoder to resolve the study area boundary. The shapefile approach gives more precise control over the study area extent; the city-name approach is simpler but depends on how Nominatim resolves the name.

POIs define the destinations that contribute to services and capabilities. They can be loaded from shapefiles or queried directly from OpenStreetMap, again depending on configuration. When loading from shapefiles, the pipeline expects three separate files: one for point geometries, one for line geometries, and one for polygon geometries. Their paths are listed in `poi_shapefile_paths` in [config.py](config.py). The pipeline reads all three and concatenates them, so every geometry type is covered regardless of how the original data was produced. Downloaded or extracted POI data are cached under `poi` so later runs can reuse them without repeating slow download steps.

Relevant files: [utils/graphml.py](utils/graphml.py), [utils/load_shapefile.py](utils/load_shapefile.py), [config.py](config.py).

## Snapping POIs to the Network

Routing algorithms operate on network nodes, not arbitrary map coordinates. Before routing can begin, every POI must be assigned to nearby nodes on each city graph, a process called snapping. Point POIs are snapped directly. Line or polygon POIs such as large parks or complex facilities can produce multiple candidate access points, which is intentional: a single entrance point for a large area may be misleading, and keeping multiple candidates lets the model choose a more realistic access point for each origin.

Walking, cycling, and driving graphs are snapped separately. Public transport reuses the walking snap points as its access locations. Snapping is implemented in [snapping_stage.py](snapping_stage.py).

## Public Transport Routing

Public transport routing is the most resource-intensive step in the pipeline. It depends on GTFS timetable data, OSM street data, R, Java, and the `r5r` package. The Python code in [bus_routing_stage.py](bus_routing_stage.py) prepares origin and destination tables, then delegates the actual routing to the R script [utils/r5_routing.r](utils/r5_routing.r). Results come back as a bus impedance matrix (a table where each row is an origin and each column is a destination), computed from waiting and travel time according to the configured formula.

Because this computation is expensive, results are stored with metadata so the system can verify that a cached matrix matches the current run configuration before reusing it.

## Walking, Cycling, and Driving Routing

Non-motorized and private-vehicle routing is handled by [non_bus_routing_stage.py](non_bus_routing_stage.py), which computes route costs using NetworkX shortest-path calculations over the loaded transport graphs. Distances are converted into travel-time-like impedance values. Walking impedance can additionally be adjusted by a walkability score computed in [walkability.py](walkability.py), which captures local street-environment quality rather than pure geometric distance.

Results are cached per origin node in the artifact bundle so they do not need to be recomputed every run. This separation is deliberate: storing routing ingredients independently allows later stages to recompute accessibility under different assumptions without re-running shortest-path calculations.

Relevant utilities: [utils/delta_g.py](utils/delta_g.py), [utils/get_impedance.py](utils/get_impedance.py).

## Decay and Multimodal Accessibility

Travel time alone is not a useful final output. A destination two minutes away and one twenty minutes away are qualitatively different, but the raw numbers do not express that difference in a way that aggregates cleanly. The accessibility stage in [accessibility_stage.py](accessibility_stage.py) addresses this by converting each impedance value into a decay value between 0 and 1. A value close to 1 means the destination is easy to reach by that mode; a value close to 0 means it is difficult or effectively unreachable. The shape of the decay curve is controlled by a decay constant defined per POI type in [config/poi_types.csv](config/poi_types.csv), allowing different destination types to have different sensitivity to travel cost.

Multimodality is central to the model's design. A location should not be treated as inaccessible simply because one mode is poor if another realistic mode provides access. The stage therefore combines the mode-specific decay values into one multimodal access value that reflects the best combined availability across walking, cycling, driving, and public transport.

Relevant utilities: [utils/decay.py](utils/decay.py), [utils/delta_g.py](utils/delta_g.py).

## Service Aggregation

Once each POI type has an accessibility value, those values are grouped into services according to the mapping in [config/services.csv](config/services.csv). The service score is computed with a normalized Choquet integral, an aggregation method that can represent complementarity: some combinations of POI types are more valuable together than their individual contributions would suggest. This intermediate layer of service scores makes the model easier to interpret and audit before reaching the final capability level.

Service scores are cached in a matrix for faster reuse across experiments. Relevant files: [service_stage.py](service_stage.py), [utils/services.py](utils/services.py).

## Capability Aggregation

Service scores are combined into the three final capability scores in [capability_stage.py](capability_stage.py). The current implementation uses an ELECTRE III based scoring function from [utils/capabilities.py](utils/capabilities.py). ELECTRE III is a multi-criteria decision method that combines several service dimensions into one normalized capability score while preserving the ability to express qualitative thresholds and preference intensities between dimensions.

The stage writes one CSV per capability, then moves those CSVs into `experiments` and appends average capability values to `experiments/capability_experiments_recap.csv`.


## Artifact Bundles and Caching

Routing and geospatial processing are the slowest parts of the pipeline. The most expensive outputs, including snapping results, public transport matrices, and non-bus routing ingredients, are saved into a compressed artifact bundle under `artifacts/{artifact_slug}` by [artifact_bundle.py](artifact_bundle.py). When a valid bundle exists, [main.py](main.py) skips those early stages entirely. All later stages (accessibility computation, service aggregation, capability scoring, and spatial output) can then run quickly from the saved artifacts.

This design separates expensive route computation from faster scenario and post-processing runs, making repeated experiments practical without requiring a full rerun of the pipeline each time.


## Scenario Analysis

Scenario analysis is built on top of the artifact bundle mechanism. Rather than recomputing routes from scratch, a scenario modifies one element of the model: for example, the public strike scenario in [scenarios.py](scenarios.py) disables public transport by making bus impedance effectively unreachable, and then reruns only the downstream stages: accessibility, service aggregation, capability aggregation, and spatial output generation. The scenario pipeline then compares results against the stored baseline and writes comparison tables and plots.

This makes "what if" questions cheap to answer, as long as suitable baseline artifacts already exist.

## Batch City Runs

[run_cities.py](run_cities.py) enables running the pipeline across many municipalities. It iterates over a configured list of cities, calls [main.py](main.py) for each one, skips cities that already have the three expected capability CSVs, and applies a timeout to prevent one city from blocking the whole batch. This supports scaling the analysis beyond individual case studies.


## Statistical Scenario Testing

[capability_significance_test.R](capability_significance_test.R) provides a post-processing check for whether differences between baseline and public-strike scenario capability scores are statistically meaningful. It selects between a paired t-test and a paired Wilcoxon test depending on a normality diagnostic, then writes statistical test results to a CSV file.


## Spatial Outputs and QGIS Support

Beyond CSV tables, the pipeline exports results in formats suited for geographic inspection. [pipeline_runner.py](pipeline_runner.py), [plot_shapefile.py](plot_shapefile.py), and [generate_experiment_shapefiles.py](generate_experiment_shapefiles.py) together produce capability plots, shapefiles, and a GeoPackage (a single-file GIS format that bundles multiple spatial layers in one file).

The GeoPackage contains two layers. The first is a point layer where each point corresponds to one evaluated transport-network node and carries the computed capability scores as attributes. The second is a hexagonal grid layer that tessellates the study area into cells of configurable size. Each hexagonal cell is attributed with the average capability score of all node points that fall within it and is coloured accordingly using a graduated colour ramp, making spatial patterns in the scores easy to read at a glance.

When QGIS is available, the pipeline resolves its executable automatically by searching common installation paths, or falls back to the path set in `qgis_bin_path`. It then runs a QGIS Python script that generates a styled project file with both layers loaded and symbolised. Once the project is ready, QGIS is launched and the project is opened automatically, so the results are immediately visible without any manual file-loading steps.

## Architecture and Main Flows

### Main Workflow

The main workflow is:

```text
1. Select study area and configuration
2. Load or build mode graphs
3. Load POIs
4. Snap POIs to the mode graphs
5. Compute public transport impedance
6. Compute walking, cycling, and driving impedance
7. Convert impedances into 0-to-1 decay values
8. Combine modes into multimodal POI accessibility
9. Aggregate POI accessibility into service scores
10. Aggregate service scores into capability scores
11. Export CSV and spatial outputs
```

The staged implementation is coordinated by [main.py](main.py). Reusable workflow helpers are in [pipeline_runner.py](pipeline_runner.py).

### Multimodal Flow

For each origin and destination, the model considers four transport modes (walking, cycling, driving, and public transport), each following the same pattern:

```text
route or travel-time estimate
  -> impedance
  -> 0-to-1 decay value
```

The mode-specific decay values are then combined into one multimodal access value, ensuring that a location is not penalized for poor access by one mode if another realistic mode provides good access.

### Aggregation Flow

The model aggregates information in layers:

```text
Individual POIs
  -> POI-type accessibility
  -> service scores
  -> capability scores
```

This layered structure keeps the final capability score interpretable. A low care score can be traced back to the relevant care services and then to the POI types behind those services.

### Cache and Reuse Flow

Caches are used throughout the pipeline to avoid repeating slow operations:

- Graphs are stored under `graph`.
- POIs are cached under `poi`.
- Snapping and routing artifacts are stored under `artifacts`.
- Final experiment CSVs are stored under `experiments`.
- GIS outputs are stored under `outputs`.

The most important reuse mechanism is the impedance artifact bundle created by [artifact_bundle.py](artifact_bundle.py), which allows the model to skip expensive routing work when suitable routing artifacts already exist.

## Config

The following settings can be changed in [config.py](config.py) to control how the pipeline runs. All of them are fields of the `PipelineConfig` dataclass.

- Study city preset, used to select a named preset (`study_city`)
- Full city name, used for graph downloads and labelling (`city_name`)
- Whether to clip the analysis to a shapefile boundary (`use_shapefile`)
- Name of the boundary shapefile (`name_shapefile`)
- Whether POIs are loaded from shapefiles rather than queried from OpenStreetMap (`poi_from_shp`)
- Paths to the three shapefile POI sources, one per geometry type: points, lines, and polygons (`poi_shapefile_paths`)
- Number of parallel workers for routing (`worker_count`)
- Maximum number of parallel workers for non-bus routing (`non_bus_max_workers`)
- Whether to skip routing entirely and rely on cached artifacts (`skip_routing`)
- Root directory where artifact bundles are stored (`artifacts_root_dir`)
- Departure date and time for public transport routing (`bus_departure_dt`)
- Weight applied to waiting time in the bus impedance formula (`bus_gamma`)
- GTFS feed file paths for public transport (`gtfs_feeds`)
- Whether the OSM PBF file for r5r is built automatically (`osm_pbf_autobuild`)
- Whether to open QGIS automatically after the run (`open_qgis_after_run`)
- Path to the QGIS executable (`qgis_bin_path`)
- Path where the QGIS project file is written (`qgis_project_path`)
- Capability field used for auto-styling the QGIS layer (`qgis_autostyle_field`)
- Number of classification classes in the QGIS style (`qgis_autostyle_classes`)
- Colour ramp used for the QGIS style (`qgis_autostyle_ramp`)
- Whether to include a hexagonal grid layer in QGIS (`qgis_grid_enabled`)
- Cell size of the hexagonal grid in metres (`qgis_grid_cell_size_m`)
- Whether accessibility and service matrices are cached between runs (`accessibility_matrix_cache_enabled`, `service_matrix_cache_enabled`)
- Node and POI limits for debug or test runs (`debug_max_nodes`, `debug_max_pois`)

## API, Data, and Component Contracts

### Stage Outputs

The main conceptual stage outputs are:

- Snapped POI locations from [snapping_stage.py](snapping_stage.py).
- Public transport impedance matrix from [bus_routing_stage.py](bus_routing_stage.py).
- Walking, cycling, and driving routing ingredients from [non_bus_routing_stage.py](non_bus_routing_stage.py).
- POI accessibility values from [accessibility_stage.py](accessibility_stage.py).
- Service scores from [service_stage.py](service_stage.py).
- Capability CSVs from [capability_stage.py](capability_stage.py).
- GIS outputs from [generate_experiment_shapefiles.py](generate_experiment_shapefiles.py).

### Final Output Files

- Capability CSV files in `experiments`.
- Recap CSV at `experiments/capability_experiments_recap.csv`.
- Plots under `plots`.
- Shapefile outputs under `outputs/shapefiles`.
- GeoPackage outputs under `outputs/gpkg`.
- Optional QGIS project outputs under `outputs/qgis`.

## Dependencies

### Python

Declared in [requirements.txt](requirements.txt):

- **NumPy** (`numpy`): array and numerical operations used throughout routing and aggregation stages.
- **Pandas** (`pandas`): tabular data handling for POI loading, matrix caches, and result export.
- **NetworkX** (`networkx`): graph data structures and shortest-path algorithms for walking, cycling, and driving routing.
- **OSMnx** (`osmnx`): downloading and building street-network graphs from OpenStreetMap, and querying OSM features as POIs.
- **GeoPandas** (`geopandas`): spatial dataframes for POI loading, snapping, shapefile and GeoPackage export.
- **Shapely** (`shapely`): geometric operations including buffering, polygon clipping, and point-in-polygon checks.
- **tqdm** (`tqdm`): progress bars for long-running routing and aggregation loops.
- **scikit-learn** (`scikit-learn`): used for preprocessing and normalization steps in the aggregation pipeline.
- **Matplotlib** (`matplotlib`): generating capability score plots and maps.
- **Seaborn** (`seaborn`): statistical plot styling used in scenario comparison outputs.
- **pyDecision** (`pyDecision`): provides the ELECTRE III implementation used during capability aggregation.
- **beautifulsoup4** (`beautifulsoup4`): HTML parsing used in utility scripts.
- **shutup** (`shutup`): suppresses noisy third-party warnings during pipeline execution.

### External

- **R** and `Rscript`: required to run the public transport routing script [utils/r5_routing.r](utils/r5_routing.r).
- **r5r** (R package): performs transit routing using GTFS timetables and an OSM street network.
- **Java**: required by `r5r` at runtime.
- **GTFS files**: public transport timetable feeds for the study area, used by `r5r`.
- **OSM PBF file**: the OpenStreetMap extract for the study area, used by `r5r` to build the routable network. Can be generated automatically if `osm_pbf_autobuild` is enabled.
- **QGIS**: used to generate styled project files and to open the GeoPackage output after the run. The pipeline resolves the QGIS executable automatically or falls back to the path set in `qgis_bin_path`.
- **DHARMa** (R package): required by [capability_significance_test.R](capability_significance_test.R) for normality diagnostics. Not installed by the Python requirements file.

## Future Work
- Create an interface that can be used to inspect the provenance of the scores and understand which pois contributed to said scores. In order to create said interface, some details of the current implementations should be updated:
  1) The output should return for each node the service scores
  2) The POIs should be displayed in the map alongside with the nodes
  3) For each (node, poi) pair the impedance score should be saved somewhere and should be multiplied with the parameter of the impedance in the modeling (still don't understand which one is it)

- The reasoning of the model should be in hexagon grids, not for each individual node. For each hexagon we select the node that it is closer to the centroid and then for each hexagon when having multiple pois we compute the impedance of each poi considering the node closest to the hexagon's centroid.

- Right now the study area and the boundary of pois extraction is the same. Instead, a threshold should be calculated establishing what is the further distance of pois I'm going to consider from the current area. This requires further brainstorming on the strategy to implement it.

- The non-bus routing cache currently writes one pickle file per OSM node, keyed by an arbitrary numeric node ID that carries no spatial information. A better approach would be to key the cache by H3 cell (already used for the hexagonal grid layer in QGIS output) instead of by node ID. Each H3 cell at an appropriate resolution would act as a single cache entry, representing all nodes that fall within it. Within a cell, the node closest to the cell centroid would be the canonical origin for routing. This would reduce the number of cache files from one per node to one per hex cell (potentially 10–100x fewer files), make the cache portable across graph versions that differ only in minor node-ID changes, and open the door to approximate cache hits where a node with no cached entry borrows the result from the nearest cached H3 neighbour.

- The impedance, decay and contribution function could have a different formulation that can be specified by the user. One should be able to use a functional notation to specify this parameter. This might not be needed for the 
