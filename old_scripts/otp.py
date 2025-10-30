import requests
import json

OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

query = """
query {

  plan(
    from: { lat: 39.22294897283518, lon: 9.114625009108789 }
    to: { lat: 39.23998019279815, lon: 9.098612322838331 }
    transportModes: [
      { mode: WALK }
      { mode: BUS }
    ]
    walkReluctance: 2.0
    walkSpeed: 1.3
    numItineraries: 3
    date: "2025-10-30T08:00:00+01:00"
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


resp = requests.post(
    OTP_URL,
    headers={"Content-Type": "application/json"},
    data=json.dumps({"query": query})
)

print(resp.status_code)
print(resp.text)
