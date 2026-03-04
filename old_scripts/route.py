import os
import json
import pickle
import hashlib
import time
from typing import cast
import requests
import threading
import polyline
import osmnx as ox
import networkx as nx
from datetime import datetime
import matplotlib.pyplot as plt
from osmnx.routing import route_to_gdf
from requests.adapters import HTTPAdapter

from utils import get_impedance, graphml


# =========================
# Cache grafi (RAM + Disco)
# =========================
_GRAPH_CACHE = {}  # cache in memoria per il grafo osm
_MODE_GRAPH_CACHE = {}  # cache in memoria per grafi full-area per modalitÃ 
_ROUTE_CACHE = {}  # cache in memoria per distanze/impeance
_ROUTE_GEOM_CACHE = {}  # cache in memoria per geometrie
_ROUTE_CACHE_FOLDER = "route_cache"
_ROUTE_COORD_ROUND = 6  # arrotondamento coordinate per chiave cache
_HTTP_LOCAL = threading.local()
_GRAPH_BOUNDS_CACHE = {}  # cache bounds per grafo (id -> (min_lat, max_lat, min_lon, max_lon))
_ROUTE_CACHE_LOCKS = {}
_ROUTE_CACHE_LOCKS_GUARD = threading.Lock()

# Data/ora per pianificazione (OTP)
ROUTE_DATE = "2025-11-12"   # YYYY-MM-DD
ROUTE_TIME = "19:30:00"     # hh:mm:ss


def _round_coord(coord):
    # arrotonda coordinate per occupare meno spazio in memoria
    return (round(coord[0], _ROUTE_COORD_ROUND), round(coord[1], _ROUTE_COORD_ROUND))


def _route_cache_key(
    network_type,
    origin,
    destination,
    route_date=None,
    route_time=None,
    dist=None,
    center_point=None,
):
    # chiave minima per identificare un routing univoco -> questo è l'indice utilizzato in cache per recuperare i dati relativi a un routing già eseguito
    origin_key = _round_coord(origin)
    dest_key = _round_coord(destination)
    center_key = _round_coord(center_point) if center_point else None
    return (
        network_type,
        origin_key,
        dest_key,
        route_date,
        route_time,
        dist,
        center_key,
    )


def _route_cache_path(key, suffix):
    # crea il path su disco per il file che conterrà il routing. Il suffisso differenzia tra distanza ("dist") e geometria ("geom")
    os.makedirs(_ROUTE_CACHE_FOLDER, exist_ok=True)
    key_bytes = pickle.dumps(key)
    key_hash = hashlib.sha1(key_bytes).hexdigest() # hash per evitare nomi file troppo lunghi
    return os.path.join(_ROUTE_CACHE_FOLDER, f"{key_hash}.{suffix}.pkl") #ad esempio: route_cache/abc123.dist.pkl


def _route_cache_source_key(route_key):
    # bucket per sorgente/modalita/tempo: una entry per ogni destinazione
    return (
        route_key[0],  # network_type
        route_key[1],  # origin_key
        route_key[3],  # route_date
        route_key[4],  # route_time
        route_key[5],  # dist
        route_key[6],  # center_key
    )


def _route_cache_dest_key(route_key):
    return route_key[2]  # destination


def _route_cache_bucket_path(source_key):
    os.makedirs(_ROUTE_CACHE_FOLDER, exist_ok=True)
    key_bytes = pickle.dumps(source_key)
    key_hash = hashlib.sha1(key_bytes).hexdigest()
    return os.path.join(_ROUTE_CACHE_FOLDER, f"{key_hash}.dist.pkl")


def _route_cache_source_lock(source_key):
    with _ROUTE_CACHE_LOCKS_GUARD:
        lock = _ROUTE_CACHE_LOCKS.get(source_key)
        if lock is None:
            lock = threading.Lock()
            _ROUTE_CACHE_LOCKS[source_key] = lock
        return lock


