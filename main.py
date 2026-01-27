from utils import graphml, route, decay, delta_g, capabilities as cap
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

# --- Configurazione ---
# Numero di processi paralleli da avviare.
# Consiglio: se noti rallentamenti dovuti al calcolo del BUS o alla rete, riduci questo numero.
CAP_WORKERS = 8  

# Silenzia warning non critici per mantenere la console pulita
shutup.please()

# Configurazione di OSMnx per ridurre l'output verboso e usare la cache interna
ox.settings.use_cache = True
ox.settings.log_console = False


def _process_node(node_item):
    """
    Funzione eseguita da ogni worker per un singolo nodo della mappa.
    Calcola l'accessibilità ai vari servizi e aggrega i punteggi.
    """
    node_id, data = node_item
    
    # Verifica che il nodo abbia coordinate valide
    if "y" not in data or "x" not in data:
        return None
    origin = (data["y"], data["x"])

    dining_out_accessibility = []
    on_the_go_accessibility = []
    services = []

    # === Fase 1: Ottimizzazione del Routing ===
    # Invece di ricalcolare le strade per ogni tipo di ristorante o bar,
    # calcoliamo la mappa delle distanze (Dijkstra) una volta sola per questo nodo.
    try:
        dist_cache = delta_g.precompute_distances(origin) 
    except Exception:
        # Se c'è un errore grave nel grafo (es. nodo isolato), saltiamo questo nodo
        return None 
    
    # === Fase 2: Calcolo Accessibilità per Categoria ===
    # Calcoliamo l'accessibilità per i luoghi dove "mangiare fuori" (ristoranti, pizzerie...)
    # Passiamo 'dist_cache' per riutilizzare i calcoli fatti sopra.
    for poi_type in cap.dining_out_list:
        val = delta_g.accessibility(poi_type, origin, network_cache=dist_cache)
        dining_out_accessibility.append(val)
    
    # Aggreghiamo i punteggi parziali usando l'Integrale di Choquet (gestisce le sinergie tra servizi)
    services.append(cap.choquet_integral(dining_out_accessibility, cap.cap_dining_out))

    # Facciamo lo stesso per i servizi "al volo" (fast food, bar...)
    for poi_type in cap.on_the_go_list:
        val = delta_g.accessibility(poi_type, origin, network_cache=dist_cache)
        on_the_go_accessibility.append(val)
        
    services.append(cap.choquet_integral(on_the_go_accessibility, cap.cap_on_the_go))

    # === Fase 3: Punteggio Finale ===
    # Combiniamo i due macro-servizi (Dining Out e On The Go) in un unico indice di "Capability to Eat"
    capability_to_eat = cap.choquet_integral(services, cap.cap_eat)

    return [
        node_id,
        origin[0],
        origin[1],
        capability_to_eat,
        services[0],
        services[1],
    ]

def init_worker(shared_graph, shared_pois):
    """
    Questa funzione viene lanciata all'avvio di ogni processo worker.
    Serve a iniettare il Grafo e i POI direttamente nella memoria del worker,
    evitando di doverli ricaricare o riscaricare ogni volta.
    """
    from utils import delta_g
    delta_g._G_CACHE = shared_graph
    delta_g._POI_GEOM_CACHE = shared_pois

def main():
    # --- Step 1: Caricamento Dati Statici ---
    print("--- STEP 1: Loading Graph ---")
    graph = graphml.get_graph()
    nodes = list(graph.nodes(data=True))

    # --- Step 2: Pre-caricamento dei Punti di Interesse (POI) ---
    # Scarichiamo tutti i POI necessari (bar, ristoranti, ecc.) una volta sola nel processo principale.
    # Questo evita che 8 worker provino a scaricarli contemporaneamente, che causerebbe blocchi o ban IP.
    print("--- STEP 2: Pre-loading POIs (sequentially) ---")
    
    # Uniamo le liste per avere l'elenco completo dei POI unici da scaricare
    all_needed_pois = set(cap.dining_out_list + cap.on_the_go_list)
    
    pois_cache = delta_g.preload_all_pois(list(all_needed_pois))
    print(f"Loaded {len(pois_cache)} POI categories in memory.")
    
    # Preparazione file di output
    os.makedirs("outputs", exist_ok=True)
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

        pbar = tqdm(total=len(nodes), desc="Processing Nodes", mininterval=1)

        # Thread separato che controlla se il processo si è bloccato
        def _monitor():
            while not stop_event.wait(10): # Controlla ogni 10 secondi
                pbar.refresh()
                with last_row_lock:
                    idle_s = time.time() - last_row_time[0]
                
                # Se non scriviamo righe nel CSV da più di 10 minuti, avvisiamo l'utente.
                # Spesso accade se il server di routing (OTP) per i BUS non risponde.
                if idle_s >= 600: 
                    print("\nWarning: no rows written in the last 10 minutes. Possible OTP/Bus timeout.")

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

        print(f"--- STEP 3: Starting Pool with {workers} workers ---")
        try:
            # Avviamo il Pool di processi, passando Grafo e POI all'inizializzatore
            with mp.Pool(processes=workers, initializer=init_worker, initargs=(graph, pois_cache)) as pool:
                # Usiamo imap_unordered per massima velocità (l'ordine delle righe nel CSV non importa)
                for row in pool.imap_unordered(_process_node, nodes, chunksize=10):
                    if row is None:
                        continue
                    writer.writerow(row)
                    f.flush() # Scrive su disco immediatamente per sicurezza
                    pbar.update(1)
                    
                    # Aggiorniamo il timestamp per dire al monitor che siamo vivi
                    with last_row_lock:
                        last_row_time[0] = time.time()
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            # Pulizia finale
            stop_event.set()
            monitor_thread.join(timeout=2)
            pbar.close()

    print(f"Done. Wrote results to: {output_path}")

if __name__ == "__main__":
    main()