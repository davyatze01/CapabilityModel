# JVM heap: read from R5_JVM_MAX_HEAP_GB (set by Python from the cgroup budget)
# or fall back to 12G. Smaller than the old 16G so that G1GC's concurrent
# collection cycles are faster — each cycle scans less heap and the stop-the-world
# pause after an expanded_travel_time_matrix call stays under the MaxGCPauseMillis
# target instead of blocking for several minutes on a 16G heap.
r5_jvm_max_heap_gb <- Sys.getenv("R5_JVM_MAX_HEAP_GB", unset = "16")
cat(sprintf("[r5_routing] JVM heap: -Xmx%sG\n", r5_jvm_max_heap_gb))
options(java.parameters = c(
  sprintf("-Xmx%sG", r5_jvm_max_heap_gb),
  "-XX:ErrorFile=hs_err_pid%p.log",
  # Fail fast instead of struggling indefinitely. By default the JVM only throws
  # OutOfMemoryError once it truly cannot allocate — with a large heap and plenty of
  # headroom, G1GC will instead spend a very long time running increasingly expensive
  # concurrent-mark/refinement cycles trying to free enough space, which from the
  # outside is indistinguishable from a hang. These two flags make it declare defeat
  # much sooner (default GCTimeLimit=98%/GCHeapFreeLimit=2% tolerates near-total time
  # spent GCing before giving up) and then immediately exit the process instead of
  # continuing to try — restoring the old "runs out of memory -> crashes" behaviour
  # instead of the current "runs out of memory -> hangs" one. The retry loop around
  # this script already resumes from the last completed chunk on any crash.
  "-XX:+ExitOnOutOfMemoryError",
  "-XX:GCTimeLimit=90",
  "-XX:GCHeapFreeLimit=5"
))

# R library for bus routing
library(r5r)
library(data.table)

# Memory visibility around each chunk. Three independent numbers, because each one can
# lie on its own:
#   - JVM heap (Runtime.totalMemory/freeMemory): only the Java heap. System.gc() acts here.
#   - R heap (gc()): only R's own Ncells/Vcells (data.table's expanded/best-route tables).
#   - Process RSS (/proc/self/status): the OS's actual view of this process's resident
#     memory, INCLUDING off-heap/native allocations (JNI direct buffers, Arrow arenas,
#     GDAL/GEOS native memory) that neither gc() nor System.gc() can touch. If RSS keeps
#     climbing chunk over chunk while JVM-used and R-used stay flat, that proves the leak
#     is in native/off-heap memory, not something more rm()/gc() calls can fix.
# Printed before routing, right after routing (peak, before cleanup), and after
# rm()+gc() so we can see directly whether cleanup is actually reclaiming space between
# chunks, or whether usage is ratcheting up chunk over chunk.
.mem_baseline_rss_gb <- NULL

process_rss_gb <- function() {
  status_lines <- tryCatch(readLines("/proc/self/status"), error = function(e) character(0))
  rss_line <- grep("^VmRSS:", status_lines, value = TRUE)
  if (length(rss_line) == 0) return(NA_real_)
  kb <- as.numeric(gsub("[^0-9]", "", rss_line[1]))
  kb / 1024^2
}

report_memory <- function(label, track_baseline = FALSE, trigger_gc = FALSE) {
  rt <- rJava::.jcall("java/lang/Runtime", "Ljava/lang/Runtime;", "getRuntime")
  jvm_total <- rJava::.jcall(rt, "J", "totalMemory") / 1024^3
  jvm_free  <- rJava::.jcall(rt, "J", "freeMemory") / 1024^3
  jvm_max   <- rJava::.jcall(rt, "J", "maxMemory") / 1024^3
  jvm_used  <- jvm_total - jvm_free

  # gc() doesn't just report memory, it RUNS a collection (even with full=FALSE it still
  # collects gen0). Only the "after cleanup" checkpoint should trigger one — that's the
  # collection this code already intentionally did before this instrumentation existed.
  # Forcing it at "before"/"peak" too would scan live data (ettm can be 9M+ rows with
  # JNI/Arrow buffers behind it right at peak) that was never collected at those points
  # before, and risks being the actual stall itself. So skip R-heap reporting there
  # entirely — RSS (below) already reflects R's live memory without forcing anything.
  r_used_str <- "n/a (not measured, to avoid forcing GC)"
  if (trigger_gc) {
    g <- gc(verbose = FALSE)
    r_used_str <- sprintf("%.0fMB", sum(g[, 2]))  # "used" column (Mb), Ncells + Vcells rows
  }

  rss_gb <- process_rss_gb()
  rss_delta_str <- ""
  if (track_baseline && !is.na(rss_gb)) {
    if (is.null(.mem_baseline_rss_gb)) {
      .mem_baseline_rss_gb <<- rss_gb
    } else {
      rss_delta_str <- sprintf("  (%+0.2fGB vs first post-cleanup baseline)", rss_gb - .mem_baseline_rss_gb)
    }
  }

  cat(sprintf(
    "[mem] %-28s  RSS=%.2fGB%s   JVM used=%.2fGB / max=%.2fGB   R used=%s\n",
    label, rss_gb, rss_delta_str, jvm_used, jvm_max, r_used_str
  ))
}

