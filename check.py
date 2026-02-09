import re
from bs4 import BeautifulSoup

HTML_FILE = "pois_by_service_links.html"

# Bounding box approssimata di Cagliari
CAGLIARI_BBOX = {
    "min_lat": 39.1950,
    "max_lat": 39.2450,
    "min_lng": 9.05,
    "max_lng": 9.18
}

def estrai_coord(url):
    """
    Estrae lat,lng da link tipo:
    https://maps.google.com/?q=39.226794,9.133378
    """
    match = re.search(r'[?&]q=([0-9\.\-]+),([0-9\.\-]+)', url)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def in_cagliari(lat, lng):
    return (
        CAGLIARI_BBOX["min_lat"] <= lat <= CAGLIARI_BBOX["max_lat"] and
        CAGLIARI_BBOX["min_lng"] <= lng <= CAGLIARI_BBOX["max_lng"]
    )


# ---------------------------
# MAIN
# ---------------------------
with open(HTML_FILE, "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, "html.parser")

links = soup.find_all("a")

dentro = []
fuori = []

for a in links:
    href = a.get("href")
    nome = a.get_text(strip=True)

    if not href:
        continue

    coord = estrai_coord(href)
    if not coord:
        continue

    lat, lng = coord
    is_inside = in_cagliari(lat, lng)

    record = {
        "nome": nome,
        "lat": lat,
        "lng": lng,
        "link": href
    }

    if is_inside:
        dentro.append(record)
    else:
        fuori.append(record)

# ---------------------------
# RISULTATI
# ---------------------------
print(f"\n✅ DENTRO CAGLIARI ({len(dentro)})")
for r in dentro:
    print(f"- {r['nome']} ({r['lat']}, {r['lng']})")

print(f"\n❌ FUORI CAGLIARI ({len(fuori)})")
for r in fuori:
    print(f"- {r['nome']} ({r['lat']}, {r['lng']})")
