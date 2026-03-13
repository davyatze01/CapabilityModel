# Set memory to 2 GB
options(java.parameters = "-Xmx2G") 

# R library for bus routing
library(r5r)

gtfs_path <- "gtfs"

jar_path <- download_r5(
  version = "7.4.0",
  force_update = TRUE
)

# Use r5 with this routing file
r5 <- build_network(gtfs_path)
