d <- read.csv('scenarios/public_strike/comparison_results.csv')
caps <- c('restorativeness','nutrition','care')
out <- lapply(caps, function(cap) {
  x <- d[[paste0('capability_', cap, '_baseline')]]
  y <- d[[paste0('capability_', cap, '_public_strike')]]
  diff <- y - x
  n <- sum(is.finite(diff))
  shp <- if (n >= 3 && n <= 5000) {
    shapiro.test(diff)$p.value
  } else {
    NA_real_
  }
  tt <- t.test(y, x, paired = TRUE)
  wt <- wilcox.test(y, x, paired = TRUE, exact = FALSE, conf.int = TRUE)
  data.frame(
    capability = cap,
    n = n,
    mean_baseline = mean(x, na.rm = TRUE),
    mean_public_strike = mean(y, na.rm = TRUE),
    mean_diff = mean(diff, na.rm = TRUE),
    median_diff = median(diff, na.rm = TRUE),
    t_p = tt$p.value,
    t_conf_low = tt$conf.int[1],
    t_conf_high = tt$conf.int[2],
    wilcox_p = wt$p.value,
    shapiro_p = shp,
    stringsAsFactors = FALSE
  )
})
res <- do.call(rbind, out)
print(signif(res, 6), row.names = FALSE)
