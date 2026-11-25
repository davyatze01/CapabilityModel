# Run from the repository root: Rscript tools/test_service_mean_significance.R
# Load definitions without executing the analysis against real scenario files.
expressions <- parse(file = 'analysis/significance_analysis.R')
for (expression in expressions) {
  if (is.call(expression) && identical(expression[[1]], as.name('<-')) &&
      identical(expression[[2]], as.name('all_results'))) break
  eval(expression, envir = .GlobalEnv)
}
stopifnot(SCORE_MODE == 'service_mean', .score_col_prefix == 'service_mean_')
N_BOOT <- 100
N_PERM <- 200
set.seed(1)
ci <- bootstrap_median_ci(rep(0.1, 4), c('a', 'a', 'b', 'b'))
stopifnot(isTRUE(all.equal(ci, c(0.1, 0.1))))
stopifnot(all(is.na(bootstrap_median_ci(c(0.1, 0.2), c('a', 'a')))))
# Opposite contributions within a block cancel under every block sign flip.
permutation <- block_permutation_test(c(-1, 1), c('a', 'a'), n_perm = 50)
stopifnot(permutation$p_value == 1)

original_wd <- getwd()
test_dir <- tempfile('service-mean-statistics-')
dir.create(test_dir)
setwd(test_dir)
tryCatch({
  dir.create(file.path('scenarios', 'fixture'), recursive = TRUE)
  input <- data.frame(
    hex_id = paste0('h', 1:4),
    centroid_lon = c(9.1, 9.101, 9.12, 9.121),
    centroid_lat = rep(39.2, 4),
    evaluation_method = 'service_mean',
    capability_score_mode = 'discrete'
  )
  for (capability in CAPABILITIES) {
    input[[paste0('service_mean_', capability, '_baseline')]] <- rep(0.2, 4)
    input[[paste0('service_mean_', capability, '_arm')]] <- rep(0.3, 4)
    # Conflicting ELECTRE columns must not be selected by service-mean tests.
    input[[paste0(capability, '_baseline')]] <- rep(0.9, 4)
    input[[paste0(capability, '_arm')]] <- rep(0.1, 4)
  }
  input_path <- file.path('scenarios', 'fixture', 'comparison_per_hexagon.csv')
  write.csv(input, input_path, row.names = FALSE)
  results <- process_scenario('fixture')
  stopifnot(nrow(results) == length(CAPABILITIES))
  stopifnot(all(abs(results$median_diff - 0.1) < 1e-12))
  stopifnot(all(abs(results$ci_low - 0.1) < 1e-12))
  stopifnot(all(results$n_blocks >= 2))
  input$evaluation_method <- NULL
  write.csv(input, input_path, row.names = FALSE)
  rejected <- tryCatch({process_scenario('fixture'); FALSE}, error = function(e) TRUE)
  stopifnot(rejected)
  cat('Service-mean statistical checks passed.\n')
}, finally = {
  setwd(original_wd)
  unlink(test_dir, recursive = TRUE)
})