# Paths can be injected by Python via env vars.
gtfs_path <- Sys.getenv("R5_DATA_PATH", unset = "gtfs")
origin_path <- Sys.getenv("R5_ORIGINS_PATH", unset = "outputs/r5r_origins.csv")
dest_path <- Sys.getenv("R5_DEST_PATH", unset = "outputs/r5r_dest.csv")
output_path <- Sys.getenv("R5_OUTPUT_PATH", unset = "outputs/r5r_expanded_travel_time_matrix.csv")
chunk_dir <- Sys.getenv("R5_CHUNK_DIR", unset = "outputs/r5r_chunks")
departure_dt_text <- Sys.getenv("R5_DEPARTURE_DATETIME", unset = "2025-10-15 12:00:00")
dest_chunk_size_text <- Sys.getenv("R5_DEST_CHUNK_SIZE", unset = "4000")
dest_chunk_size <- as.integer(dest_chunk_size_text)
if (is.na(dest_chunk_size) || dest_chunk_size <= 0) {
  dest_chunk_size <- 4000L
}

origins <- fread(
  origin_path,
  colClasses = list(character = "id")
)
destinations <- fread(
  dest_path,
  colClasses = list(character = "id")
)

# Use the R5 version that the installed r5r package expects by default (currently 7.5.1).
# Hard-pinning 7.4.0 fails on this r5r release: it looks for a 7.4.0 jar that doesn't match
# r5r's expected size and reports "R5 Jar file is corrupted". The matching jar is already
# cached under ~/.cache/R/r5r by the package default.
download_r5(force_update = FALSE)

gtfs_files <- list.files(gtfs_path, pattern = "\\.zip$", full.names = TRUE)
if (length(gtfs_files) == 0) {
  stop(sprintf("No GTFS zip files found in %s", gtfs_path))
}
cat(sprintf("Found %d GTFS feed(s): %s\n", length(gtfs_files), paste(basename(gtfs_files), collapse = ", ")))

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
  # Keep routing IDs as character to avoid numeric/bit64 coercion artifacts.
  ettm[, from_id := as.character(from_id)]
  ettm[, to_id := as.character(to_id)]
  # Pick the minimum-total_time row per (from_id, to_id) pair via index lookup
  # instead of sorting the full table first.  setorder on 48M rows is O(n log n)
  # and was causing multi-minute stalls after "Preparing final output... DONE!".
  best_idx <- ettm[, .I[which.min(total_time)], by = .(from_id, to_id)]$V1
  best_ettm <- ettm[best_idx]

  # Write atomically so a crash mid-write can't leave a truncated chunk that a
  # later resume would mistake for completed work.
  tmp_path <- paste0(chunk_path, ".tmp")
  fwrite(best_ettm, tmp_path)
  file.rename(tmp_path, chunk_path)

  rm(ettm, best_ettm)
  gc()

  chunk_path

}

process_origin_dest_chunk <- function(origins_chunk, destinations_chunk, chunk_label, chunk_path) {
  cat(sprintf(
    "\nProcessing %s with %d origins and %d destinations\n",
    chunk_label, nrow(origins_chunk), nrow(destinations_chunk)
  ))
  report_memory(sprintf("before routing (%s)", chunk_label))

  ettm <- expanded_travel_time_matrix(
      r5r_network = r5r_network,
      origins = origins_chunk,
      destinations = destinations_chunk,
      mode = mode,
      departure_datetime = departure_datetime,
      time_window = 60,
      breakdown = TRUE,
      max_walk_time = 30,
      max_trip_duration = max_trip_duration,
      progress = TRUE,
      verbose = FALSE
  )
  cat(sprintf(
    "Got %d expanded rows for %s; selecting fastest departure per OD pair...\n",
    nrow(ettm), chunk_label
  ))
  report_memory(sprintf("after routing, peak (%s)", chunk_label))
  ettm[, from_id := as.character(from_id)]
  ettm[, to_id := as.character(to_id)]
  # Pick the minimum-total_time row per (from_id, to_id) pair via index lookup
  # instead of sorting the full table first. setorder on tens of millions of rows
  # is O(n log n) and was silently stalling for minutes after "DONE!" with no
  # console output — this mirrors the fix already applied in process_chunk().
  best_idx <- ettm[, .I[which.min(total_time)], by = .(from_id, to_id)]$V1
  best_ettm <- ettm[best_idx]

  cat(sprintf(
    "Selected %d OD pairs for %s; writing chunk to disk...\n",
    nrow(best_ettm), chunk_label
  ))

  # Write atomically so a crash mid-write can't leave a truncated chunk that a
  # later resume would mistake for completed work.
  tmp_path <- paste0(chunk_path, ".tmp")
  fwrite(best_ettm, tmp_path)
  file.rename(tmp_path, chunk_path)

  rm(ettm, best_ettm)
  gc()
  tryCatch(rJava::.jcall("java/lang/System", "V", "gc"), error = function(e) NULL)
  report_memory(sprintf("after cleanup (%s)", chunk_label), track_baseline = TRUE, trigger_gc = TRUE)

  cat(sprintf("Chunk written: %s\n", chunk_path))

  chunk_path
}

