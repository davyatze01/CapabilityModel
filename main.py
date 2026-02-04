from utils import graphml, route, decay, delta_g, capabilities as cap, services as serv
import csv
import os
import osmnx as ox
import math
import matplotlib.pyplot as plt
import shutup
from tqdm import tqdm
import multiprocessing as mp
import threading
import time
import pickle
import random

# Set to None to use (cpu_count - 1)
CAP_WORKERS = None
CAP_OTP = None # None => usa tutti i thread bus disponibili
BUS_TIMEOUT_S = 15
NON_BUS_CACHE_DIR = os.path.join("cache", "non_bus")
_POI_COORDS_FILTER = None

# Silenzia warning non critici per mantenere la console pulita
shutup.please()

# Configurazione di OSMnx per ridurre l'output verboso e usare la cache interna
ox.settings.use_cache = True
ox.settings.log_console = False


def _non_bus_cache_path(node_id):
    return os.path.join(NON_BUS_CACHE_DIR, f"{node_id}.pkl")


def _write_non_bus_cache(path, payload):
    with open(path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)


def _load_non_bus_cache(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _init_worker(
    poi_coords_by_type,
    graph=None,
    mode_graphs=None,
    poi_geom_cache=None,
    poi_points_cache=None,
):
    global _POI_COORDS_FILTER
    _POI_COORDS_FILTER = {}
    for poi_type, coords in poi_coords_by_type.items():
        _POI_COORDS_FILTER[poi_type] = {
            (round(lat, 6), round(lon, 6)) for lat, lon in coords
        }
    if graph is not None and delta_g._G_CACHE is None:
        delta_g._G_CACHE = graph
    if mode_graphs:
        for mode, mode_graph in mode_graphs.items():
            if mode not in delta_g._MODE_GRAPH_CACHE:
                delta_g._MODE_GRAPH_CACHE[mode] = mode_graph
    if poi_geom_cache:
        delta_g._POI_GEOM_CACHE = poi_geom_cache
    if poi_points_cache:
        delta_g._POI_POINTS_CACHE = poi_points_cache


def empty_cache():
    for folder in ["route_cache", "rra_cache", "poi_geom_cache", NON_BUS_CACHE_DIR]:
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                os.remove(path)
    output_csv = os.path.join("outputs", "capability_to_eat.csv")
    if os.path.isfile(output_csv):
        os.remove(output_csv)


def _compute_capability_from_cache(item):
    node_id, cache_path = item
    if not os.path.exists(cache_path):
        return None
    state = _load_non_bus_cache(cache_path)

    def _compute_entry_accessibility(origin, entry):
        if entry["cache_hit"]:
            return entry["accessibility_value"]

        beta = math.log(2) / 20.0
        decay_bus = []
        for coord in entry["poi_coords"]:
            if route.bus_cache_exists(origin, coord):
                try:
                    _, _, imp_bus = route.get_route(
                        None,
                        "bus",
                        origin,
                        coord,
                        impedance_flag=True,
                        ax=None,
                        distance_only=True,
                        quiet=True,
                    )
                except Exception:
                    imp_bus = None
            else:
                imp_bus = None

            if imp_bus:
                decay_bus.append(decay.distance_decay(beta, imp_bus))
            else:
                decay_bus.append(0.0)

        RRA, acc = delta_g.merge_rra_and_accessibility(
            entry["decay_walk"],
            entry["decay_bike"],
            entry["decay_drive"],
            decay_bus,
        )
        delta_g.save_rra(entry["cache_file"], RRA)
        return acc

    dining_vals = []
    for entry in state["dining_out"]:
        dining_vals.append(_compute_entry_accessibility(state["origin"], entry))

    on_the_go_vals = []
    for entry in state["on_the_go"]:
        on_the_go_vals.append(_compute_entry_accessibility(state["origin"], entry))

    services = []
    services.append(serv.choquet_integral(dining_vals, serv.cap_dining_out))
    services.append(serv.choquet_integral(on_the_go_vals, serv.cap_on_the_go))

    # === Fase 3: Punteggio Finale ===
    # Combiniamo i due macro-servizi (Dining Out e On The Go) in un unico indice di "Capability to Eat"
    capability_to_eat = serv.choquet_integral(services, cap.cap_eat)

    return [
        node_id,
        state["origin"][0],
        state["origin"][1],
        capability_to_eat,
        services[0],
        services[1],
    ]

def _process_node(node_item):
    node_id, data = node_item
    if "y" not in data or "x" not in data:
        return None
    origin = (data["y"], data["x"])

    dining_out = []
    on_the_go = []

    for poi_type in serv.dining_out_list:
        data = delta_g.accessibility_non_bus(poi_type, origin)
        if data["cache_hit"]:
            dining_out.append({
                "poi_type": poi_type,
                "cache_hit": True,
                "cache_file": data["cache_file"],
                "accessibility_value": data["accessibility_value"],
            })
        else:
            poi_points = data["poi_points"]
            decay_walk = data["decay_walk"]
            decay_bike = data["decay_bike"]
            decay_drive = data["decay_drive"]

            if _POI_COORDS_FILTER and poi_type in _POI_COORDS_FILTER:
                keep_idx = []
                for i, geom in enumerate(poi_points):
                    key = (round(geom.y, 6), round(geom.x, 6))
                    if key in _POI_COORDS_FILTER[poi_type]:
                        keep_idx.append(i)
                poi_points = [poi_points[i] for i in keep_idx]
                decay_walk = [decay_walk[i] for i in keep_idx]
                decay_bike = [decay_bike[i] for i in keep_idx]
                decay_drive = [decay_drive[i] for i in keep_idx]

            poi_coords = [(geom.y, geom.x) for geom in poi_points]
            dining_out.append({
                "poi_type": poi_type,
                "cache_hit": False,
                "cache_file": data["cache_file"],
                "decay_walk": decay_walk,
                "decay_bike": decay_bike,
                "decay_drive": decay_drive,
                "poi_coords": poi_coords,
            })

    for poi_type in serv.on_the_go_list:
        data = delta_g.accessibility_non_bus(poi_type, origin)
        if data["cache_hit"]:
            on_the_go.append({
                "poi_type": poi_type,
                "cache_hit": True,
                "cache_file": data["cache_file"],
                "accessibility_value": data["accessibility_value"],
            })
        else:
            poi_points = data["poi_points"]
            decay_walk = data["decay_walk"]
            decay_bike = data["decay_bike"]
            decay_drive = data["decay_drive"]

            if _POI_COORDS_FILTER and poi_type in _POI_COORDS_FILTER:
                keep_idx = []
                for i, geom in enumerate(poi_points):
                    key = (round(geom.y, 6), round(geom.x, 6))
                    if key in _POI_COORDS_FILTER[poi_type]:
                        keep_idx.append(i)
                poi_points = [poi_points[i] for i in keep_idx]
                decay_walk = [decay_walk[i] for i in keep_idx]
                decay_bike = [decay_bike[i] for i in keep_idx]
                decay_drive = [decay_drive[i] for i in keep_idx]

            poi_coords = [(geom.y, geom.x) for geom in poi_points]
            on_the_go.append({
                "poi_type": poi_type,
                "cache_hit": False,
                "cache_file": data["cache_file"],
                "decay_walk": decay_walk,
                "decay_bike": decay_bike,
                "decay_drive": decay_drive,
                "poi_coords": poi_coords,
            })

    return {
        "node_id": node_id,
        "origin": origin,
        "dining_out": dining_out,
        "on_the_go": on_the_go,
    }


def _request_bus_route(graph, origin, coord, otp_semaphore):
    try:
        with otp_semaphore:
            route.get_route(
                graph,
                "bus",
                origin,
                coord,
                impedance_flag=True,
                ax=None,
                distance_only=True,
                return_geometry=False,
                quiet=True,
                timeout_s=BUS_TIMEOUT_S,
            )
    except Exception:
        pass


def _precompute_bus_routes(
    graph,
    nodes,
    poi_coords_by_type,
    otp_semaphore,
    progress_counter=None,
    progress_lock=None,
):
    for node_id, data in nodes:
        if "y" not in data or "x" not in data:
            continue
        origin = (data["y"], data["x"])
        for poi_type in serv.dining_out_list:
            for coord in poi_coords_by_type[poi_type]:
                if not route.bus_cache_exists(origin, coord):
                    _request_bus_route(graph, origin, coord, otp_semaphore)
                if progress_counter is not None and progress_lock is not None:
                    with progress_lock:
                        progress_counter[0] += 1
        for poi_type in serv.on_the_go_list:
            for coord in poi_coords_by_type[poi_type]:
                if not route.bus_cache_exists(origin, coord):
                    _request_bus_route(graph, origin, coord, otp_semaphore)
                if progress_counter is not None and progress_lock is not None:
                    with progress_lock:
                        progress_counter[0] += 1


def run_pipeline(max_nodes=None, max_pois=None, seed=42, enable_progress=True):
    graph = graphml.get_graph()
    nodes = list(graph.nodes(data=True))
    nodes_with_coords = [item for item in nodes if "y" in item[1] and "x" in item[1]]
    if max_nodes is not None:
        rng = random.Random(seed)
        nodes_with_coords = rng.sample(nodes_with_coords, min(max_nodes, len(nodes_with_coords)))

    os.makedirs("outputs", exist_ok=True)
    os.makedirs(NON_BUS_CACHE_DIR, exist_ok=True)
    output_path = os.path.join("outputs", "capability_to_eat.csv")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Intestazione del CSV
        writer.writerow([
            "node_id", "lat", "lon", "capability_to_eat", "dining_out_service", "on_the_go_service",
        ])

        # Determina il numero di worker da usare
        workers = max(1, mp.cpu_count() - 1) if CAP_WORKERS is None else max(1, int(CAP_WORKERS))
        
        # Variabili per il monitoraggio dello stato
        last_row_lock = threading.Lock()
        last_row_time = [time.time()]
        stop_event = threading.Event()
        routing_running = [False]

        pbar = None

        # Thread separato che controlla se il processo si è bloccato
        def _monitor():
            while not stop_event.wait(60):
                if pbar:
                    pbar.refresh()
                with last_row_lock:
                    idle_s = time.time() - last_row_time[0]
                if idle_s >= 300 and not routing_running[0]:
                    print("Warning: no rows written in the last 5 minutes.")

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

        try:
            poi_coords_by_type = {}
            poi_points_by_type = {}
            non_bus_poi_counts = {}
            rng = random.Random(seed)
            all_poi_types = list(dict.fromkeys(serv.dining_out_list + serv.on_the_go_list))
            # Preload POI geometries on disk to avoid parallel downloads.
            delta_g.preload_all_pois(all_poi_types)
            # Load graphs once in parent.
            shared_graph = graph
            shared_mode_graphs = {
                "walk": graphml.get_mode_graph("walk"),
                "bike": graphml.get_mode_graph("bike"),
                "drive": graphml.get_mode_graph("drive"),
            }
            shared_poi_geoms = dict(delta_g._POI_GEOM_CACHE)
            for poi_type in all_poi_types:
                poi_points = delta_g.get_poi_points(poi_type, (0.0, 0.0))
                poi_points_by_type[poi_type] = poi_points
                coords = [(geom.y, geom.x) for geom in poi_points]
                if max_pois is not None:
                    coords = rng.sample(coords, min(max_pois, len(coords)))
                poi_coords_by_type[poi_type] = coords
                non_bus_poi_counts[poi_type] = len(coords)

            total_pois = sum(len(poi_coords_by_type[poi_type]) for poi_type in serv.dining_out_list)
            total_pois += sum(len(poi_coords_by_type[poi_type]) for poi_type in serv.on_the_go_list)
            total_bus_tasks = len(nodes_with_coords) * total_pois

            bus_pbar = tqdm(total=total_bus_tasks, desc="Bus routes", mininterval=0) if enable_progress else None
            bus_progress_counter = [0] if bus_pbar else None
            bus_progress_lock = threading.Lock() if bus_pbar else None
            bus_threads_count = min(workers, len(nodes_with_coords)) if nodes_with_coords else 0
            otp_limit = bus_threads_count if CAP_OTP is None else max(1, int(CAP_OTP))
            otp_semaphore = threading.Semaphore(max(1, otp_limit))
            routing_running[0] = True
            bus_progress_thread = None

            def _flush_bus_progress():
                if not bus_pbar:
                    return
                with bus_progress_lock:
                    delta = bus_progress_counter[0]
                    bus_progress_counter[0] = 0
                if delta:
                    bus_pbar.update(delta)

            def _refresh_bus_progress():
                while routing_running[0] and not stop_event.wait(1):
                    _flush_bus_progress()
                    bus_pbar.refresh()

            if bus_pbar:
                bus_progress_thread = threading.Thread(target=_refresh_bus_progress, daemon=True)
                bus_progress_thread.start()

            bus_threads = []
            for i in range(bus_threads_count):
                shard = nodes_with_coords[i::bus_threads_count]
                if not shard:
                    continue
                bus_threads.append(
                    threading.Thread(
                        target=_precompute_bus_routes,
                        args=(
                            graph,
                            shard,
                            poi_coords_by_type,
                            otp_semaphore,
                            bus_progress_counter,
                            bus_progress_lock,
                        ),
                        daemon=True
                    )
                )
            for t in bus_threads:
                t.start()
            for t in bus_threads:
                t.join()
            routing_running[0] = False
            if bus_progress_thread:
                bus_progress_thread.join(timeout=2)
            if bus_pbar:
                _flush_bus_progress()
                bus_pbar.refresh()
                bus_pbar.close()

            cache_paths = {}
            nodes_to_compute = []
            cached_nodes = 0
            for node_id, data in nodes_with_coords:
                cache_path = _non_bus_cache_path(node_id)
                cache_paths[node_id] = cache_path
                if os.path.exists(cache_path):
                    cached_nodes += 1
                    continue
                nodes_to_compute.append((node_id, data))

            with mp.Pool(
                processes=workers,
                initializer=_init_worker,
                initargs=(
                    poi_coords_by_type,
                    shared_graph,
                    shared_mode_graphs,
                    shared_poi_geoms,
                    poi_points_by_type,
                ),
            ) as pool:
                total_non_bus_pois = sum(non_bus_poi_counts[poi_type] for poi_type in serv.dining_out_list)
                total_non_bus_pois += sum(non_bus_poi_counts[poi_type] for poi_type in serv.on_the_go_list)
                total_non_bus_tasks = len(nodes_with_coords) * total_non_bus_pois
                pbar_non_bus = tqdm(total=total_non_bus_tasks, desc="Non-bus routes", mininterval=0) if enable_progress else None
                if pbar_non_bus and cached_nodes:
                    pbar_non_bus.update(cached_nodes * total_non_bus_pois)
                for partial in pool.imap_unordered(_process_node, nodes_to_compute, chunksize=20):
                    if partial is None:
                        continue
                    cache_path = _non_bus_cache_path(partial["node_id"])
                    _write_non_bus_cache(cache_path, partial)
                    if pbar_non_bus:
                        pbar_non_bus.update(total_non_bus_pois)
                if pbar_non_bus:
                    pbar_non_bus.close()

            if enable_progress:
                pbar = tqdm(total=len(nodes_with_coords), desc="Nodes", mininterval=0)

            items = [(node_id, cache_paths[node_id]) for node_id, _ in nodes_with_coords]
            with mp.Pool(processes=workers) as pool:
                for row in pool.imap_unordered(_compute_capability_from_cache, items, chunksize=20):
                    if row is None:
                        continue
                    writer.writerow(row)
                    f.flush()
                    if pbar:
                        pbar.update(1)
                    with last_row_lock:
                        last_row_time[0] = time.time()
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            # Pulizia finale
            stop_event.set()
            monitor_thread.join(timeout=2)
            if pbar:
                pbar.close()

    print(f"Wrote results to: {output_path}")


def main():
    # empty_cache()
    run_pipeline()


if __name__ == "__main__":
    main()
