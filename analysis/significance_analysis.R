#!/usr/bin/env Rscript
#
# Significance analysis of scenario service averages.
# Reads service_mean_<capability>_<arm> from comparison_per_hexagon.csv.
# Capability assignment remains ELECTRE TRI; evaluation uses the equal-weight
# arithmetic mean of constituent services, aggregated to the shared hexagon grid.
# Paired differences are arm minus baseline on the 0-1 service-average scale.
# Spatial block bootstrap intervals and block sign-flip tests retain within-block
# dependence; block-size sensitivity remains necessary.
#
# Run with: Rscript analysis/significance_analysis.R
# (no command-line arguments -- edit the knobs below instead, per project convention)

# ── Knobs ───────────────────────────────────────────────────────────────────────────────
# Which scenario folders (under scenarios/<key>/comparison_per_hexagon.csv) to include. A
# scenario not yet run (e.g. new-metro before it's implemented) is skipped with a warning,
# not a hard failure, so this list can stay ahead of what's actually been generated.
SCENARIO_KEYS <- c("public-strike", "elder-student", "underservice-is-mirrionis")

# Capabilities scored by the pipeline (utils.capabilities / core.profiles.CAPABILITIES).
CAPABILITIES <- c("restorativeness", "nutrition", "care")

# Bootstrap resamples for the median-difference CI, and the CI width.
N_BOOT <- 2000
CONF_LEVEL <- 0.95
BOOT_SEED <- 1

# SCORE_MODE selects the measure, independently of capability assignment:
# "service_mean"       -- arithmetic service averages (primary evaluation).
# "service_mean_level" -- their ten display bands (secondary comparison).
# "continuous"         -- stored ELECTRE capability scores (legacy comparison).
# "level"              -- five ELECTRE display classes (legacy comparison).
SCORE_MODE <- "service_mean"

# SPATIAL_BLOCK_TEST: when TRUE, also run a spatial block sign-flip permutation test (Nichols &
# Holmes 2002-style "randomise" approach) alongside the naive paired Wilcoxon, and add its
# p-value as p_value_block_perm. Hexagons are grouped into contiguous BLOCK_SIZE_M x
# BLOCK_SIZE_M blocks by their centroid coordinates; each of the N_PERM permutations draws ONE
# random +-1 sign per block and applies it to every hexagon in that block together, instead of
# flipping each hexagon independently -- so correlated neighbours move together under the null
# exactly as they would under the alternative. This is what makes p_value_block_perm defensible
# to report/publish, unlike p_value (the naive per-hexagon Wilcoxon), which treats ~1500
# correlated hexagons as 1500 independent trials and underflows to 0 regardless of true effect
# size. Recommended report-in-the-paper columns: median_diff + ci_low/ci_high (effect size) and
# p_value_block_perm (significance) -- not the raw p_value/p_adj_bh.
SPATIAL_BLOCK_TEST <- TRUE

# Block footprint in metres. Larger = fewer, bigger, more-independent blocks (more conservative,
# lower power); smaller = closer to the naive per-hexagon test. No automatic variogram/Moran's I
# fitting is done here -- pick something in the ballpark of the spatial autocorrelation range
# for these scores (a few hundred metres for a city-scale hex grid at hexagon_radius=100m is a
# reasonable starting guess) and sanity-check n_blocks in the output isn't tiny (too
# conservative) or close to n_hexagons (barely different from the naive test).
BLOCK_SIZE_M <- 500

# Sign-flip permutations for the block-permutation p-value. The empirical p-value's resolution
# floor is 1/(N_PERM+1), so e.g. N_PERM=2000 can't report anything below ~0.0005 -- raise this if
# you need finer resolution near a significance threshold.
N_PERM <- 50000
PERM_SEED <- 1

# Where the combined results table is written.
OUT_CSV <- file.path("scenarios", "significance_results.csv")

# ── Implementation ──────────────────────────────────────────────────────────────────────

