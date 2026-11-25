# Capability assignment and scenario evaluation

Capability assignment retains ELECTRE TRI (`capability_score_mode = "discrete"`
in `core/config.py` and `CAPABILITY_SCORE_MODE = "discrete"` in
`analysis/scenarios.py`). Its weights, vetoes and thresholds still come from the
existing configuration. Capability maps retain the five ELECTRE display classes.

Scenario evaluation is separate: `service_mean_<capability>` is the equal-weight
arithmetic mean of all services listed for that capability in
`config/capability.csv`. Zero scores remain in the denominator. ELECTRE weights
are not used. The mean is computed at each sampled node and passes through the
existing spatial grid export, alongside the ELECTRE result. Grid fields are
named `grid_mean_service_mean_<capability>`; despite the existing `grid_mean_`
naming convention, this export currently assigns the nearest sampled node's
values to each measured display cell. No-data cells remain excluded.

The comparison CSV retains the existing ELECTRE columns and adds:

- `service_mean_<capability>_<arm>`: numerical service average.
- `service_mean_level_<capability>_<arm>`: one of ten 0.1-wide display bands.
- `dmean_<capability>_<a>_to_<b>`: average B minus average A.
- `dmeanlevel_<capability>_<a>_to_<b>`: display band B minus display band A.
- `evaluation_method` and `capability_score_mode`: provenance fields.

Scenario difference maps use service-average band changes (-9 through +9).
Absolute capability maps continue to show ELECTRE scores.

`analysis/significance_analysis.R` defaults to `SCORE_MODE <- "service_mean"`.
It tests numerical service averages and retains the spatial block bootstrap and
block sign-flip test. It records the aggregation and confidence-interval methods.
For example, a median difference of -0.04 is a reduction of four percentage
points on the 0-1 service-average scale, not a relative percentage reduction or
an ELECTRE class change. Block-size sensitivity remains necessary.

Regenerate baseline and all scenario capability CSVs, grids and comparison CSVs
before running the statistical analysis. Older grids and comparison CSVs without
explicit service-average fields are rejected; ELECTRE scores are never substituted
for means. Upstream routing and service caches can still be reused when compatible.
No existing result files are migrated automatically.

The scenario report generator describes the new measure. Existing interpretive
readings remain preserved in its source, but are withheld from newly generated
reports until reviewed against service-average results.

Validation:

```sh
python -m tools.test_capability_config
python -m tools.test_scenario_service_means
Rscript tools/test_service_mean_significance.R
```

These checks use temporary artifacts and do not run real city scenarios.
