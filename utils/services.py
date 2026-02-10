from dataclasses import dataclass


@dataclass(frozen=True)
class PoiQuery:
    service: str
    poi_type: str
    radius_m: int | None = None
    tags: dict | None = None


SERVICE_POI_QUERIES = {
    # H1 -> Restorativeness capability (F1-F5)
    # F1 -> Sport and movement
    "sport_and_movement": [
        PoiQuery("sport_and_movement", "leisure_sports_centre", tags={"leisure": "sports_centre"}),
        PoiQuery("sport_and_movement", "leisure_swimming_pool", tags={"leisure": "swimming_pool"}),
        PoiQuery("sport_and_movement", "leisure_pitch", tags={"leisure": "pitch"}),
        PoiQuery("sport_and_movement", "leisure_track", tags={"leisure": "track"}),
        PoiQuery("sport_and_movement", "leisure_golf_course", tags={"leisure": "golf_course"}),
        PoiQuery("sport_and_movement", "leisure_ice_rink", tags={"leisure": "ice_rink"}),
        PoiQuery("sport_and_movement", "leisure_fitness_centre", tags={"leisure": "fitness_centre"}),
        PoiQuery("sport_and_movement", "leisure_skatepark", tags={"leisure": "skatepark"}),
        PoiQuery("sport_and_movement", "sport_climbing", tags={"sport": "climbing"}),
        PoiQuery("sport_and_movement", "leisure_climbing", tags={"leisure": "climbing"}),
        PoiQuery("sport_and_movement", "leisure_swimming_area", tags={"leisure": "swimming_area"}),
        PoiQuery("sport_and_movement", "leisure_marina", tags={"leisure": "marina"}),
        PoiQuery("sport_and_movement", "route_hiking", tags={"route": "hiking"}),
        PoiQuery("sport_and_movement", "route_running", tags={"route": "running"}),
    ],

    # F2 -> Enjoy scenic views
    "scenic_views": [
        PoiQuery("scenic_views", "tourism_attractions", tags={"tourism": "attractions"}),
        PoiQuery("scenic_views", "leisure_park", tags={"leisure": "park"}),
        PoiQuery("scenic_views", "leisure_garden", tags={"leisure": "garden"}),
        PoiQuery("scenic_views", "natural_water", tags={"natural": "water"}),
        PoiQuery("scenic_views", "amenity_fountain", tags={"amenity": "fountain"}),
        PoiQuery("scenic_views", "waterway_canal", tags={"waterway": "canal"}),
        PoiQuery("scenic_views", "waterway_stream", tags={"waterway": "stream"}),
        PoiQuery("scenic_views", "waterway_river", tags={"waterway": "river"}),
    ],

    # F3 -> Enjoy quietness
    "quietness": [
        PoiQuery("quietness", "leisure_park", tags={"leisure": "park"}),
        PoiQuery("quietness", "leisure_garden", tags={"leisure": "garden"}),
        PoiQuery("quietness", "amenity_library", tags={"amenity": "library"}),
    ],

    # F4 -> Cultural activities
    "cultural_activities": [
        PoiQuery("cultural_activities", "amenity_library", tags={"amenity": "library"}),
        PoiQuery("cultural_activities", "amenity_cinema", tags={"amenity": "cinema"}),
        PoiQuery("cultural_activities", "amenity_theatre", tags={"amenity": "theatre"}),
        PoiQuery("cultural_activities", "amenity_arts_centre", tags={"amenity": "arts_centre"}),
        PoiQuery("cultural_activities", "tourism_museum", tags={"tourism": "museum"}),
        PoiQuery(
            "cultural_activities",
            "historic_tourism_attraction",
            tags={"historic": True, "tourism": "attraction"},
        ),
        PoiQuery("cultural_activities", "amenity_community_centre", tags={"amenity": "community_centre"}),
    ],

    # F5 -> Nature contact
    "nature_contact": [
        PoiQuery("nature_contact", "leisure_park", tags={"leisure": "park"}),
        PoiQuery("nature_contact", "leisure_garden", tags={"leisure": "garden"}),
        PoiQuery("nature_contact", "natural_water", tags={"natural": "water"}),
        PoiQuery("nature_contact", "amenity_fountain", tags={"amenity": "fountain"}),
        PoiQuery("nature_contact", "waterway_canal", tags={"waterway": "canal"}),
        PoiQuery("nature_contact", "waterway_stream", tags={"waterway": "stream"}),
        PoiQuery("nature_contact", "waterway_river", tags={"waterway": "river"}),
    ],

    # Nutrition capability (F6-F8)
    # F6 -> Eating out
    "eating_out": [
        PoiQuery("eating_out", "amenity_restaurant", tags={"amenity": "restaurant"}),
        PoiQuery("eating_out", "amenity_fast_food", tags={"amenity": "fast_food"}),
        PoiQuery("eating_out", "shop_bakery", tags={"shop": "bakery"}),
        PoiQuery("eating_out", "shop_pastry", tags={"shop": "pastry"}),
    ],

    # F7 -> Fresh food access
    "fresh_food_access": [
        PoiQuery("fresh_food_access", "shop_convenience", tags={"shop": "convenience"}),
        PoiQuery("fresh_food_access", "shop_supermarket", tags={"shop": "supermarket"}),
        PoiQuery("fresh_food_access", "shop_greengrocer", tags={"shop": "greengrocer"}),
        PoiQuery("fresh_food_access", "shop_bakery", tags={"shop": "bakery"}),
        PoiQuery("fresh_food_access", "shop_butcher", tags={"shop": "butcher"}),
        PoiQuery("fresh_food_access", "shop_fishmonger", tags={"shop": "fishmonger"}),
    ],

    # F8 -> Ready food access
    "ready_food_access": [
        PoiQuery("ready_food_access", "shop_convenience", tags={"shop": "convenience"}),
        PoiQuery("ready_food_access", "shop_frozen_food", tags={"shop": "frozen_food"}),
        PoiQuery("ready_food_access", "shop_beverages", tags={"shop": "beverages"}),
    ],

    # H3 -> Care capability (F9-F14)
    # F9 -> Medicines and supplies
    "medicines_and_supplies": [
        PoiQuery("medicines_and_supplies", "shop_medical_supply", tags={"shop": "medical_supply"}),
        PoiQuery("medicines_and_supplies", "shop_healthcare", tags={"shop": "healthcare"}),
        PoiQuery("medicines_and_supplies", "amenity_pharmacy", tags={"amenity": "pharmacy"}),
        PoiQuery("medicines_and_supplies", "healthcare_pharmacy", tags={"healthcare": "pharmacy"}),
    ],

    # F10 -> Impatient and care
    "impatient_and_care": [
        PoiQuery("impatient_and_care", "amenity_hospital", tags={"amenity": "hospital"}),
        PoiQuery("impatient_and_care", "healthcare_hospital", tags={"healthcare": "hospital"}),
        PoiQuery("impatient_and_care", "amenity_clinic", tags={"amenity": "clinic"}),
        PoiQuery("impatient_and_care", "healthcare_clinic", tags={"healthcare": "clinic"}),
        PoiQuery("impatient_and_care", "healthcare_centre", tags={"healthcare": "centre"}),
        PoiQuery(
            "impatient_and_care",
            "amenity_emergency_service_emergency_yes",
            tags={"amenity": "emergency_service", "emergency": "yes"},
        ),
        PoiQuery(
            "impatient_and_care",
            "social_facility_assisted_living",
            tags={"amenity": "social_facility", "social_facility": "assisted_living"},
        ),
        PoiQuery("impatient_and_care", "amenity_retirement_home", tags={"amenity": "retirement_home"}),
    ],

    # F11 -> Rehabilitation services
    "rehabilitation_services": [
        PoiQuery("rehabilitation_services", "healthcare_prosthetics", tags={"healthcare": "prosthetics"}),
        PoiQuery(
            "rehabilitation_services",
            "healthcare_occupational_therapist",
            tags={"healthcare": "occupational_therapist"},
        ),
        PoiQuery("rehabilitation_services", "healthcare_psychomotrician", tags={"healthcare": "psychomotrician"}),
        PoiQuery("rehabilitation_services", "healthcare_dietitian", tags={"healthcare": "dietitian"}),
        PoiQuery(
            "rehabilitation_services",
            "office_healthcare_speciality_dietitian",
            tags={"office": "healthcare", "healthcare:speciality": "dietitian"},
        ),
        PoiQuery("rehabilitation_services", "healthcare_psychologist", tags={"healthcare": "psychologist"}),
        PoiQuery("rehabilitation_services", "healthcare_physiotherapist", tags={"healthcare": "physiotherapist"}),
        PoiQuery("rehabilitation_services", "amenity_spa", tags={"amenity": "spa"}),
        PoiQuery("rehabilitation_services", "leisure_spa", tags={"leisure": "spa"}),
    ],

    # F12 -> Diagnosis and prevention
    "diagnosis_and_prevention": [
        PoiQuery("diagnosis_and_prevention", "healthcare_centre", tags={"healthcare": "centre"}),
        PoiQuery("diagnosis_and_prevention", "amenity_clinic", tags={"amenity": "clinic"}),
        PoiQuery("diagnosis_and_prevention", "healthcare_clinic", tags={"healthcare": "clinic"}),
        PoiQuery(
            "diagnosis_and_prevention",
            "healthcare_specialty_psychiatry",
            tags={"healthcare:specialty": "psychiatry"},
        ),
        PoiQuery(
            "diagnosis_and_prevention",
            "healthcare_centre_speciality_preventive_medicine",
            tags={"healthcare": "centre", "healthcare:speciality": "preventive_medicine"},
        ),
        PoiQuery(
            "diagnosis_and_prevention",
            "healthcare_clinic_speciality_preventive_medicine",
            tags={"healthcare": "clinic", "healthcare:speciality": "preventive_medicine"},
        ),
        PoiQuery(
            "diagnosis_and_prevention",
            "healthcare_centre_facility_multi_speciality",
            tags={"healthcare": "centre", "healthcare:facility": "multi_speciality"},
        ),
        PoiQuery(
            "diagnosis_and_prevention",
            "healthcare_clinic_facility_multi_speciality",
            tags={"healthcare": "clinic", "healthcare:facility": "multi_speciality"},
        ),
        PoiQuery("diagnosis_and_prevention", "healthcare_physiotherapist", tags={"healthcare": "physiotherapist"}),
        PoiQuery("diagnosis_and_prevention", "healthcare_podiatrist", tags={"healthcare": "podiatrist"}),
        PoiQuery("diagnosis_and_prevention", "healthcare_laboratory", tags={"healthcare": "laboratory"}),
        PoiQuery(
            "diagnosis_and_prevention",
            "amenity_clinic_speciality_laboratory_medicine",
            tags={"amenity": "clinic", "healthcare:speciality": "laboratory_medicine"},
        ),
        PoiQuery("diagnosis_and_prevention", "healthcare_blood_donation", tags={"healthcare": "blood_donation"}),
        PoiQuery("diagnosis_and_prevention", "blood_donation_transfusion", tags={"blood_donation": "transfusion"}),
        PoiQuery("diagnosis_and_prevention", "amenity_doctors", tags={"amenity": "doctors"}),
        PoiQuery("diagnosis_and_prevention", "healthcare_doctor", tags={"healthcare": "doctor"}),
        PoiQuery("diagnosis_and_prevention", "amenity_dentist", tags={"amenity": "dentist"}),
        PoiQuery("diagnosis_and_prevention", "healthcare_dentist", tags={"healthcare": "dentist"}),
        PoiQuery("diagnosis_and_prevention", "healthcare_psychologist", tags={"healthcare": "psychologist"}),
    ],

    # F13 -> Emergency services
    "emergency_services": [
        PoiQuery("emergency_services", "amenity_hospital", tags={"amenity": "hospital"}),
        PoiQuery("emergency_services", "healthcare_hospital", tags={"healthcare": "hospital"}),
        PoiQuery("emergency_services", "emergency_department", tags={"emergency": "department"}),
        PoiQuery(
            "emergency_services",
            "healthcare_speciality_obstetrics",
            tags={"healthcare:speciality": "obstetrics"},
        ),
        PoiQuery("emergency_services", "amenity_ambulance_station", tags={"amenity": "ambulance_station"}),
        PoiQuery("emergency_services", "emergency_ambulance_station", tags={"emergency": "ambulance_station"}),
        PoiQuery("emergency_services", "healthcare_blood_donation", tags={"healthcare": "blood_donation"}),
    ],

    # F14 -> Care services
    "care_services": [
        PoiQuery("care_services", "healthcare_midwife", tags={"healthcare": "midwife"}),
        PoiQuery("care_services", "healthcare_nurse", tags={"healthcare": "nurse"}),
        PoiQuery("care_services", "healthcare_dialysis", tags={"healthcare": "dialysis"}),
        PoiQuery("care_services", "healthcare_home_care", tags={"healthcare": "home_care"}),
        PoiQuery("care_services", "healthcare_clinic", tags={"healthcare": "clinic"}),
        PoiQuery(
            "care_services",
            "healthcare_speciality_addiction_medicine",
            tags={"healthcare:speciality": "addiction_medicine"},
        ),
        PoiQuery(
            "care_services",
            "healthcare_centre_speciality_maternal_child_health",
            tags={"healthcare": "centre", "healthcare:speciality": "maternal_child_health"},
        ),
        PoiQuery(
            "care_services",
            "social_facility_assisted_living",
            tags={"amenity": "social_facility", "social_facility": "assisted_living"},
        ),
        PoiQuery(
            "care_services",
            "social_facility_nursing_home",
            tags={"amenity": "social_facility", "social_facility": "nursing_home"},
        ),
        PoiQuery(
            "care_services",
            "social_facility_day_care",
            tags={"amenity": "social_facility", "social_facility": "day_care"},
        ),
        PoiQuery("care_services", "amenity_retirement_home", tags={"amenity": "retirement_home"}),
        PoiQuery("care_services", "amenity_community_centre", tags={"amenity": "community_centre"}),
        PoiQuery(
            "care_services",
            "social_facility_group_home",
            tags={"amenity": "social_facility", "social_facility": "group_home"},
        ),
        PoiQuery(
            "care_services",
            "social_facility_shelter",
            tags={"amenity": "social_facility", "social_facility": "shelter"},
        ),
        PoiQuery(
            "care_services",
            "social_facility_workshop",
            tags={"amenity": "social_facility", "social_facility": "workshop"},
        ),
        PoiQuery(
            "care_services",
            "social_facility_assisted_living_only",
            tags={"social_facility": "assisted_living"},
        ),
        PoiQuery("care_services", "amenity_shelter", tags={"amenity": "shelter"}),
        PoiQuery("care_services", "amenity_refugee_site", tags={"amenity": "refugee_site"}),
        PoiQuery("care_services", "amenity_social_facility", tags={"amenity": "social_facility"}),
    ],
}


