# Set memory to 16 GB
options(java.parameters = "-Xmx16G")

# R library for bus routing
library(r5r)
library(data.table)

# paths of the gtfs folder, and the two csvs with origin and destination routes
gtfs_path <- "gtfs"
origin_path <- "outputs/r5r_origins.csv"
dest_path <- "outputs/r5r_dest.csv"
output_path <- "outputs/r5r_expanded_travel_time_matrix.csv"

origins <- fread(origin_path)
destinations <- fread(dest_path)

download_r5(version = "7.4.0", force_update = FALSE)

# Use r5 with this routing file
r5r_network <- build_network(gtfs_path)



process_chunk <- function(origins_chunk, chunk_index, n_chunks, chunk_path) {
  cat(sprintf(
    "\nProcessing chunk %d out of %d with %d origins \n",
    chunk_index, n_chunks, nrow(origins_chunk)
  ))

  ettm <- expanded_travel_time_matrix(
      r5r_network = r5r_network,
      origins = origins_chunk,
      destinations = destinations,
      mode = mode,
      departure_datetime = departure_datetime,
      time_window = 60,
      breakdown = TRUE,
      max_walk_time = 30,
      max_trip_duration = max_trip_duration,
      progress = TRUE,
      verbose = FALSE
  )
  setorder(ettm, from_id, to_id, total_time, wait_time, departure_time)
  best_ettm <- ettm[, .SD[1], by = .(from_id, to_id)]

  fwrite(best_ettm, chunk_path)

  rm(ettm, best_ettm)
  gc()

  chunk_path

}

n_origins <- nrow(origins)
if (n_origins == 0) {
  stop("No origins found.")
}

chunk_size <- 200L
n_chunks <- ceiling(n_origins / chunk_size)
chunk_dir <- "outputs/r5r_chunks"

dir.create(chunk_dir, recursive = TRUE, showWarnings = FALSE)
chunk_files <- character()

# routing inputs
mode <- c("WALK", "TRANSIT")
max_trip_duration <- 60 # minutes


# departure time
departure_datetime <- as.POSIXct("15-10-2025 12:00:00",
                                 format = "%d-%m-%Y %H:%M:%S")


for (chunk_index in seq_len(n_chunks)) {
  start_idx <- ((chunk_index - 1) * chunk_size) + 1
  end_idx <- min(chunk_index * chunk_size, n_origins)

  if (start_idx > n_origins) {
    next
  }
  chunk_path <- file.path(chunk_dir, sprintf("chunk_%03d.csv", chunk_index))
  if (file.exists(chunk_path)) {
    cat(sprintf("Skipping chunk %d, already present in memory\n", chunk_index))
    chunk_files <- c(chunk_files, chunk_path)
    next
  }

  origins_chunk <- origins[start_idx:end_idx]
  chunk_path <- process_chunk(origins_chunk, chunk_index, n_chunks, chunk_path)
  chunk_files <- c(chunk_files, chunk_path)
}

all_chunks <- lapply(chunk_files, fread)
final_ettm <- rbindlist(all_chunks)

fwrite(final_ettm, output_path)