n_origins <- nrow(origins)
if (n_origins == 0) {
  stop("No origins found.")
}

chunk_size <- 200L
n_chunks <- ceiling(n_origins / chunk_size)
destination_chunk_size <- min(dest_chunk_size, 5000L)
n_destination_chunks <- ceiling(nrow(destinations) / destination_chunk_size)

dir.create(chunk_dir, recursive = TRUE, showWarnings = FALSE)
chunk_files <- character()

# routing inputs
# Transit mode is injected by Python (R5_TRANSIT_MODE): "TRANSIT" routes all transit layers
# (single-feed cities), while "BUS"/"SUBWAY" route a single layer so each modality of a
# combined feed (e.g. IDFM) is routed separately.
transit_mode <- Sys.getenv("R5_TRANSIT_MODE", unset = "TRANSIT")
mode <- c("WALK", transit_mode)
cat(sprintf("Routing transit mode: %s\n", transit_mode))
max_trip_duration <- 60 # minutes


# departure time
departure_datetime <- as.POSIXct(
  departure_dt_text,
  format = "%Y-%m-%d %H:%M:%S"
)

# Resume support: skip chunks already computed by a previous run. No fingerprint
# check against the origin/destination CSVs or routing parameters -- a chunk
# file present in this city+transport-type's own chunk_dir (a distinct directory
# per city and per transport type, e.g. artifacts/<city>/bus/r5r_chunks) is
# treated as valid and reused as-is. This trades away protection against a
# genuinely stale chunk left over from a *different* job reusing this same
# directory (e.g. a prior run with a different chunk_size, destination set, or
# departure time) in exchange for never discarding good progress: the previous
# md5-based fingerprint invalidated (and wiped) all chunks whenever Python
# regenerated the origin/destination CSVs with any incidental difference (byte
# order, floating-point formatting, etc.) even when the job was identical --
# which is what caused a mid-job crash retry to restart from chunk 1 instead of
# resuming. If you deliberately change chunk_size, destination_chunk_size, mode,
# or the origins/destinations for a city+transport_type, clear that chunk_dir
# yourself before rerunning.
cat(sprintf("Resume: reusing any existing chunk files in %s.\n", chunk_dir))


for (chunk_index in seq_len(n_chunks)) {
  start_idx <- ((chunk_index - 1) * chunk_size) + 1
  end_idx <- min(chunk_index * chunk_size, n_origins)

  if (start_idx > n_origins) {
    next
  }

  origins_chunk <- origins[start_idx:end_idx]

  for (dest_chunk_index in seq_len(n_destination_chunks)) {
    dest_start_idx <- ((dest_chunk_index - 1) * destination_chunk_size) + 1
    dest_end_idx <- min(dest_chunk_index * destination_chunk_size, nrow(destinations))

    if (dest_start_idx > nrow(destinations)) {
      next
    }

    destinations_chunk <- destinations[dest_start_idx:dest_end_idx]
    chunk_path <- file.path(
      chunk_dir,
      sprintf("chunk_%03d_%03d.csv", chunk_index, dest_chunk_index)
    )
    if (file.exists(chunk_path) && file.info(chunk_path)$size > 0) {
      cat(sprintf(
        "Skipping completed origin chunk %d/%d, destination chunk %d/%d\n",
        chunk_index, n_chunks, dest_chunk_index, n_destination_chunks
      ))
    } else {
      process_origin_dest_chunk(
        origins_chunk,
        destinations_chunk,
        sprintf("origin chunk %d/%d, destination chunk %d/%d", chunk_index, n_chunks, dest_chunk_index, n_destination_chunks),
        chunk_path
      )
    }
    chunk_files <- c(chunk_files, chunk_path)
  }
}

all_chunks <- lapply(
  chunk_files,
  fread,
  colClasses = list(character = c("from_id", "to_id"))
)
final_ettm <- rbindlist(all_chunks)

# Defensive filtering to avoid stale/mismatched IDs propagating downstream.
valid_origin_ids <- as.character(origins$id)
valid_destination_ids <- as.character(destinations$id)
final_ettm <- final_ettm[
  from_id %in% valid_origin_ids & to_id %in% valid_destination_ids
]

if (nrow(final_ettm) == 0) {
  cat("Warning: final routing matrix is empty after ID filtering.\n")
}

fwrite(final_ettm, output_path)
