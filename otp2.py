import requests
import json

OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

query = """
query {
  plan(
    fromPlace: "39.22294897283518,9.114625009108789",
    toPlace: "39.23998019279815,9.098612322838331",
    numItineraries: 1,
    transportModes: [
      {
        mode: BUS
      }
    ]
  ) {
    itineraries {
      legs {
        mode
        from {lat lon}
        to {lat lon}
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