def _route_cache_bucket_load(source_key):
    if source_key in _ROUTE_CACHE:
        bucket = _ROUTE_CACHE[source_key]
        if isinstance(bucket, dict):
            return bucket
    path = _route_cache_bucket_path(source_key)
    if not os.path.exists(path):
        bucket = {}
        _ROUTE_CACHE[source_key] = bucket
        return bucket
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict):
            _ROUTE_CACHE[source_key] = data
            return data
    except Exception:
        pass
    bucket = {}
    _ROUTE_CACHE[source_key] = bucket
    return bucket


def _atomic_pickle_write(path, value, retries=5, sleep_s=0.05):
    # Windows can raise PermissionError on os.replace if another handle is active.
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    for attempt in range(retries):
        try:
            with open(tmp_path, "wb") as f:
                pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt >= retries - 1:
                raise
            time.sleep(sleep_s * (attempt + 1))
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


def _route_cache_get(key):
    # cerca se il dato è già stato caricato e quindi si trova nel dictionary di cache in RAM, oppure sul file corrispondente su disco
    source_key = _route_cache_source_key(key)
    dest_key = _route_cache_dest_key(key)
    lock = _route_cache_source_lock(source_key)
    with lock:
        bucket = _route_cache_bucket_load(source_key)
        if dest_key in bucket:
            return bucket[dest_key]

    legacy_path = _route_cache_path(key, "dist")
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None
    return None


def _route_cache_set(key, value):
    # salva in cache esclusivamente il valore di distanza
    source_key = _route_cache_source_key(key)
    dest_key = _route_cache_dest_key(key)
    lock = _route_cache_source_lock(source_key)
    with lock:
        bucket = dict(_route_cache_bucket_load(source_key))
        bucket[dest_key] = value
        _ROUTE_CACHE[source_key] = bucket
        path = _route_cache_bucket_path(source_key)
        _atomic_pickle_write(path, bucket)


def _route_geom_cache_get(key):
    # Cerca se la geometria del percorso breve (solo bus) è gia in cache (RAM) oppure la carica sul file corrispondente su disco.
    if key in _ROUTE_GEOM_CACHE:
        return _ROUTE_GEOM_CACHE[key]
    path = _route_cache_path(key, "geom")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            _ROUTE_GEOM_CACHE[key] = data
            return data
        except Exception:
            return None
    return None


def _route_geom_cache_set(key, value):
    # Quando viene eseguito un bus routing, la geometria viene salvata in RAM e su disco (scrittura atomica)
    _ROUTE_GEOM_CACHE[key] = value
    path = _route_cache_path(key, "geom")
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def bus_cache_exists(origin, destination, route_date=None, route_time=None):
    # verifica se cache bus distanza è già disponibile
    cache_key = _route_cache_key(
        "bus",
        origin,
        destination,
        route_date=route_date or ROUTE_DATE,
        route_time=route_time or ROUTE_TIME,
    )
    return _route_cache_get(cache_key) is not None


def bus_distance_cache_exists(origin, destination, route_date=None, route_time=None):
    # distance/impedance cache only (ignore geometry)
    cache_key = _route_cache_key(
        "bus",
        origin,
        destination,
        route_date=route_date or ROUTE_DATE,
        route_time=route_time or ROUTE_TIME,
    )
    return _route_cache_get(cache_key) is not None


def _cached_distance_value(cached):
    # compatibilità retroattiva: vecchi cache dict e nuovo cache numerico
    if isinstance(cached, (int, float)):
        return float(cached)
    if isinstance(cached, dict):
        if isinstance(cached.get("distance_m"), (int, float)):
            return float(cached["distance_m"])
        legacy = cached.get("result_value")
        if isinstance(legacy, (int, float)):
            return float(legacy)
    return None


def _cached_waiting_min_value(cached):
    if isinstance(cached, dict) and isinstance(cached.get("waiting_min"), (int, float)):
        return float(cached["waiting_min"])
    return None


def _bus_distance_to_impedance_minutes(distance_m):
    # fallback semplice quando dal cache è disponibile solo la distanza
    BUS_SPEED_KMH = 10.0
    return (distance_m / 1000.0 / BUS_SPEED_KMH) * 60.0


