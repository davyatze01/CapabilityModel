ox.settings.use_cache = True

ox.settings.log_console = False
 
grafo = graphml.get_graph()
 
origine = (39.22231439353061, 9.113848879825527)
 
print("Nodi: ", type(grafo))
 
G = grafo
 
first_node = next(iter(G.nodes))

print("Primo nodo:", first_node)
 
# ==========================

# CACHE accessibility array

# ==========================

ACCESS_CACHE_DIR = "access_cache"

os.makedirs(ACCESS_CACHE_DIR, exist_ok=True)
 
poi_type = "healthcare"

cache_path = os.path.join(ACCESS_CACHE_DIR, f"accessibility_{poi_type}.pkl")
 
if os.path.exists(cache_path):

    print("📦 Carico accessibility da cache")

    with open(cache_path, "rb") as f:

        payload = pickle.load(f)
 
    node_ids = payload["node_ids"]

    accessibility = payload["accessibility"]

else:

    accessibility = []

    node_ids = []
 
    for node, data in tqdm(

        G.nodes(data=True),

        total=G.number_of_nodes(),

        desc="Calcolo accessibility",

        mininterval=0.5

    ):

        lat = data.get("y")

        lon = data.get("x")

        origin = (lat, lon)
 
        # skip nodi senza coordinate (per sicurezza)

        if lat is None or lon is None:

            continue
 
        node_ids.append(node)

        accessibility.append(delta_g.accessibility(poi_type, origin))
 
    print("💾 Salvo accessibility in cache")

    with open(cache_path, "wb") as f:

        pickle.dump({"node_ids": node_ids, "accessibility": accessibility}, f)
 
print("✅ Lunghezza accessibility:", len(accessibility))
 