import requests
import json
import folium
import polyline

# URL del server OTP2
OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

# Query GraphQL
query = """
query {

  plan(
    from: { lat: 39.22294897283518, lon: 9.114625009108789 }
    to: { lat: 39.2312824 , lon: 9.0945547 }
    transportModes: [
      { mode: WALK }
      { mode: BUS }
    ]
    walkReluctance: 2.0
    walkSpeed: 1.3
    numItineraries: 3
    date: "2025-10-6T08:00:00+01:00"
  ) {
    itineraries {
      duration
      walkDistance
      legs {
        mode
        startTime
        endTime
        from {
          name
          lat
          lon
        }
        to {
          name
          lat
          lon
        }
        route {
          shortName
          longName
        }
        distance
        legGeometry {
          points
        }
      }
    }
  }

}
"""

# Esegui la richiesta al server OTP
resp = requests.post(
    OTP_URL,
    headers={"Content-Type": "application/json"},
    data=json.dumps({"query": query})
)

if resp.status_code != 200:
    print("Errore nella richiesta:", resp.status_code)
    print(resp.text)
    exit()

data = resp.json()

# Estrai gli itinerari
itineraries = data.get("data", {}).get("plan", {}).get("itineraries", [])
if not itineraries:
    print("Nessun itinerario trovato.")
    exit()

# 🔹 Seleziona l’itinerario più veloce
itinerary = min(itineraries, key=lambda i: i["duration"])

print(f"Itinerario selezionato: durata {itinerary['duration']} secondi, "
      f"camminata {itinerary['walkDistance']} metri")

# Crea la mappa centrata sul punto di partenza
start_lat = 39.22294897283518
start_lon = 9.114625009108789
m = folium.Map(location=[start_lat, start_lon], zoom_start=14)

# Aggiungi marker per partenza e arrivo
folium.Marker(
    [start_lat, start_lon],
    popup="Partenza, Palazzo delle Scienze",
    icon=folium.Icon(color="green", icon="play")
).add_to(m)

end_lat = 39.23002781302833
end_lon = 9.107288497834322
folium.Marker(
    [end_lat, end_lon],
    popup="Arrivo, Biblioteca Ingegneria",
    icon=folium.Icon(color="red", icon="stop")
).add_to(m)

# Disegna ogni leg dell’itinerario
for leg in itinerary["legs"]:
    geometry = leg["legGeometry"]["points"]
    coords = polyline.decode(geometry)
    mode = leg["mode"]

    # Colore diverso per tipo di trasporto
    color = "blue" if mode == "BUS" else "gray"

    tooltip_text = f"{mode}"
    if leg["route"]:
        tooltip_text += f" ({leg['route']['shortName']} - {leg['route']['longName']})"

    folium.PolyLine(
        coords,
        color=color,
        weight=5,
        opacity=0.8,
        tooltip=tooltip_text
    ).add_to(m)

# Salva la mappa su file
m.save("itinerario_otp2.html")
print("✅ Mappa salvata come 'itinerario_otp2.html'")

# Recupero codice geometria percorso

geometry_code = leg['legGeometry']['points']

print(geometry_code)

# Decodifico tutti i punti del percorso (itinerario)

punti_percorso = polyline.decode(geometry_code)

print(punti_percorso)