SERVICE_KEYS = list(SERVICE_POI_QUERIES.keys())

SERVICE_SINGLETON_M = {
    "care_services": {
        "healthcare_midwife": 0.6,
        "healthcare_nurse": 0.6,
        "healthcare_dialysis": 0.6,
        "healthcare_home_care": 0.8,
        "healthcare_clinic": 0.6,
        "healthcare_speciality_addiction_medicine": 0.4,
        "healthcare_centre_speciality_maternal_child_health": 0.6,
        "social_facility_assisted_living": 0.8,
        "social_facility_nursing_home": 1.0,
        "social_facility_day_care": 0.4,
        "amenity_retirement_home": 0.8,
        "amenity_community_centre": 0.4,
        "social_facility_group_home": 0.6,
        "social_facility_shelter": 0.6,
        "social_facility_workshop": 0.4,
        "social_facility_assisted_living_only": 0.8,
        "amenity_shelter": 0.6,
        "amenity_refugee_site": 0.4,
        "amenity_social_facility": 0.6,
    },

    "cultural_activities": {
        "amenity_library": 0.8,
        "amenity_cinema": 0.8,
        "amenity_theatre": 0.8,
        "amenity_arts_centre": 0.6,
        "tourism_museum": 0.8,
        "historic_tourism_attraction": 0.4,
        "amenity_community_centre": 0.4,
    },

    "diagnosis_and_prevention": {
        "healthcare_centre": 0.8,
        "amenity_clinic": 0.8,
        "healthcare_clinic": 0.8,
        "healthcare_specialty_psychiatry": 0.6,
        "healthcare_centre_speciality_preventive_medicine": 0.8,
        "healthcare_clinic_speciality_preventive_medicine": 0.8,
        "healthcare_centre_facility_multi_speciality": 1.0,
        "healthcare_clinic_facility_multi_speciality": 1.0,
        "healthcare_physiotherapist": 0.4,
        "healthcare_podiatrist": 0.4,
        "healthcare_laboratory": 1.0,
        "amenity_clinic_speciality_laboratory_medicine": 1.0,
        "healthcare_blood_donation": 0.2,
        "blood_donation_transfusion": 0.2,
        "amenity_doctors": 1.0,
        "healthcare_doctor": 1.0,
        "amenity_dentist": 0.8,
        "healthcare_dentist": 0.8,
        "healthcare_psychologist": 0.6,
    },

    "eating_out": {
        "amenity_restaurant": 1.0,
        "amenity_fast_food": 0.8,
        "shop_bakery": 0.4,
        "shop_pastry": 0.4,
    },

    "emergency_services": {
        "amenity_hospital": 1.0,
        "healthcare_hospital": 1.0,
        "emergency_department": 1.0,
        "healthcare_speciality_obstetrics": 0.6,
        "amenity_ambulance_station": 0.8,
        "emergency_ambulance_station": 0.8,
        "healthcare_blood_donation": 0.4,
    },

    "fresh_food_access": {
        "shop_convenience": 0.6,
        "shop_supermarket": 1.0,
        "shop_greengrocer": 0.8,
        "shop_bakery": 0.6,
        "shop_butcher": 0.8,
        "shop_fishmonger": 0.8,
    },

    "impatient_and_care": {
        "amenity_hospital": 1.0,
        "healthcare_hospital": 1.0,
        "amenity_clinic": 0.8,
        "healthcare_clinic": 0.8,
        "healthcare_centre": 0.8,
        "amenity_emergency_service_emergency_yes": 1.0,
        "social_facility_assisted_living": 0.6,
        "amenity_retirement_home": 0.6,
    },

    "medicines_and_supplies": {
        "shop_medical_supply": 0.8,
        "shop_healthcare": 0.6,
        "amenity_pharmacy": 1.0,
        "healthcare_pharmacy": 1.0,
    },

    "nature_contact": {
        "leisure_park": 0.8,
        "leisure_garden": 0.8,
        "natural_water": 1.0,
        "amenity_fountain": 0.4,
        "waterway_canal": 0.6,
        "waterway_stream": 0.6,
        "waterway_river": 0.8,
    },

    "quietness": {
        "leisure_park": 0.6,
        "leisure_garden": 0.6,
        "amenity_library": 1.0,
    },

    "ready_food_access": {
        "shop_convenience": 0.8,
        "shop_frozen_food": 0.6,
        "shop_beverages": 0.4,
    },

    "rehabilitation_services": {
        "healthcare_prosthetics": 1.0,
        "healthcare_occupational_therapist": 0.8,
        "healthcare_psychomotrician": 0.6,
        "healthcare_dietitian": 0.6,
        "office_healthcare_speciality_dietitian": 0.6,
        "healthcare_psychologist": 0.6,
        "healthcare_physiotherapist": 1.0,
        "amenity_spa": 0.6,
        "leisure_spa": 0.6,
    },

    "scenic_views": {
        "tourism_attractions": 1.0,
        "leisure_park": 0.8,
        "leisure_garden": 0.8,
        "natural_water": 1.0,
        "amenity_fountain": 0.6,
        "waterway_canal": 0.6,
        "waterway_stream": 0.6,
        "waterway_river": 0.8,
    },

    "sport_and_movement": {
        "leisure_sports_centre": 1.0,
        "leisure_swimming_pool": 0.8,
        "leisure_pitch": 0.6,
        "leisure_track": 0.6,
        "leisure_golf_course": 0.4,
        "leisure_ice_rink": 0.4,
        "leisure_fitness_centre": 0.8,
        "leisure_skatepark": 0.4,
        "sport_climbing": 0.6,
        "leisure_climbing": 0.6,
        "leisure_swimming_area": 0.4,
        "leisure_marina": 0.4,
        "route_hiking": 0.4,
        "route_running": 0.4,
    },
}


