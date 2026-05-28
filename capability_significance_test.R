#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
input_csv <- if (length(args) >= 1) args[1] else "scenarios/public_strike/comparison_results.csv"
output_csv <- if (length(args) >= 2) args[2] else "scenarios/public_strike/capability_significance_results.csv"

if (!file.exists(input_csv)) {
  stop(sprintf("Input file not found: %s", input_csv))
}

if (!requireNamespace("DHARMa", quietly = TRUE)) {
  stop("Package 'DHARMa' is required. Install it with install.packages('DHARMa').")
}

d <- read.csv(input_csv)
caps <- c("restorativeness", "nutrition", "care")

res_list <- lapply(caps, function(cap) {
  baseline_col <- paste0("capability_", cap, "_baseline")
  scenario2_col <- paste0("capability_", cap, "_public_strike")

  if (!(baseline_col %in% names(d)) || !(scenario2_col %in% names(d))) {
    stop(sprintf("Missing expected columns for capability '%s'", cap))
  }

  x <- d[[baseline_col]]
  y <- d[[scenario2_col]]
  keep <- is.finite(x) & is.finite(y)
  x <- x[keep]
  y <- y[keep]
  diff <- y - x

  if (length(diff) < 3) {
    stop(sprintf("Not enough valid paired observations for capability '%s'", cap))
  }

  # Use DHARMa simulated residual diagnostics on an intercept-only model of paired differences.
  m <- lm(diff ~ 1)
  sim_res <- DHARMa::simulateResiduals(fittedModel = m, n = 1000, plot = FALSE)
  dharma_uniformity_p <- DHARMa::testUniformity(sim_res, plot = FALSE)$p.value
  normality_verified <- dharma_uniformity_p > 0.05

  if (normality_verified) {
    test_res <- t.test(y, x, paired = TRUE)
    selected_test <- "paired_t_test"
    selected_p <- test_res$p.value
    ci_low <- test_res$conf.int[1]
    ci_high <- test_res$conf.int[2]
  } else {
    test_res <- suppressWarnings(wilcox.test(y, x, paired = TRUE, exact = FALSE, conf.int = TRUE))
    selected_test <- "paired_wilcoxon"
    selected_p <- test_res$p.value
    ci_low <- test_res$conf.int[1]
    ci_high <- test_res$conf.int[2]
  }

  data.frame(
    capability = cap,
    n = length(diff),
    mean_baseline = mean(x),
    mean_scenario2 = mean(y),
    normality_p_value = dharma_uniformity_p,
    normality_verified_0_05 = normality_verified,
    selected_test = selected_test,
    selected_test_p_value = selected_p,
    selected_test_ci_low = ci_low,
    selected_test_ci_high = ci_high,
    significant_0_05 = selected_p < 0.05,
    stringsAsFactors = FALSE
  )
})

results <- do.call(rbind, res_list)

write.csv(results, output_csv, row.names = FALSE)
print(results)
cat(sprintf("\nSaved results to: %s\n", output_csv))