#' Percentile bootstrap CI for the median of x (paired differences).
bootstrap_median_ci <- function(
    x, block_id, n_boot = N_BOOT, conf_level = CONF_LEVEL) {

  stopifnot(length(x) == length(block_id))

  blocks <- split(x, block_id)
  n_blocks <- length(blocks)

  if (n_blocks < 2L) {
    return(c(NA_real_, NA_real_))
  }

  boot_medians <- replicate(n_boot, {
    selected <- sample.int(n_blocks, n_blocks, replace = TRUE)
    values <- unlist(blocks[selected], use.names = FALSE)
    median(values)
  })

  alpha <- 1 - conf_level
  unname(quantile(
    boot_medians,
    probs = c(alpha / 2, 1 - alpha / 2)
  ))
}

#' Assign each hexagon to a spatial block via its centroid, by snapping (lon, lat) to a local
#' metric grid of block_size_m x block_size_m cells. The degrees->metres conversion uses a
#' single equirectangular approximation around the data's mean latitude, which is accurate
#' enough at city scale (a few km across) for grouping purposes -- this is not used for any
#' actual distance measurement.
assign_spatial_blocks <- function(lon, lat, block_size_m) {
  lat0 <- mean(lat, na.rm = TRUE)
  m_per_deg_lat <- 111320
  m_per_deg_lon <- 111320 * cos(lat0 * pi / 180)
  x_m <- lon * m_per_deg_lon
  y_m <- lat * m_per_deg_lat
  bx <- floor(x_m / block_size_m)
  by <- floor(y_m / block_size_m)
  paste(bx, by, sep = "_")
}

#' Spatial block sign-flip permutation test on paired differences `diff` (ties already dropped
#' by the caller), grouped into blocks by `block_id` (one entry per element of diff).
#'
#' Statistic: the signed-rank sum W = sum(rank(|diff|) * sign(diff)) -- the same information the
#' Wilcoxon signed-rank test uses, just kept as a signed, symmetric-under-the-null statistic so
#' permuted values are directly comparable. Each permutation multiplies every hexagon's sign by
#' its block's single random +-1 draw (vectorized as a n_perm x n_blocks sign matrix expanded to
#' hexagons), giving an empirical null distribution for W. Two-sided p-value with the usual
#' +1 continuity correction so it's never reported as exactly 0.
block_permutation_test <- function(diff, block_id, n_perm = N_PERM, seed = PERM_SEED) {
  r <- rank(abs(diff))
  s0 <- sign(diff)
  w_obs <- sum(r * s0)

  blocks <- unique(block_id)
  n_blocks <- length(blocks)
  block_idx <- match(block_id, blocks)

  set.seed(seed)
  eps <- matrix(sample(c(-1, 1), n_perm * n_blocks, replace = TRUE), nrow = n_perm, ncol = n_blocks)
  eps_per_hex <- eps[, block_idx, drop = FALSE]  # n_perm x n_hexagons, block sign broadcast out
  w_perm <- as.numeric(eps_per_hex %*% (r * s0))

  p_value <- (1 + sum(abs(w_perm) >= abs(w_obs))) / (n_perm + 1)
  list(p_value = p_value, n_blocks = n_blocks)
}