def get_service_queries(service: str) -> list[PoiQuery]:
    return SERVICE_POI_QUERIES[service]


def get_service_poi_types() -> dict[str, list[str]]:
    return {
        service: [q.poi_type for q in queries]
        for service, queries in SERVICE_POI_QUERIES.items()
    }


def all_queries() -> list[PoiQuery]:
    return [q for group in SERVICE_POI_QUERIES.values() for q in group]


def unique_query_keys() -> list[PoiQuery]:
    seen = set()
    out = []
    for q in all_queries():
        key = query_key(q)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def query_key(q: PoiQuery) -> tuple[str, int | None, tuple | None]:
    tags_key = None
    if q.tags:
        tags_key = tuple(sorted(q.tags.items()))
    return (q.poi_type, q.radius_m, tags_key)


SPORT_AND_MOVEMENT_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["sport_and_movement"])}
SCENIC_VIEWS_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["scenic_views"])}
QUIETNESS_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["quietness"])}
CULTURAL_ACTIVITIES_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["cultural_activities"])}
NATURE_CONTACT_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["nature_contact"])}
EATING_OUT_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["eating_out"])}
FRESH_FOOD_ACCESS_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["fresh_food_access"])}
READY_FOOD_ACCESS_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["ready_food_access"])}
MEDICINES_AND_SUPPLIES_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["medicines_and_supplies"])}
IMPATIENT_AND_CARE_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["impatient_and_care"])}
REHABILITATION_SERVICES_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["rehabilitation_services"])}
DIAGNOSIS_AND_PREVENTION_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["diagnosis_and_prevention"])}
EMERGENCY_SERVICES_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["emergency_services"])}
CARE_SERVICES_IDX = {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES["care_services"])}


def cap(S, service):
    if len(S) == 0:
        return 0
    elif len(S) == 1:
        return SERVICE_SINGLETON_M[service][S[0]]
    else:
        singletons =  [SERVICE_SINGLETON_M[service][k] for k in S]
        m = max(singletons)
        return min(1, m + 0.2 * (1 - m))

def choquet_integral(x, service):
    n = len(x)
    order = sorted(range(n), key=lambda i: x[i])
    x_sorted = [x[i] for i in order]
    poi_types = [q.poi_type for q in SERVICE_POI_QUERIES[service]]

    total = 0.0
    prev = 0.0
    for j in range(n):
        tail = [poi_types[i] for i in order[j:]]
        total += (x_sorted[j] - prev) * cap(tail, service)
        prev = x_sorted[j]
    return total


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_service_poi_types(), sort_dicts=True)
