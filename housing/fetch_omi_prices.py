"""One-off fetch: current OMI (Agenzia delle Entrate) residential price quotations
for every zone in Cagliari_OMI.gpkg, joined back into that same file.

Confirmed scriptable as a plain 4-step POST form wizard (no CAPTCHA, no session
token, no JS challenge) against www1.agenziaentrate.gov.it -- verified by hand
before writing this. Not a scrape of a site that forbids it (unlike Immobiliare.it,
whose robots.txt explicitly disallows the equivalent paths): this is the tax
agency's own free public consultation service, walked the same way a browser
would, at a light request rate.

CODZONA is the join key (already present in the gpkg and matching the site's own
zone codes -- OMI zone codes are stable across semesters even though this
geometry's vintage predates the fetched prices). Writes 6 new columns onto the
existing zone geometries: omi_sale_min/max/final, omi_rent_min/max/final
(sale = euro/sqm, rent = euro/sqm/month). "final" = average(min, max) * MARKUP.

Usage: python housing/fetch_omi_prices.py
"""

from __future__ import annotations

import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from bs4 import BeautifulSoup

GPKG_PATH = Path(__file__).resolve().parent / "Cagliari_OMI.gpkg"
PROVINCE_CODE = "CA"
COMUNE_NAME = "CAGLIARI"
# Tried in order; some peripheral zones have no observed "civili"-tier transactions
# for a given semester and only publish the lower "economico" tier -- fall back to
# that rather than leaving the zone with no price at all.
CATEGORIES = ["Abitazioni civili", "Abitazioni di tipo economico"]
MARKUP = 1.20
# Amortizes a sale price into its equivalent monthly mortgage installment, so
# buying is comparable to renting on the same monthly-cost-per-sqm basis.
MORTGAGE_ANNUAL_RATE = 0.034  # nominal annual rate; /12 gives r, the per-payment (monthly) rate
MORTGAGE_N_PAYMENTS = 240     # n, number of monthly payments (240 = 20 years)
REQUEST_DELAY_S = 0.3

BASE = "https://www1.agenziaentrate.gov.it/servizi/Consultazione"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) capability-model-research/1.0"}


def _parse_options(html: str, select_id: str) -> list[tuple[str, str]]:
    """Return [(value, text), ...] for every <option> inside <select id=select_id>."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id=select_id)
    if select is None:
        raise RuntimeError(f"<select id={select_id!r}> not found in response.")
    return [(opt.get("value", ""), opt.get_text(strip=True)) for opt in select.find_all("option")]


def _parse_hidden(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        inp.get("name"): inp.get("value", "")
        for inp in soup.find_all("input", type="hidden")
        if inp.get("name")
    }


def _amortization_factor(r: float, n: int) -> float:
    """Loan-to-payment factor: multiplying a principal by this gives the fixed
    periodic installment that fully amortizes it over n periods at rate r."""
    return (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def _to_float(text: str) -> float | None:
    text = text.strip()
    if not text or text == "-":
        return None
    return float(text.replace(".", "").replace(",", "."))


def _parse_prices(html: str, categories: list[str]) -> tuple[str, float, float, float, float] | None:
    """Return (category_used, sale_min, sale_max, rent_min, rent_max) for the first
    category in `categories` (priority order) that has a row in this zone's table."""
    soup = BeautifulSoup(html, "html.parser")
    rows_by_category = {}
    for row in soup.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        rows_by_category[cells[0].get_text(strip=True)] = cells

    for category in categories:
        cells = rows_by_category.get(category)
        if cells is None:
            continue
        sale_min = _to_float(cells[2].get_text())
        sale_max = _to_float(cells[3].get_text())
        rent_min = _to_float(cells[5].get_text())
        rent_max = _to_float(cells[6].get_text())
        if None in (sale_min, sale_max, rent_min, rent_max):
            continue
        return category, sale_min, sale_max, rent_min, rent_max
    return None