#' Paired Wilcoxon signed-rank test + bootstrap CI (+ optional spatial block permutation test)
#' between two groups' scores at the same capability (group_b - group_a). Used both for
#' baseline-vs-arm and, for elder-student, the extra student-vs-elderly comparison.
test_one <- function(scenario_key, capability, group_a, group_b, level_a, level_b,
                      centroid_lon = NULL, centroid_lat = NULL) {
  keep <- is.finite(level_a) & is.finite(level_b)
  a_v <- level_a[keep]
  b_v <- level_b[keep]
  diff <- b_v - a_v
  n <- length(diff)
  n_changed <- sum(diff != 0)

  if (n < 2 || n_changed == 0) {
    return(data.frame(
      scenario = scenario_key, capability = capability, group_a = group_a, group_b = group_b,
      n_hexagons = n, n_changed = n_changed, pct_changed = ifelse(n > 0, 100 * n_changed / n, NA),
      median_diff = ifelse(n > 0, median(diff), NA), ci_low = NA, ci_high = NA,
      p_value = NA, p_value_block_perm = NA, n_blocks = NA,
      note = "insufficient variation for a test",
      stringsAsFactors = FALSE
    ))
  }

  wt <- suppressWarnings(wilcox.test(b_v, a_v, paired = TRUE, exact = FALSE))

  ci <- c(NA_real_, NA_real_)
  p_value_block_perm <- NA_real_
  n_blocks <- NA_integer_

  has_coordinates <- (
    !is.null(centroid_lon) &&
    !is.null(centroid_lat)
  )

  if (has_coordinates) {
    lon_k <- centroid_lon[keep]
    lat_k <- centroid_lat[keep]

    has_coordinates <- (
      all(is.finite(lon_k)) &&
      all(is.finite(lat_k))
    )
  }

  if (has_coordinates) {
    # All paired differences, including zeros, enter the bootstrap.
    block_id <- assign_spatial_blocks(lon_k, lat_k, BLOCK_SIZE_M)

    set.seed(BOOT_SEED)
    ci <- bootstrap_median_ci(diff, block_id)

    if (SPATIAL_BLOCK_TEST) {
      # The signed-rank test excludes zero differences.
      nz <- diff != 0
      perm <- block_permutation_test(diff[nz], block_id[nz])
      p_value_block_perm <- perm$p_value
      n_blocks <- perm$n_blocks
    }
  } else {
    warning("Missing or invalid centroids: spatial confidence interval unavailable.")
  }
  data.frame(
    scenario = scenario_key, capability = capability, group_a = group_a, group_b = group_b,
    n_hexagons = n, n_changed = n_changed, pct_changed = 100 * n_changed / n,
    median_diff = median(diff), ci_low = ci[1], ci_high = ci[2],
    p_value = wt$p.value, p_value_block_perm = p_value_block_perm, n_blocks = n_blocks,
    note = "",
    stringsAsFactors = FALSE
  )
}

# Explicit prefixes prevent service-average tests from consuming ELECTRE scores.
.score_col_prefix <- switch(
  SCORE_MODE,
  service_mean = "service_mean_",
  service_mean_level = "service_mean_level_",
  continuous = "",
  level = "level_",
  stop(sprintf("Unknown SCORE_MODE: %s", SCORE_MODE))
)

#' Run every capability x non-baseline-arm test found in one scenario's comparison CSV.
process_scenario <- function(scenario_key) {
  csv_path <- file.path("scenarios", scenario_key, "comparison_per_hexagon.csv")
  if (!file.exists(csv_path)) {
    warning(sprintf("[%s] skipped: %s not found (run analysis/scenarios.py for it first)",
                     scenario_key, csv_path))
    return(NULL)
  }

  df <- read.csv(csv_path, stringsAsFactors = FALSE)
  if (SCORE_MODE %in% c("service_mean", "service_mean_level")) {
    if (!"evaluation_method" %in% names(df) ||
        anyNA(df$evaluation_method) ||
        !all(df$evaluation_method == "service_mean")) {
      stop(sprintf("[%s] regenerate comparison CSVs with separate service-average outputs", scenario_key))
    }
    required <- paste0(.score_col_prefix, CAPABILITIES, "_baseline")
    if (!all(required %in% names(df))) {
      stop(sprintf("[%s] missing service-average baseline columns; regenerate scenario exports", scenario_key))
    }
  }
  has_centroids <- all(c("centroid_lon", "centroid_lat") %in% names(df))
  if (SPATIAL_BLOCK_TEST && !has_centroids) {
    warning(sprintf("[%s] centroid_lon/centroid_lat not found; skipping spatial block test for this scenario",
                     scenario_key))
  }
  lon <- if (has_centroids) df$centroid_lon else NULL
  lat <- if (has_centroids) df$centroid_lat else NULL

  results <- list()

  for (capability in CAPABILITIES) {
    baseline_col <- paste0(.score_col_prefix, capability, "_baseline")
    if (!baseline_col %in% names(df)) next

    score_cols <- grep(paste0("^", .score_col_prefix, capability, "_"), names(df), value = TRUE)
    arms <- sub(paste0("^", .score_col_prefix, capability, "_"), "", score_cols)
    arms <- setdiff(arms, "baseline")

    for (arm in arms) {
      arm_col <- paste0(.score_col_prefix, capability, "_", arm)
      res <- test_one(scenario_key, capability, "baseline", arm, df[[baseline_col]], df[[arm_col]],
                       centroid_lon = lon, centroid_lat = lat)
      results[[length(results) + 1]] <- res
    }

    # elder-student has a third arm (student and elderly are both non-baseline personas) --
    # also compare them directly to each other, not just each against baseline.
    if (scenario_key == "elder-student" && all(c("student", "elderly") %in% arms)) {
      student_col <- paste0(.score_col_prefix, capability, "_student")
      elderly_col <- paste0(.score_col_prefix, capability, "_elderly")
      res <- test_one(scenario_key, capability, "student", "elderly", df[[student_col]], df[[elderly_col]],
                       centroid_lon = lon, centroid_lat = lat)
      results[[length(results) + 1]] <- res
    }
  }

  if (length(results) == 0) {
    warning(sprintf("[%s] no %s<capability>_<arm> columns found in %s",
                     scenario_key, .score_col_prefix, csv_path))
    return(NULL)
  }

  do.call(rbind, results)
}

