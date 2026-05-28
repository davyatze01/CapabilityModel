# Scenario Analysis Framework

The `scenarios.py` file provides a framework for running the capability model under different scenarios and comparing results. Currently, it implements the "public strike" scenario which disables all bus routing.

## Features

- **Scenario Modifiers**: Extensible base class for creating custom scenarios
- **Shared Impedance Bundle**: Reuses pre-computed routing results to efficiently explore variations
- **Automatic Comparison**: Loads baseline results and compares scenarios
- **Visualization**: Generates plots showing capability score distributions and differences
- **Statistical Summary**: Produces CSV with statistics for each capability and scenario

## Prerequisites

Before running scenarios, you must:

1. **Run the baseline pipeline** to generate the impedance bundle:
   ```bash
   python main.py
   ```
   This creates the `artifacts/[city_slug]/impedances.npz` file and baseline capability results.

2. **Install optional dependencies** (if not already installed):
   ```bash
   pip install pandas matplotlib
   ```

## Running Scenarios

### Quick Start

Run all scenarios and compare with baseline:

```bash
python scenarios.py
```

This will:
1. Load the pre-computed impedance bundle
2. Run the "public strike" scenario (bus routing disabled)
3. Compare results with baseline
4. Generate plots and statistics

### Output

Results are saved to `scenarios/{scenario_name}/`:

- **CSV Files**: 
  - `comparison_results.csv` - Full comparison with differences
  - `scenario_summary.csv` - Statistical summary

- **Plots**:
  - `comparison_restorativeness.png`
  - `comparison_nutrition.png`
  - `comparison_care.png`

Example structure:
```
scenarios/
  public_strike/
    comparison_results.csv
    scenario_summary.csv
    comparison_restorativeness.png
    comparison_nutrition.png
    comparison_care.png
```

The `scenarios/` folder is git-ignored, so scenario results won't be committed to the repository.

## Creating Custom Scenarios

To add a new scenario:

1. Create a subclass of `ScenarioModifier`:

```python
class MyScenario(ScenarioModifier):
    def modify_bus_routing(self, bus, ctx):
        # Optionally modify bus routing
        return bus
    
    def modify_non_bus_routing(self, non_bus, ctx):
        # Optionally modify non-bus routing
        return non_bus
    
    def get_name(self):
        return "my_scenario"
```

2. Add it to the `scenarios` list in `run_all_scenarios()`:

```python
scenarios = [
    PublicStrikeScenario(),
    MyScenario(),
]
```

## Scenario Descriptions

### Public Strike (`PublicStrikeScenario`)

**Purpose**: Evaluate accessibility under conditions where public transport is unavailable.

**Implementation**: Sets all bus impedance matrix values to infinity, effectively making all bus destinations unreachable.

**Expected Impact**:
- Capability scores should decrease in urban areas dependent on public transport
- Non-bus modes (walking, cycling, car) maintain their normal accessibility
- Rural areas may show minimal impact if they already have low bus service

## How It Works

1. **Load Impedance Bundle**: Reads pre-computed bus and non-bus routing results
2. **Apply Modifications**: Scenario modifiers alter routing data
3. **Run Pipeline Stages**:
   - Accessibility: Compute accessibility with modified routing
   - Service Aggregation: Combine accessibility into service opportunities
   - Capability Aggregation: Compute final capability scores
4. **Save Results**: Outputs saved with scenario suffix
5. **Restore Original**: Original bus matrix restored after scenario completes
6. **Compare & Visualize**: Aggregate results and generate plots

## Notes

- Each scenario temporarily modifies the bus impedance matrix file
- Original matrix is backed up and restored automatically
- Results are written to both `outputs/` and `experiments/` directories
- Comparison uses node IDs as keys to ensure spatial alignment
- All visualizations are saved as PNG files

## Troubleshooting

### No baseline results found
Make sure you've run the main pipeline first:
```bash
python main.py
```

### Memory issues
If running out of memory:
- Reduce `worker_count` in `config.py`
- Use `debug_max_nodes` to test with fewer nodes

### Files not found
Make sure the impedance bundle was generated successfully. Check:
```
artifacts/[city_slug]/impedances.npz
```

## Future Enhancements

Potential scenarios to implement:
- **Service Disruptions**: Disable specific service types (e.g., parks, shops)
- **Network Changes**: Add/remove connections or modify travel times
- **Seasonal Scenarios**: Different routing by season
- **Weather Impact**: Modified costs based on weather conditions