def fetch_zone_prices(
    session: requests.Session, comune_code: str, semester: str, zone_value: str
) -> tuple[str, float, float, float, float] | None:
    step4 = session.post(
        f"{BASE}/ricerca.htm",
        headers=HEADERS,
        data={
            "level": "4", "lingua": "IT", "pr": PROVINCE_CODE,
            "co": comune_code, "anno_semestre": semester, "linkzonastrada": zone_value,
        },
    )
    step4.raise_for_status()
    hidden = _parse_hidden(step4.text)

    result = session.post(f"{BASE}/risultato.php", headers=HEADERS, data={**hidden, "utilizzo": "Residenziale"})
    result.raise_for_status()
    return _parse_prices(result.text, CATEGORIES)


def main() -> None:
    gdf = gpd.read_file(GPKG_PATH)
    # Rerunning after a previous fetch: drop any omi_* columns already on disk so the
    # merge below can't produce omi_sale_min_x/_y duplicates.
    gdf = gdf.drop(columns=[c for c in gdf.columns if c.startswith("omi_")], errors="ignore")
    print(f"[load] {GPKG_PATH} ({len(gdf)} zones)", flush=True)

    session = requests.Session()

    step1 = session.post(f"{BASE}/ricerca.htm", headers=HEADERS, data={"level": "1", "lingua": "IT", "pr": PROVINCE_CODE})
    step1.raise_for_status()
    semesters = _parse_options(step1.text, "anno_semestre")
    semester = max(semesters, key=lambda kv: int(kv[0]))[0]
    comuni = _parse_options(step1.text, "co")
    comune_code = next(value for value, text in comuni if text.upper() == COMUNE_NAME)
    print(f"[omi] comune={COMUNE_NAME} ({comune_code})  semester={semester}", flush=True)

    step2 = session.post(
        f"{BASE}/ricerca.htm",
        headers=HEADERS,
        data={"level": "2", "lingua": "IT", "pr": PROVINCE_CODE, "co": comune_code, "anno_semestre": semester},
    )
    step2.raise_for_status()
    zones = _parse_options(step2.text, "linkzonastrada")
    print(f"[omi] {len(zones)} zones found for {COMUNE_NAME}", flush=True)

    rows = []
    for i, (zone_value, zone_label) in enumerate(zones, 1):
        zone_code = zone_label.split("/")[0].strip()
        result = fetch_zone_prices(session, comune_code, semester, zone_value)
        if result is None:
            print(f"[omi] {i}/{len(zones)} {zone_code} ({zone_label}): no data in any of {CATEGORIES}, skipped", flush=True)
            time.sleep(REQUEST_DELAY_S)
            continue
        category, sale_min, sale_max, rent_min, rent_max = result
        sale_final = round((sale_min + sale_max) / 2 * MARKUP, 2)
        amort_factor = _amortization_factor(MORTGAGE_ANNUAL_RATE / 12, MORTGAGE_N_PAYMENTS)
        rows.append(
            {
                "CODZONA": zone_code,
                "omi_category": category,
                "omi_sale_min": sale_min,
                "omi_sale_max": sale_max,
                "omi_sale_final": sale_final,
                "omi_sale_monthly": round(sale_final * amort_factor, 4),
                "omi_rent_min": rent_min,
                "omi_rent_max": rent_max,
                "omi_rent_final": round((rent_min + rent_max) / 2 * MARKUP, 2),
            }
        )
        fallback_note = "" if category == CATEGORIES[0] else f"  [fallback: {category}]"
        print(
            f"[omi] {i}/{len(zones)} {zone_code} ({zone_label}): "
            f"sale {sale_min}-{sale_max}, rent {rent_min}-{rent_max}{fallback_note}",
            flush=True,
        )
        time.sleep(REQUEST_DELAY_S)

    prices_df = pd.DataFrame(rows)
    merged = gdf.merge(prices_df, on="CODZONA", how="left")
    missing = int(merged["omi_sale_final"].isna().sum())
    if missing:
        print(f"[omi] WARNING: {missing}/{len(merged)} zones have no price data", flush=True)

    if GPKG_PATH.exists():
        GPKG_PATH.unlink()  # clean overwrite -- avoids stacking a second layer inside the gpkg
    merged.to_file(GPKG_PATH, driver="GPKG")
    print(f"[done] wrote prices for {len(rows)} zones into {GPKG_PATH}", flush=True)


if __name__ == "__main__":
    main()
