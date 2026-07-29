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
  r_used_str <- ""  # only reported at the post-cleanup checkpoint that actually runs gc()
  if (trigger_gc) {
    g <- gc(verbose = FALSE)
    r_used_str <- sprintf("   R used=%.0fMB", sum(g[, 2]))  # "used" column (Mb), Ncells + Vcells rows
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
    "[mem] %-28s  RSS=%.2fGB%s   JVM used=%.2fGB / max=%.2fGB%s\n",
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
dest_chunk_size_text <- Sys.getenv("R5_DEST_CHUNK_SIZE", unset = "0")
dest_chunk_size <- as.integer(dest_chunk_size_text)
# 0 / invalid => resolved to "all destinations in one chunk" once nrow is known.
if (is.na(dest_chunk_size) || dest_chunk_size <= 0) {
  dest_chunk_size <- 0L
}
origin_chunk_size_text <- Sys.getenv("R5_ORIGIN_CHUNK_SIZE", unset = "0")
origin_chunk_size <- as.integer(origin_chunk_size_text)
# 0 / invalid => resolved to a safe default (1 origin) once nrow is known.
if (is.na(origin_chunk_size) || origin_chunk_size <= 0) {
  origin_chunk_size <- 0L
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
      # r5r's default (5) draws multiple random headway realizations per departure
      # minute -- meant for frequency-based GTFS (frequencies.txt). Neither GTFS feed
      # this pipeline routes against uses frequencies.txt (both ARST/Cagliari and
      # IDFM/Paris are fully stop_times-scheduled), so per-minute draws beyond the
      # first are redundant resampling of a deterministic schedule. 1 draw keeps
      # time_window's 60 distinct departure minutes exactly as before, just without
      # the ~5x redundant resampling on top.
      draws_per_minute = 1,
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
      # See the matching comment in process_chunk(): neither GTFS feed this pipeline
      # uses is frequency-based, so the extra per-minute Monte Carlo draws r5r's
      # default (5) would add are redundant resampling of a deterministic schedule.
      draws_per_minute = 1,
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

# Origin-primary chunking: keep destinations in as few large chunk(s) as R5 allows
# and chunk origins instead. R5's per-origin transit search (RAPTOR) dominates cost
# and is ~independent of the destination count, so many small destination chunks
# re-run it redundantly (once per chunk). Sizes are injected by Python from the
# memory budget (R5_ORIGIN_CHUNK_SIZE / R5_DEST_CHUNK_SIZE); the fallbacks keep the
# script runnable standalone.
#
# HARD CAP: expanded_travel_time_matrix(breakdown = TRUE) computes detailed path
# breakdowns via R5's PathResult, which throws
#   "Number of detailed path destinations exceeds limit of 5000"
# for any call with more than 5000 destinations. The breakdown columns
# (access_time / wait_time / ride_time / transfer_time / routes / n_rides) are
# required by the downstream generalized-cost model, so we cannot drop breakdown to
# lift this limit. The cap below is therefore mandatory, not tuning: it caps at 5000
# regardless of the injected R5_DEST_CHUNK_SIZE. Small cities (e.g. Cagliari) stay in
# one chunk; large ones (Paris' 268k destinations => 54 chunks) pay the unavoidable
# RAPTOR re-run cost that breakdown = TRUE forces.
R5_MAX_BREAKDOWN_DESTINATIONS <- 5000L
if (origin_chunk_size <= 0L) {
  origin_chunk_size <- 1L
}
chunk_size <- origin_chunk_size
n_chunks <- ceiling(n_origins / chunk_size)
if (dest_chunk_size <= 0L) {
  destination_chunk_size <- nrow(destinations)
} else {
  destination_chunk_size <- min(dest_chunk_size, nrow(destinations))
}
if (destination_chunk_size <= 0L) {
  destination_chunk_size <- nrow(destinations)
}
destination_chunk_size <- min(destination_chunk_size, R5_MAX_BREAKDOWN_DESTINATIONS)
n_destination_chunks <- ceiling(nrow(destinations) / destination_chunk_size)
cat(sprintf(
  "Chunking: %d origins in %d chunk(s) of %d; %d destinations in %d chunk(s) of %d.\n",
  n_origins, n_chunks, chunk_size,
  nrow(destinations), n_destination_chunks, destination_chunk_size
))

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

# --- Stale-cache guard -------------------------------------------------------
# The resume logic below reuses any chunk_*.csv present by name. That is only
# valid when the inputs and chunk grid match the ones those files were written
# under. If the origins/destinations, departure, transit mode, time window, or
# chunk sizes changed, old chunks encode incompatible results -- e.g. a "d{idx}"
# label now points at a different coordinate, or the (origin_chunk, dest_chunk)
# grid no longer lines up -- and must not be silently blended into the matrix.
#
# The fingerprint is over ROUNDED coordinates (6 dp, ~0.1 m) plus the routing
# parameters, NOT the raw CSV bytes. This is deliberate: an earlier md5-of-file
# guard was removed because Python re-emitting the same coordinates with
# incidental float-formatting/byte-order differences changed the hash and wiped
# a perfectly valid cache (restarting a job from chunk 1). Rounded coordinates
# are stable across that reformatting, while still changing on any real change of
# the coordinates, their order, or the grid.
rounded_coord_md5 <- function(dt) {
  tf <- tempfile()
  on.exit(unlink(tf), add = TRUE)
  writeLines(sprintf("%.6f,%.6f", dt$lat, dt$lon), tf)
  unname(tools::md5sum(tf))
}
current_signature <- paste(
  rounded_coord_md5(origins),
  rounded_coord_md5(destinations),
  departure_dt_text,
  paste(mode, collapse = "+"),
  60,                       # time_window (minutes)
  max_trip_duration,
  chunk_size,
  destination_chunk_size,
  sep = "|"
)
signature_path <- file.path(chunk_dir, "input_signature.txt")
previous_signature <- if (file.exists(signature_path)) {
  readLines(signature_path, n = 1, warn = FALSE)
} else {
  ""
}
if (!identical(previous_signature, current_signature)) {
  stale <- list.files(chunk_dir, pattern = "^chunk_.*\\.csv$", full.names = TRUE)
  if (length(stale) > 0) {
    cat(sprintf(
      "Input signature changed since last run: clearing %d stale chunk file(s) in %s.\n",
      length(stale), chunk_dir
    ))
    file.remove(stale)
  }
  writeLines(current_signature, signature_path)
} else {
  cat("Input signature unchanged: reusing existing chunk files where present.\n")
}

# Resume support: skip chunks already computed by a previous run. Within a run
# whose inputs match the signature above, a chunk file present in this
# city+transport-type's own chunk_dir (a distinct directory per city and per
# transport type, e.g. artifacts/<city>/bus/r5r_chunks) is treated as valid and
# reused as-is. Correctness against a genuinely stale chunk (a different
# destination set, chunk grid, departure, or mode) is now handled up front by
# the signature guard, which wipes the chunk_dir on mismatch -- so a changed job
# can no longer silently blend incompatible rows into the matrix, while an
# identical job (including one whose CSVs were merely re-emitted with different
# float formatting) still resumes instead of restarting from chunk 1.
#
# draws_per_minute is deliberately NOT on that list: for a fully stop_times-scheduled
# GTFS feed (no frequencies.txt -- true of every feed this pipeline currently routes
# against), the best-of-N-draws-per-minute value should be the same regardless of N,
# so chunks already computed under a different draws_per_minute are still valid to
# reuse as-is alongside newly-computed ones.
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

# Streaming combine: one chunk file at a time -> filter -> append to the output.
# The previous version loaded every chunk file at once (17GB+ of CSV for Paris),
# rbindlist'd them, then filtered -- up to three simultaneous copies of the full
# matrix on top of the live R5 JVM heap, which pinned the run at the cgroup
# memory cap and reclaim-throttled it into what looked like a hang. Peak memory
# is now a single chunk. The periodic progress lines both feed the Python stall
# watchdog and mark the combine phase so it gets its own generous timeout
# (public_transport_routing_stage.py) instead of the tight 60s routing one.
# Written to a .part file first: a kill mid-combine must not leave a truncated
# file that a later run could mistake for the finished matrix.
valid_origin_ids <- as.character(origins$id)
valid_destination_ids <- as.character(destinations$id)

part_path <- paste0(output_path, ".part")
if (file.exists(part_path)) {
  file.remove(part_path)
}
n_chunk_files <- length(chunk_files)
total_rows_kept <- 0
cat(sprintf("Combining chunk files: 0/%d\n", n_chunk_files))
for (i in seq_along(chunk_files)) {
  chunk <- fread(
    chunk_files[[i]],
    colClasses = list(character = c("from_id", "to_id"))
  )
  # Defensive filtering to avoid stale/mismatched IDs propagating downstream.
  chunk <- chunk[from_id %in% valid_origin_ids & to_id %in% valid_destination_ids]
  total_rows_kept <- total_rows_kept + nrow(chunk)
  fwrite(chunk, part_path, append = file.exists(part_path))
  if (i %% 200 == 0 || i == n_chunk_files) {
    cat(sprintf(
      "Combining chunk files: %d/%d (%.0f rows kept)\n",
      i, n_chunk_files, total_rows_kept
    ))
  }
}

if (total_rows_kept == 0) {
  cat("Warning: final routing matrix is empty after ID filtering.\n")
}
file.rename(part_path, output_path)
