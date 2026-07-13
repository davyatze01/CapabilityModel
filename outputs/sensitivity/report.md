# Capability model — parameter sensitivity (Cagliari)

* Input: `experiments/Cagliari_capability_9.csv` (1563 nodes)
* Baseline: q_factor=0.25, p_factor=0.75, lambda=0.65, veto=inf, uniform weights

## Parameter sensitivity (OAT sweeps)

% of nodes whose assigned class changes vs baseline:

### q_factor

```
capability  care  nutrition  restorativeness
value                                       
0.20         3.2       0.26             0.38
0.25         0.0       0.00             0.00
0.30         3.2       0.06             0.32
```

### p_factor

```
capability  care  nutrition  restorativeness
value                                       
0.65        4.03       1.22             1.79
0.75        0.00       0.00             0.00
0.85        4.29       1.22             1.54
```

### lambda

```
capability   care  nutrition  restorativeness
value                                        
0.60         8.89       0.96            18.55
0.65         0.00       0.00             0.00
0.70        10.36       0.90             2.56
```

## Weight perturbation (Dirichlet around uniform)

                 mean    std   50%    max
capability                               
care             8.33  11.90  5.37  65.26
nutrition        1.21   4.25  0.64  35.51
restorativeness  4.44   6.75  1.54  21.05

Robustness to input noise now lives in robustness_analysis.py / robustness_report.py.