def _normalize_node_id(node):
    # Convert numpy scalars returned by nearest_nodes to plain hashable ids.
    try:
        return node.item()
    except Exception:
        return node


def _format_time(timestamp_ms):
    """OTP ritorna epoch in millisecondi."""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")


def _get_mode_graph(network_type):
    # carica un grafo full-area per modalita e salva in RAM
    if network_type not in _MODE_GRAPH_CACHE:
        _MODE_GRAPH_CACHE[network_type] = graphml.get_mode_graph(network_type)
    return _MODE_GRAPH_CACHE[network_type]


def _get_http_session():
    # una Session per thread: mantiene connessioni keep-alive senza condivisione cross-thread
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _HTTP_LOCAL.session = session
    return session


def _ensure_ax(ax):
    if ax is None:
        fig, ax = plt.subplots(figsize=(8, 8))
        return fig, ax
    return ax.figure, ax


def _get_graph_bounds(graph):
    if graph is None:
        return None
    key = id(graph)
    if key in _GRAPH_BOUNDS_CACHE:
        return _GRAPH_BOUNDS_CACHE[key]
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    for _, data in graph.nodes(data=True):
        if "y" not in data or "x" not in data:
            continue
        lat = data["y"]
        lon = data["x"]
        if lat < min_lat:
            min_lat = lat
        if lat > max_lat:
            max_lat = lat
        if lon < min_lon:
            min_lon = lon
        if lon > max_lon:
            max_lon = lon
    if min_lat == float("inf"):
        bounds = None
    else:
        bounds = (min_lat, max_lat, min_lon, max_lon)
    _GRAPH_BOUNDS_CACHE[key] = bounds
    return bounds


def coord_in_graph_bounds(graph, coord):
    bounds = _get_graph_bounds(graph)
    if bounds is None:
        return True
    lat, lon = coord
    return bounds[0] <= lat <= bounds[1] and bounds[2] <= lon <= bounds[3]



