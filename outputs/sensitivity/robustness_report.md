# Capability model — robustness to input noise (Cagliari)

* Input: `experiments/Cagliari_capability_9.csv` (1563 nodes)
* Baseline: q_factor=0.25, p_factor=0.75, lambda=0.65, veto=inf, uniform weights (all held fixed -- only the input service scores are perturbed)

## Robustness to input noise (Monte Carlo, 200 reps per sigma)

Stability = share of repetitions assigning the node's modal class.

     capability  sigma  mean_stability  pct_nodes_stability_lt_0.8  pct_nodes_stability_lt_0.5
restorativeness   0.02           0.968                       6.398                       0.000
restorativeness   0.05           0.924                      16.251                       0.000
restorativeness   0.10           0.864                      29.495                       0.000
      nutrition   0.02           0.954                       9.469                       0.000
      nutrition   0.05           0.872                      26.488                       0.000
      nutrition   0.10           0.738                      65.579                       0.192
           care   0.02           0.904                      19.834                       0.000
           care   0.05           0.781                      49.712                       0.000
           care   0.10           0.671                      86.628                       0.448

Per-node stability (for QGIS join on node_id): `robustness_node_stability.csv`
