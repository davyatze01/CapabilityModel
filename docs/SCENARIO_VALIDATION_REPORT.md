# Scenario Validation Report

This report explains how each accessibility scenario is compared against baseline, what
statistical test establishes whether an observed difference is real and how to read the results for each scenario. It documents the current hexagon-grid comparison and significance-testing workflow.

## 1. What is being compared, in general

Every scenario compares two or more **conditions** — a baseline plus one or more alternatives —
over the same set of roughly 1,568 hexagonal cells covering the study area.

The **baseline** is the run made without changing anything: the universal-traveler setup, full
transit feed, full mode set, standard affordability — the same conditions the model already runs
with by default. It is not itself a "finding"; it's the reference point every other condition in a
scenario is measured against. Each **alternative condition** changes exactly one thing (or a
clearly-scoped bundle of things — see each scenario's own description in Section 5) relative to that same
baseline: a bus service removed, a neighbourhood's POIs removed, a new transit line added, a
different traveler profile simulated. Whatever difference shows up between baseline and an
alternative condition is attributable to that specific change, precisely because everything else
about the run is held identical to baseline.

Each condition's capability score is computed independently for every hexagon, then the results
are lined up so that each hexagon has one score per condition, side by side.

## 2. Choosing a capability score: discrete bands vs. a continuous score

The model's production output places every hexagon into one of five ordered capability bands
(very low through very high) and reports the band's midpoint value. That is the right choice for
presenting results on a map, but it is a poor choice for detecting change statistically: two
hexagons that both remain in the "medium" band register as *zero* difference even if one moved
much closer to the boundary than the other, and the significance test used here treats exact-zero
differences as uninformative and discards them. If most hexagons don't fully cross a band
boundary, most of the sample ends up contributing nothing to the test.

To avoid discarding real, sub-band movement, this analysis uses a continuous version of the same
underlying score instead: the smooth outranking credibility that the banding procedure computes
internally, before it collapses everything into five bands. It uses the same
concordance/discordance/veto logic as the production score, without the final rounding step. It is
not a different measure of accessibility, only a higher-resolution readout of the same one. This
continuous mode is opt-in. It never affects the model's normal (production) output. Every result in
this report was generated with it turned on.

## 3. Baseline test: paired Wilcoxon signed-rank test

For every (scenario, capability, condition) combination, the analysis:

1. Keeps only hexagons that have valid data on both sides of the comparison.
2. Computes the paired difference in score between the two conditions, hexagon by hexagon.
3. Runs a paired Wilcoxon signed-rank test on those differences. The null hypothesis is that
   there is no systematic shift between the two conditions — i.e., that the paired differences are
   symmetric around zero.
4. Estimates an effect size via a bootstrap 95% confidence interval on the **median** difference
   (2,000 resamples).

## 4. Correction: spatial block sign-flip permutation test

To account for the fact that nearby hexagons are not independent, a second, spatially-aware test
is run alongside the naive Wilcoxon test, following the block/cluster permutation approach that is
standard practice in spatial statistics and neuroimaging when observations are geographically
correlated:

1. **Group nearby hexagons into blocks.** Each hexagon is assigned to a roughly 500m × 500m
   spatial cell based on its location; hexagons that land in the same cell form one block. This
   block size is a tunable sensitivity parameter, not something fit to the data — a sanity check
   is to confirm the resulting number of blocks is neither implausibly small (over-correcting) nor
   close to the number of hexagons (no different from treating them as independent).
2. **Test statistic.** The same signed-rank information the Wilcoxon test uses is combined into a
   single summary number for the whole comparison.
3. **Permutation.** Many times over (thousands of repetitions), every hexagon in a given block has
   its sign randomly and simultaneously flipped — never hexagon-by-hexagon — and the test
   statistic is recomputed. This is the core of the correction: under this resampling, correlated
   neighbours move together exactly as they would under a genuine, spatially clustered effect,
   instead of having that correlation averaged away as if it didn't exist.
4. **Empirical significance.** The observed test statistic is compared against the distribution of
   statistics produced by all those random permutations; the p-value is the fraction of
   permutations that were as extreme or more extreme than what was actually observed. Running more
   permutations lowers the smallest p-value the test can report (2,000 permutations bottoms out
   around 0.0005; 50,000 pushes that down to about 0.00002).
5. **Correcting for testing many things at once.** Because this analysis runs many such tests in a
   single report (three capabilities × several conditions per scenario), a standard multiple-comparison
   correction (Benjamini-Hochberg) is applied across all of them, for both the naive and the
   spatially-corrected p-values.

## 5. Per-scenario results

### 5.1 Public transport strike

**What changes vs. baseline**: every bus route in the city is made effectively unusable — as if a
citywide bus strike removed bus service entirely — while walking, biking, driving, the underlying
transit schedule data, and affordability are left untouched. Any difference from baseline is
therefore attributable to the loss of bus service alone, not to some other simultaneous change.

| capability | n hex | % changed | median Δ | 95% CI | p (block-perm) | n blocks |
|---|---|---|---|---|---|---|
| restorativeness | 1568 | 30.4% | 0.000 | [0, 0] | < 0.0005 (< 0.00002 @ 50k) | 151 |
| nutrition | 1568 | 20.7% | 0.000 | [0, 0] | < 0.0005 | 125 |
| care | 1568 | 68.8% | **−0.011** | [−0.012, −0.010] | < 0.0005 (< 0.00002 @ 50k) | 204 |

**Reading**: care is the capability most exposed to losing bus service — nearly 70% of hexagons
are affected, with a real (if modest) drop in the typical case. Restorativeness and nutrition show
localized changes — some areas' parks or food access are partly bus-dependent — but no net
citywide shift. The effect survives the spatial correction comfortably: this is a genuine, real
finding rather than a statistical artifact.

### 5.2 Neighbourhood service loss (Is Mirrionis)

**What changes vs. baseline**: every POI that feeds any of the three capabilities and sits
within the Is Mirrionis neighbourhood is removed from consideration entirely, as if that
neighbourhood's local capability-feeding infrastructure — all of it, not one category — vanished.
No routing, travel mode, speed, or transit-schedule change happens anywhere else in the city; only
this one neighbourhood's set of POIs differs from baseline.

| capability | n hex | % changed | median Δ | 95% CI | p (block-perm) | n blocks |
|---|---|---|---|---|---|---|
| restorativeness | 1568 | 48.8% | 0.000 | [0, 0] | < 0.0005 | 260 |
| nutrition | 1568 | 21.8% | 0.000 | [0, 0] | < 0.0005 | 152 |
| care | 1568 | 95.7% | **−0.032** | [−0.034, −0.031] | < 0.0005 (< 0.00002 @ 50k) | 333 |

**Reading**: care is again the most affected capability, both in size (−0.032, roughly three times
the size of the bus-strike effect) and in reach (nearly every hexagon shows some change). Removing
a neighbourhood's care-related POIs pulls down accessibility across a much larger surrounding area
than the removal footprint itself, because nearby hexagons partly relied on that neighbourhood as
one of several redundant options. Restorativeness and nutrition again show localized,
net-zero-median effects. The finding is robust to the spatial correction.

### 5.3 New metro line

**What changes vs. baseline**: a new transit line is added to the network — an extension of the
existing Metrocagliari line further into the city, with several new stops — while walking speed,
affordability, and the set of enabled travel modes are all identical to baseline. The only
difference from baseline is the presence and routing of this one additional line.

| capability | n hex | % changed | median Δ | p (block-perm) | n blocks |
|---|---|---|---|---|---|
| restorativeness | 1568 | 15.8% | 0.000 | < 0.0005 | 71 |
| nutrition | 1568 | 2.7% | 0.000 | < 0.0005 (< 0.00002 @ 50k) | 30 |
| care | 1568 | 28.5% | 0.000 | < 0.0005 | 81 |

**Reading**: this is the most spatially localized scenario by design — a single new corridor only
affects a narrow band of hexagons (2.7%–28.5%, depending on capability), so the citywide median
stays at zero for all three capabilities even though the effect within the corridor itself is real
and consistently positive. The spatial-block test confirms that the localized effect is genuine.

### 5.4 Traveler profiles: student vs. elderly

This scenario compares three distinct traveler profiles, each simulated as a completely
independent run (not a perturbation layered on baseline) — a universal baseline traveler, a
low-income student, and an elderly traveler with mobility constraints:

| parameter | baseline | student | elderly |
|---|---|---|---|
| travel modes available | walk, bike, drive, bus | walk, drive, bus (no bike) | walk, bus only |
| walking speed | 5 km/h | 5 km/h (same) | 2 km/h |
| affordability multiplier | 1.0 (full) | 0.2 (low) | 0.5 (reduced) |
| free-canteen benefit | full | full (same) | none |
| transit accessibility | standard feed | standard (same) | wheelchair-accessible stops only |

**What changes vs. baseline**: the student profile differs from baseline in exactly two respects —
losing access to biking, and a much lower affordability multiplier — with speed, the canteen
benefit, and transit accessibility unchanged. The elderly profile differs from baseline in every
respect at once (modes, speed, affordability, canteen benefit, and transit accessibility all
change together), so a baseline-vs-elderly difference can't be pinned on any single one of those
changes. Only the student-vs-elderly comparison isolates the effect of speed, mode restriction, and
transit accessibility with affordability held roughly comparable between the two (0.2 vs. 0.5,
both low). (n hex = 1568 for every row below.)

| capability | comparison | % changed | median Δ | 95% CI | p (block-perm) | n blocks |
|---|---|---|---|---|---|---|
| restorativeness | baseline→elderly | 100% | **−0.667** | [−0.674,−0.659] | < 0.0005 | 331 |
| restorativeness | baseline→student | 100% | **−0.946** | [−0.950,−0.940] | < 0.0005 | 331 |
| restorativeness | student→elderly | 81.4% | +0.250 | [0.250,0.250] | < 0.0005 | 243 |
| nutrition | baseline→elderly | 100% | **−0.680** | [−0.700,−0.663] | < 0.0005 | 331 |
| nutrition | baseline→student | 100% | **−0.723** | [−0.742,−0.709] | < 0.0005 | 331 |
| nutrition | student→elderly | 69.5% | 0.000 | [0,0] | **0.120 (not significant)** | 244 |
| care | baseline→elderly | 100% | **−0.605** | [−0.610,−0.602] | < 0.0005 | 331 |
| care | baseline→student | 100% | **−0.812** | [−0.816,−0.806] | < 0.0005 | 331 |
| care | student→elderly | 74.9% | +0.202 | [0.197,0.211] | < 0.0005 | 224 |

**Reading — two very different findings in this table:**

1. **Baseline-vs-persona shifts are large** (−0.6 to −0.95, on a [0, 1] scale) and survive the
   spatial correction comfortably. The size is disproportionate to what a mode or speed change
   alone would explain. The reason is the affordability model: the affordability multiplier (0.2
   for the student, 0.5 for the elderly traveler) discounts every POI's contribution equally,
   with the sole exception of free canteens. Nominally free POIs, like parks or walk-in public
   healthcare, get discounted by the same factor as POIs that actually cost money. This is a
   **modeling-scope decision, not a statistical artifact**: the spatial correction correctly
   confirms the effect is real and broad given how the model is currently specified. Before
   presenting these numbers as a mobility/routing finding, decide whether the blanket affordability
   discount is the intended framing ("capability deprivation," where low income erodes access to
   everything including nominally free space), or whether the discount should instead be scoped
   to only the POIs that are actually economically gated, the way canteens already are exempted.
2. **The student-to-elderly nutrition comparison is the one result in this whole report that does
   not survive the spatial correction** (naive test says highly significant; spatially-corrected
   test says p ≈ 0.12 — stable whether checked with thousands or tens of thousands of
   permutations). This is a useful concrete illustration of exactly what the spatial correction is
   for: the naive test would have called this "significant," and the corrected test correctly
   identifies it as noise — the change is small, diffuse across many disconnected hexagons rather
   than clustered, only about 70% of hexagons even changed, and there's zero net shift in the
   typical hexagon.

## 6. Methodological notes for reproducibility

- All results use the continuous capability score described in Section 2, not the production
  five-band score.
- The bootstrap confidence interval on the median difference uses 2,000 resamples at the 95%
  confidence level.
- Multiple-comparison correction (Benjamini-Hochberg) is applied across all tests run together in
  a given report.
