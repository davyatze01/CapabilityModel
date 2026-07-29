# Authority Document for the Capability Model

## Purpose

This document is the normative reference for the repository's capability model. It defines the objects that exist in the model, the dynamics by which they interact, the implementation boundaries that realize those dynamics, and the authority rules that govern interpretation when documents or code disagree.

The repository implements a geospatial capability model that estimates how well locations in a study area can access opportunities supporting everyday life and wellbeing. The model produces three final capability scores:

- `restorativeness`
- `nutrition`
- `care`

This document does not merely describe the system. It states the canonical structure of the model and the rules that repository work must follow.

## Taxonomy Summary

The model shall be read through four taxonomies:

1. Object content
2. Dynamics
3. Implementation
4. Authority

These taxonomies are not interchangeable. Object content defines what the model contains. Dynamics defines how those contents interact over time and across stages. Implementation defines which files and modules realize the model. Authority defines which definitions are binding and how conflicts are resolved.

## 1. Object Content

### Core Objects

The model shall use the following core objects as its conceptual vocabulary:

- Study area
- Transport-network node
- Mode graph
- Place of interest (`POI`)
- Service
- Capability
- Artifact bundle
- Scenario
- Configuration preset

### Model Entities

- A **study area** is the spatial extent being analysed.
- A **transport-network node** is the unit of evaluation for accessibility and capability scoring.
- A **mode graph** is the network representation used for one travel mode.
- A **POI** is a destination or opportunity relevant to a service.
- A **service** groups POI types into a meaningful domain category.
- A **capability** aggregates services into one of the final scores.
- An **artifact bundle** stores expensive intermediate results for reuse.
- A **scenario** modifies one or more assumptions for comparative analysis.
- A **configuration preset** defines a named analysis setup for a specific study area.

### Domain Hierarchy

The domain hierarchy shall be interpreted as:

`POI types -> services -> capabilities`

This hierarchy is the canonical domain structure. All interpretation of scores shall respect this structure. Low capability values shall be traceable back to the contributing services and POI types.

### Canonical Input Files

The following files are authoritative for domain content:

- [`config/poi_types.csv`](config/poi_types.csv)
- [`config/services.csv`](config/services.csv)

The following file is a generated representation and shall be treated as derived from its source module:

- [`config/capability.csv`](config/capability.csv)

The Python modules that load or generate these files are secondary to the stored CSVs only when the CSV is the intended editable source. Where a module is explicitly responsible for generation, the module is the authoritative source and the generated file is a reproducible artifact.

## 2. Dynamics

### Pipeline Dynamics

The model shall execute as a staged pipeline with the following canonical flow:

1. Select study area and configuration preset
2. Load or build mode graphs
3. Load POIs
4. Snap POIs to the graphs
5. Compute public transport impedance
6. Compute walking, cycling, and driving impedance
7. Convert impedance into decay-based accessibility
8. Aggregate accessibility into POI-level access
9. Aggregate POI-level access into service scores
10. Aggregate service scores into capability scores
11. Export tables and spatial outputs

This ordering is normative unless a documented scenario or cached artifact permits safe skipping of an upstream stage.

### Multimodal Dynamics

Accessibility shall be evaluated across walking, cycling, driving, and public transport where data are available.

For each origin-destination pair, each mode shall produce an impedance value. Impedance values shall then be transformed into decay values in the range `0` to `1`. The model shall combine the mode-specific values into a multimodal access measure rather than treating a single poor mode as decisive when another realistic mode provides access.

### Service and Capability Dynamics

The aggregation dynamics shall be:

`impedance -> decay value -> POI accessibility -> service score -> capability score`

Service aggregation shall preserve interpretability. Capability aggregation shall preserve the separation between services so that outputs can be audited against their contributing domain components.

### Caching and Reuse Dynamics

The pipeline shall treat routing and geospatial processing as expensive upstream operations. When a valid artifact bundle exists, the system may reuse it to skip recomputation of those stages.

The following reuse rules apply:

- Graph outputs shall be reused when compatible with the selected configuration.
- POI caches shall be reused when source and study-area settings match.
- Snapping and routing artifacts shall be reused when metadata match the current run.
- Downstream scenario analysis shall reuse baseline artifacts when the scenario changes only downstream assumptions.

### Scenario Dynamics