all_results <- do.call(rbind, lapply(SCENARIO_KEYS, process_scenario))

if (is.null(all_results) || nrow(all_results) == 0) {
  stop("No scenario comparison CSVs found. Run analysis/scenarios.py for at least one SCENARIO first.")
}

# Benjamini-Hochberg adjustment across all tests run in this pass, since we're testing 3
# capabilities x several arms at once and an unadjusted p-value inflates the false-positive rate.
# Applied to both the naive and block-permutation p-values.
all_results$p_adj_bh <- p.adjust(all_results$p_value, method = "BH")
all_results$p_adj_bh_block_perm <- p.adjust(all_results$p_value_block_perm, method = "BH")
all_results$score_mode <- SCORE_MODE
all_results$aggregation_method <- if (SCORE_MODE %in% c("service_mean", "service_mean_level")) "arithmetic_service_mean" else "electre_tri"
all_results$ci_method <- "spatial_block_bootstrap"
all_results$ci_block_size_m <- BLOCK_SIZE_M
all_results$block_size_m <- if (SPATIAL_BLOCK_TEST) BLOCK_SIZE_M else NA

all_results <- all_results[order(all_results$scenario, all_results$capability, all_results$group_a, all_results$group_b), ]

dir.create(dirname(OUT_CSV), recursive = TRUE, showWarnings = FALSE)
write.csv(all_results, OUT_CSV, row.names = FALSE)

cat("\n=== Significance analysis: baseline vs. each scenario arm ===\n")
cat(sprintf("SCORE_MODE = %s.  Paired Wilcoxon signed-rank test, %d%% bootstrap CI on median shift.\n",
            SCORE_MODE, as.integer(100 * CONF_LEVEL)))
if (SCORE_MODE == "service_mean") {
  cat("Effect sizes are scenario-minus-reference arithmetic service averages on the 0-1 scale.\n")
}
if (SPATIAL_BLOCK_TEST) {
  cat(sprintf("Spatial block sign-flip permutation test: BLOCK_SIZE_M=%d, N_PERM=%d.\n",
              BLOCK_SIZE_M, N_PERM))
  cat("p_value is the naive per-hexagon test (NOT spatially valid -- treats correlated hexagons\n")
  cat("as independent). Report p_value_block_perm (and median_diff + ci_low/ci_high for effect\n")
  cat("size) instead.\n")
}
cat("\n")
print(all_results, row.names = FALSE, digits = 4)
cat(sprintf("\n[Output] Saved: %s\n", OUT_CSV))