def get_route(
    graph,
    network_type,
    origin,
    destination,
    impedance_flag=False,
    ax=None,
    distance_only=False,
    return_geometry=False,
    quiet=False,
    timeout_s=60,
    otp_url=None,
    save_geometry=True,
    return_status=False,
):
    """
    origin/destination: tuple (lat, lon)

    network_type:
      - 'bus' usa OTP2
      - 'walk' / 'drive' / 'bike' usa OSMnx + NetworkX (con cache per non riscaricare)

    distance_only:
      - True: non disegna nulla, ritorna solo impedenza/distanza

    return_geometry:
      - True: ritorna anche la geometria della strada (gdf) quando disponibile
    """

    # URL del server OTP2
    OTP_URL = otp_url or "http://localhost:8080/otp/routers/default/index/graphql"

    fig = None
    if not distance_only:
        # Se ax non è creato lo creo
        fig, ax = _ensure_ax(ax)

    def _build_status(ok, found_itinerary, error_type=None, error_detail=None, status_code=None):
        return {
            "ok": bool(ok),
            "found_itinerary": bool(found_itinerary),
            "error_type": error_type,
            "error_detail": error_detail,
            "status_code": status_code,
        }

    def _finalize(result_value, geom=None, status=None):
        if return_status:
            status_payload = status or _build_status(True, bool(result_value))
            if return_geometry:
                return fig, ax, result_value, geom, status_payload
            return fig, ax, result_value, status_payload
        if return_geometry:
            return fig, ax, result_value, geom
        return fig, ax, result_value

    # ==========================
    # CASO BUS (OTP2)
    # ==========================
    if network_type == "bus":
        # cache solo per richieste bus distance_only
        cache_key = _route_cache_key(
            network_type,
            origin,
            destination,
            route_date=ROUTE_DATE,
            route_time=ROUTE_TIME,
        )
        cached = _route_cache_get(cache_key)
        if cached is not None and distance_only:
            cached_distance = _cached_distance_value(cached)
            if cached_distance is None:
                cached_value = None
            elif impedance_flag:
                cached_waiting_min = _cached_waiting_min_value(cached)
                if cached_waiting_min is None:
                    cached_value = _bus_distance_to_impedance_minutes(cached_distance)
                else:
                    cached_value = get_impedance.impedance_bus(cached_distance / 1000.0, cached_waiting_min)
            else:
                cached_value = cached_distance
            return _finalize(
                cached_value,
                geom=None,
                status=_build_status(True, bool(cached_value), error_type=None, error_detail=None, status_code=200),
            )

        query = f"""
        query {{
          plan(
            from: {{ lat: {origin[0]}, lon: {origin[1]} }}
            to: {{ lat: {destination[0]}, lon: {destination[1]} }}
            date: "{ROUTE_DATE}"
            time: "{ROUTE_TIME}"
            transportModes: [
              {{ mode: WALK }}
              {{ mode: BUS }}
            ]
            walkReluctance: 2.0
            walkSpeed: 1.3
            numItineraries: 3
            boardSlack: 180
          ) {{
            itineraries {{
              duration
              walkDistance
              legs {{
                mode
                startTime
                endTime
                from {{ name lat lon }}
                to   {{ name lat lon }}
                route {{ shortName longName }}
                distance
                {"" if distance_only and not save_geometry else "legGeometry { points }"}
              }}
            }}
          }}
        }}
        """

        try:
            resp = _get_http_session().post(
                OTP_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"query": query}),
                timeout=timeout_s
            )
        except Exception as e:
            print("Non hai avviato correttamente OpenTripPlanner oppure OTP non è raggiungibile.")
            print("Dettaglio:", e)
            return _finalize(
                ([] if impedance_flag else None),
                geom=None,
                status=_build_status(False, False, error_type="otp_unreachable", error_detail=str(e), status_code=None),
            )

        if resp.status_code != 200:
            if not quiet:
                print("Errore nella richiesta OTP:", resp.status_code)
                print(resp.text)
            return _finalize(
                ([] if impedance_flag else None),
                geom=None,
                status=_build_status(
                    False,
                    False,
                    error_type="otp_http_error",
                    error_detail=f"HTTP {resp.status_code}",
                    status_code=resp.status_code,
                ),
            )

        try:
            data = resp.json()
        except Exception:
            print("OTP ha risposto ma la risposta non è JSON valido.")
            print(resp.text)
            return _finalize(
                ([] if impedance_flag else None),
                geom=None,
                status=_build_status(False, False, error_type="otp_bad_json", error_detail="Invalid JSON", status_code=resp.status_code),
            )

        itineraries = data.get("data", {}).get("plan", {}).get("itineraries", [])
        if not itineraries:
            if not quiet:
                print("Nessun itinerario trovato (OTP).")
            return _finalize(
                ([] if impedance_flag else None),
                geom=None,
                status=_build_status(True, False, error_type="otp_no_itinerary", error_detail=None, status_code=resp.status_code),
            )

        # itinerario più veloce
        itinerary = min(itineraries, key=lambda i: i["duration"])
        total_distance_m = sum(leg.get("distance", 0) for leg in itinerary["legs"])

        if not distance_only:
            # sfondo: grafo passato in input (es. il tuo graphml di Cagliari)
            fig, ax = _ensure_ax(ax)
            ox.plot_graph(
                graph,
                ax=ax,
                bgcolor="black",
                edge_color="black",
                node_size=0,
                edge_linewidth=0.5,
                show=False,
                close=False
            )

        mode_colors = {
            "WALK": "cyan",
            "BUS": "orange",
            "TRAM": "cyan",
            "RAIL": "blue",
            "CAR": "red",
            "IMPEDANCE": "violet",
        }

        prec_leg_end_time = None
        impedance = []
        bus_geom = []
        total_waiting_min = 0.0

        for leg in itinerary["legs"]:
            if not distance_only or save_geometry:
                geometry = leg["legGeometry"]["points"]
                coords = polyline.decode(geometry)  # [(lat, lon), ...]
                bus_geom.append(coords)
                lats, lons = zip(*coords)

            mode = leg["mode"]
            color = mode_colors.get(mode, "white")

            departure_t = _format_time(leg.get("startTime", 0))
            arrive_t = _format_time(leg.get("endTime", 0))
            leg_distance_km = leg["distance"] / 1000.0

            # impedance WALK (leg senza route)
            if impedance_flag and not leg.get("route"):
                impedance.append(get_impedance.impedance_base(leg_distance_km, "walk"))

            label = ""
            if leg.get("route"):
                route_info = leg["route"]
                short = route_info.get("shortName", "")
                long = route_info.get("longName", "")

                # waiting time tra mezzi (minuti)
                if prec_leg_end_time is None:
                    waiting_time_min = 0
                else:
                    waiting_time_min = (leg["startTime"] - prec_leg_end_time) / 60000.0
                total_waiting_min += waiting_time_min

                if impedance_flag:
                    impedance.append(get_impedance.impedance_bus(leg_distance_km, waiting_time_min))

                if short or long:
                    label = f"{short} Partenza: {departure_t} - Arrivo: {arrive_t}"
                else:
                    label = f"BUS Partenza: {departure_t} - Arrivo: {arrive_t}"
            else:
                label = f"{mode} Partenza: {departure_t} - Arrivo: {arrive_t}"

    
            if not distance_only and bus_geom:
                fig, ax = _ensure_ax(ax)
                ax.plot(lons, lats, color=color, linewidth=3, label=label, zorder=5)
            prec_leg_end_time = leg["endTime"]

        if impedance_flag and not distance_only:
            imp_label = "Impedance:\n" + "\n".join([f"{i}: {imp:.3f}" if isinstance(imp, (int, float)) else f"{i}: {imp}"
                                                    for i, imp in enumerate(impedance)])
            fig, ax = _ensure_ax(ax)
            ax.scatter([], [], color="violet", label=imp_label)

        if not distance_only:
            fig, ax = _ensure_ax(ax)
            ax.scatter(origin[1], origin[0], c="lime", s=100, marker="o", label="Origine", zorder=6)
            ax.scatter(destination[1], destination[0], c="red", s=100, marker="o", label="Destinazione", zorder=6)

            handles, labels = ax.get_legend_handles_labels()
            by_label = dict(zip(labels, handles))
            ax.legend(
                by_label.values(),
                by_label.keys(),
                facecolor="black",
                labelcolor="white",
                loc="lower left",
                fontsize=7,
                framealpha=0.9
            )

            ax.set_title("bus", color="white")

        result_value = impedance if impedance_flag else (total_distance_m if distance_only else None)
        # salva distanza + waiting-time bus per riuso futuro
        _route_cache_set(cache_key, {
            "distance_m": total_distance_m,
            "waiting_min": total_waiting_min,
        })
        return _finalize(
            result_value,
            geom=bus_geom,
            status=_build_status(True, True, error_type=None, error_detail=None, status_code=resp.status_code),
        )

    # ==========================
    # CASO WALK/DRIVE/BIKE
    # ==========================
    # usa sempre il grafo full-area per la modalita richiesta
    center_point = None
    dist = None
    network_graph = _get_mode_graph(network_type)

    # colori per il plotting
    mode_colors = {
        "walk": "cyan",
        "drive": "red",
        "bike": "yellow",
        "IMPEDANCE": "violet",
    }
    color = mode_colors.get(network_type, "white")

    # chiave cache per walk/bike/drive
    cache_key = _route_cache_key(
        network_type,
        origin,
        destination,
        dist=dist,
        center_point=center_point,
    )
    # tenta cache del routing già calcolato
    cached = _route_cache_get(cache_key)
    if cached is not None and distance_only and not return_geometry:
        cached_distance = _cached_distance_value(cached)
        if cached_distance is not None:
            if impedance_flag:
                cached_result = get_impedance.impedance_base(cached_distance / 1000.0, network_type)
                if return_status:
                    return fig, ax, cached_result, _build_status(True, True)
                return fig, ax, cached_result
            if return_status:
                return fig, ax, cached_distance, _build_status(True, True)
            return fig, ax, cached_distance

    snapped_nodes = ox.distance.nearest_nodes(
        network_graph,
        [origin[1], destination[1]],
        [origin[0], destination[0]],
    )
    try:
        snapped_nodes = list(snapped_nodes)
    except TypeError:
        snapped_nodes = [snapped_nodes]
    if len(snapped_nodes) < 2:
        raise RuntimeError("nearest_nodes did not return both origin and destination nodes")
    origin_node = int(_normalize_node_id(snapped_nodes[0]))
    destination_node = int(_normalize_node_id(snapped_nodes[1]))

    route_nodes = None
    route_gdf = None
    distance_m = None

    if distance_only:
        distance_m = nx.shortest_path_length(
            network_graph,
            origin_node,
            destination_node,
            weight="length",
            method="dijkstra"
        )
        if return_geometry:
            route_nodes = nx.shortest_path(
                network_graph,
                origin_node,
                destination_node,
                weight="length",
                method="dijkstra"
            )
            route_nodes = cast(list[int], route_nodes)
            route_gdf = route_to_gdf(network_graph, route_nodes)
    else:
        route_nodes = nx.shortest_path(
            network_graph,
            origin_node,
            destination_node,
            weight="length",
            method="dijkstra"
        )
        route_nodes = cast(list[int], route_nodes)

    if not distance_only:
        if route_nodes is None:
            raise RuntimeError("route_nodes is required when distance_only is False")
        route_nodes_nn = cast(list[int], route_nodes)
        # plot grafo + route
        fig, ax = _ensure_ax(ax)
        ox.plot_graph(
            network_graph,
            ax=ax,
            bgcolor="black",
            edge_color="black",
            node_size=0,
            edge_linewidth=0.6,
            show=False,
            close=False
        )

        ox.plot_graph_route(
            network_graph,
            route_nodes_nn,
            ax=ax,
            route_color=color,
            route_linewidth=3,
            show=False,
            close=False
        )

        # marker origine/destinazione
        origin_x, origin_y = network_graph.nodes[origin_node]["x"], network_graph.nodes[origin_node]["y"]
        dest_x, dest_y = network_graph.nodes[destination_node]["x"], network_graph.nodes[destination_node]["y"]

        fig, ax = _ensure_ax(ax)
        ax.scatter(origin_x, origin_y, c="lime", s=100, marker="o", label="Origine", zorder=5)
        ax.scatter(dest_x, dest_y, c="red", s=100, marker="o", label="Destinazione", zorder=5)

    imp_value = None
    if impedance_flag:
        if distance_m is None:
            if route_nodes is None:
                raise RuntimeError("route_nodes is required to compute impedance from geometry")
            gdf = route_to_gdf(network_graph, cast(list[int], route_nodes))
            distance_km = gdf["length"].sum() / 1000.0
        else:
            distance_km = distance_m / 1000.0
        imp_value = get_impedance.impedance_base(distance_km, network_type)
        if not distance_only:
            fig, ax = _ensure_ax(ax)
            ax.scatter([], [], c="violet", label=f"Impedance: {imp_value}", marker="s")

    if not distance_only:
        fig, ax = _ensure_ax(ax)
        ax.legend(facecolor="black", labelcolor="white", loc="lower right", fontsize=8, framealpha=0.9)
        ax.set_title(network_type, color="white")

    # valore finale del routing (impedenza o distanza)
    result_value = imp_value if impedance_flag else (distance_m if distance_only else None)
    if distance_m is None:
        if route_nodes is None:
            raise RuntimeError("route_nodes is required to derive distance_m")
        distance_m = route_to_gdf(network_graph, cast(list[int], route_nodes))["length"].sum()
    # salva distanza (+ waiting nullo per coerenza schema)
    _route_cache_set(cache_key, {
        "distance_m": distance_m,
        "waiting_min": 0.0,
    })

    if return_geometry:
        if route_gdf is None:
            if route_nodes is None:
                raise RuntimeError("route_nodes is required when return_geometry is True")
            route_gdf = route_to_gdf(network_graph, cast(list[int], route_nodes))
        if return_status:
            return fig, ax, result_value, route_gdf, _build_status(True, True)
        return fig, ax, result_value, route_gdf
    if return_status:
        return fig, ax, result_value, _build_status(True, True)
    return fig, ax, result_value