Scenarios shall modify the model by changing one or more controlled assumptions without redefining the entire pipeline. A scenario may disable a mode, alter a parameter, or adjust downstream aggregation, but it shall preserve comparability with the baseline unless explicitly stated otherwise.

## 3. Implementation

### Canonical Entry Points

The following files shall be treated as the principal implementation entry points:

- [`main.py`](main.py)
- [`pipeline_runner.py`](pipeline_runner.py)
- [`context.py`](context.py)
- [`config.py`](config.py)

These files define orchestration, execution context, configuration, and workflow coordination.

### Module Responsibilities

The implementation shall be interpreted using the following module responsibilities:

- [`bus_routing_stage.py`](bus_routing_stage.py) prepares public transport inputs and invokes the R routing script.
- [`utils/r5_routing.r`](utils/r5_routing.r) performs the public transport routing computation.
- [`non_bus_routing_stage.py`](non_bus_routing_stage.py) computes walking, cycling, and driving routing outputs.
- [`snapping_stage.py`](snapping_stage.py) performs POI-to-network snapping.
- [`accessibility_stage.py`](accessibility_stage.py) converts impedance into decay-based accessibility values.
- [`service_stage.py`](service_stage.py) aggregates POI accessibility into service scores.
- [`capability_stage.py`](capability_stage.py) aggregates service scores into capability scores.
- [`artifact_bundle.py`](artifact_bundle.py) manages cached intermediate artifacts.
- [`scenarios.py`](scenarios.py) defines scenario modifications.
- [`run_cities.py`](run_cities.py) executes batch runs across multiple municipalities.
- [`plot_shapefile.py`](plot_shapefile.py) and [`generate_experiment_shapefiles.py`](generate_experiment_shapefiles.py) generate spatial outputs.

### Data Contract Modules

The following modules shall be treated as the canonical contract layer for shared data structures and helpers:

- [`pipeline_types.py`](pipeline_types.py)
- [`utils/services.py`](utils/services.py)
- [`utils/capabilities.py`](utils/capabilities.py)
- [`utils/decay.py`](utils/decay.py)
- [`utils/get_impedance.py`](utils/get_impedance.py)
- [`utils/delta_g.py`](utils/delta_g.py)

### External Dependencies

The following implementation facts are part of the authoritative model description:

- Public transport routing depends on R, Java, GTFS data, and the `r5r` package.
- Walking, cycling, and driving routing use network-based shortest-path computation.
- QGIS integration is optional and is used only for presentation and spatial inspection.

## 4. Authority

### Authority Hierarchy

When interpreting the repository, the following order of authority shall apply:

1. The concrete data files that define model content
2. The Python and R code that implements those data files and pipeline stages
3. This authority document
4. Other descriptive documentation such as implementation notes, tutorials, and scenario notes

If a lower-level document conflicts with a higher-level source, the higher-level source shall prevail.

### Source of Truth Rules

- Configuration values shall be sourced from `config.py` unless overridden by an explicitly selected preset.
- Domain definitions shall be sourced from the CSV files in `config/`.
- Generated outputs shall not be treated as authoritative inputs.
- Cached artifacts shall be treated as reproducible intermediates, not as the source of model semantics.
- Descriptive documentation shall never override executable code or canonical data definitions.

### Normative Language

In this document:

- `shall` indicates a binding requirement
- `should` indicates a recommended practice
- `may` indicates an allowed option
- `must` is used only where a hard constraint is intended

### Change Control

Changes to the authoritative model shall be made in the correct layer:

- Change domain meaning in the configuration CSV files or the generating module that owns them.
- Change pipeline behavior in the corresponding stage module.
- Change shared contracts in the shared type or utility modules.
- Change study-area setup in `config.py` and `context.py`.

Documentation updates alone shall not be considered sufficient when the underlying implementation has changed.

## Canonical Interpretation of the Model

The model shall be interpreted as a staged, multimodal, cached accessibility pipeline that maps network-based opportunity access into service scores and then into capability scores. The following statements are authoritative:

- The model is node-based rather than area-based at the primary computation level.
- The model is multimodal rather than single-mode.
- The model is layered rather than flat.
- The model is cache-aware rather than recomputing all expensive steps every run.
- The model is configuration-driven rather than hard-coded for a single city.

## Summary of Binding Structure

The repository shall be understood as:

`object content + dynamics + implementation + authority`

These four taxonomies define the model completely enough for analysis, maintenance, and extension.
