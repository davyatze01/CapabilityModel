# run_cities.py (new file)

import os
import sys
import subprocess
import time
import threading
import queue
from core.config import derive_city_slug

CITIES = [
    "Assemini, Sardinia, Italy",
    "Capoterra, Sardinia, Italy",
    "Decimomannu, Sardinia, Italy",
    "Maracalagonis, Sardinia, Italy",
    "Pula, Sardinia, Italy",
    "Quartu Sant'Elena, Sardinia, Italy",
    "Sarroch, Sardinia, Italy",
    "Selargius, Sardinia, Italy",
    "Sestu, Sardinia, Italy",
    "Settimo San Pietro, Sardinia, Italy",
    "Sinnai, Sardinia, Italy",
    "Uta, Sardinia, Italy",
    "Villa San Pietro, Sardinia, Italy",
    "Quartucciu, Sardinia, Italy",
    "Elmas, Sardinia, Italy",
    "Monserrato, Sardinia, Italy",
    "Arbus, Sardinia, Italy",
    "Armungia, Sardinia, Italy",
    "Ballao, Sardinia, Italy",
    "Barrali, Sardinia, Italy",
    "Barumini, Sardinia, Italy",
    "Buggerru, Sardinia, Italy",
    "Burcei, Sardinia, Italy",
    "Calasetta, Sardinia, Italy",
    "Carbonia, Sardinia, Italy",
    "Carloforte, Sardinia, Italy",
    "Castiadas, Sardinia, Italy",
    "Collinas, Sardinia, Italy",
    "Decimoputzu, Sardinia, Italy",
    "Dolianova, Sardinia, Italy",
    "Domus de Maria, Sardinia, Italy",
    "Domusnovas, Sardinia, Italy",
    "Donori, Sardinia, Italy",
    "Escalaplano, Sardinia, Italy",
    "Escolca, Sardinia, Italy",
    "Esterzili, Sardinia, Italy",
    "Fluminimaggiore, Sardinia, Italy",
    "Furtei, Sardinia, Italy",
    "Genoni, Sardinia, Italy",
    "Genuri, Sardinia, Italy",
    "Gergei, Sardinia, Italy",
    "Gesico, Sardinia, Italy",
    "Gesturi, Sardinia, Italy",
    "Giba, Sardinia, Italy",
    "Goni, Sardinia, Italy",
    "Gonnesa, Sardinia, Italy",
    "Gonnosfanadiga, Sardinia, Italy",
    "Guamaggiore, Sardinia, Italy",
    "Guasila, Sardinia, Italy",
    "Guspini, Sardinia, Italy",
    "Iglesias, Sardinia, Italy",
    "Isili, Sardinia, Italy",
    "Las Plassas, Sardinia, Italy",
    "Lunamatrona, Sardinia, Italy",
    "Mandas, Sardinia, Italy",
    "Masainas, Sardinia, Italy",
    "Monastir, Sardinia, Italy",
    "Muravera, Sardinia, Italy",
    "Musei, Sardinia, Italy",
    "Narcao, Sardinia, Italy",
    "Nuragus, Sardinia, Italy",
    "Nurallao, Sardinia, Italy",
    "Nuraminis, Sardinia, Italy",
    "Nurri, Sardinia, Italy",
    "Nuxis, Sardinia, Italy",
    "Orroli, Sardinia, Italy",
    "Ortacesus, Sardinia, Italy",
    "Pabillonis, Sardinia, Italy",
    "Pauli Arbarei, Sardinia, Italy",
    "Perdaxius, Sardinia, Italy",
    "Pimentel, Sardinia, Italy",
    "Piscinas, Sardinia, Italy",
    "Portoscuso, Sardinia, Italy",
    "Sadali, Sardinia, Italy",
    "Samassi, Sardinia, Italy",
    "Samatzai, Sardinia, Italy",
    "San Basilio, Sardinia, Italy",
    "San Gavino Monreale, Sardinia, Italy",
    "San Giovanni Suergiu, Sardinia, Italy",
    "San Nicolò Gerrei, Sardinia, Italy",
    "San Sperate, Sardinia, Italy",
    "San Vito, Sardinia, Italy",
    "Sanluri, Sardinia, Italy",
    "Santadi, Sardinia, Italy",
    "Sant'Andrea Frius, Sardinia, Italy",
    "Sant'Anna Arresi, Sardinia, Italy",
    "Sant'Antioco, Sardinia, Italy",
    "Sardara, Sardinia, Italy",
    "Segariu, Sardinia, Italy",
    "Selegas, Sardinia, Italy",
    "Senorbì, Sardinia, Italy",
    "Serdiana, Sardinia, Italy",
    "Serramanna, Sardinia, Italy",
    "Serrenti, Sardinia, Italy",
    "Serri, Sardinia, Italy",
    "Setzu, Sardinia, Italy",
    "Seui, Sardinia, Italy",
    "Seulo, Sardinia, Italy",
    "Siddi, Sardinia, Italy",
    "Siliqua, Sardinia, Italy",
    "Silius, Sardinia, Italy",
    "Siurgus Donigala, Sardinia, Italy",
    "Soleminis, Sardinia, Italy",
    "Suelli, Sardinia, Italy",
    "Teulada, Sardinia, Italy",
    "Tratalias, Sardinia, Italy",
    "Tuili, Sardinia, Italy",
    "Turri, Sardinia, Italy",
    "Ussana, Sardinia, Italy",
    "Ussaramanna, Sardinia, Italy",
    "Vallermosa, Sardinia, Italy",
    "Villacidro, Sardinia, Italy",
    "Villamar, Sardinia, Italy",
    "Villamassargia, Sardinia, Italy",
    "Villanova Tulo, Sardinia, Italy",
    "Villanovaforru, Sardinia, Italy",
    "Villanovafranca, Sardinia, Italy",
    "Villaperuccio, Sardinia, Italy",
    "Villaputzu, Sardinia, Italy",
    "Villasalto, Sardinia, Italy",
    "Villasimius, Sardinia, Italy",
    "Villasor, Sardinia, Italy",
    "Villaspeciosa, Sardinia, Italy",
]

CITY_TIMEOUT_SECONDS = 60 * 60  # 1 hour per city
SUCCESS_EXIT_GRACE_SECONDS = 90
STOP_ON_FAILURE = False
FORCE_RERUN = False
EXPERIMENTS_DIR = "experiments"
REQUIRED_EXPERIMENT_BASENAMES = (
    "capability_restorativeness.csv",
    "capability_nutrition.csv",
    "capability_care.csv",
)
CITY_SUFFIX = ", Sardinia, Italy"


def city_slug(city_name: str) -> str:
    return derive_city_slug(city_name)


def city_already_computed(city_name: str) -> bool:
    slug = city_slug(city_name)
    required_paths = [
        os.path.join(EXPERIMENTS_DIR, f"{slug}_{basename}")
        for basename in REQUIRED_EXPERIMENT_BASENAMES
    ]
    return all(os.path.exists(path) for path in required_paths)


def validate_cities(cities: list[str]) -> None:
    """Fail fast when city literals look malformed (for example missing comma concatenation)."""
    bad: list[str] = []
    for city in cities:
        if city.count(CITY_SUFFIX) != 1:
            bad.append(city)
            continue
        if not city.endswith(CITY_SUFFIX):
            bad.append(city)
    if bad:
        details = "\n".join(f"- {name}" for name in bad)
        raise ValueError(
            "Malformed city entries detected in CITIES. "
            "This often happens when a missing comma concatenates two string literals.\n"
            f"{details}"
        )


def run_city(city_name: str) -> int:
    env = os.environ.copy()
    env["CAP_CITY_NAME"] = city_name
    env["PYTHONUNBUFFERED"] = "1"
    print(f"\n=== Running city: {city_name} ===", flush=True)
    start = time.time()
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    q: queue.Queue[str] = queue.Queue()

    def _pump_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)
        q.put("")

    t = threading.Thread(target=_pump_stdout, daemon=True)
    t.start()

    saw_completion_marker = False
    completion_seen_at = 0.0
    visible_prefixes = (
        "[Stage]",
        "[Artifact]",
        "[Output]",
        "[Bus]",
        "[Non-bus]",
        "[Plot]",
        "[POI]",
        "[Graph]",
        "[Walkability",
    )

    while True:
        now = time.time()
        elapsed = now - start

        if elapsed > CITY_TIMEOUT_SECONDS:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(f"=== Timeout city: {city_name} after {elapsed:.1f}s ===", flush=True)
            return 124

        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            line = None

        if line:
            stripped = line.strip()
            if stripped.startswith(visible_prefixes) or stripped.startswith("Traceback"):
                print(line, end="", flush=True)
            if "[Output] Capability CSV files:" in line:
                saw_completion_marker = True
                completion_seen_at = now
        elif line == "":
            # EOF on child's stdout
            pass

        rc = proc.poll()
        if rc is not None:
            elapsed = time.time() - start
            print(f"=== Finished city: {city_name} (exit={rc}, {elapsed:.1f}s) ===", flush=True)
            return int(rc)

        if saw_completion_marker and (now - completion_seen_at) > SUCCESS_EXIT_GRACE_SECONDS:
            # Main output is done but process did not exit cleanly: terminate and continue.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            elapsed = time.time() - start
            print(
                f"=== Forced-close city: {city_name} after completion marker ({elapsed:.1f}s) ===",
                flush=True,
            )
            return 0

def main() -> None:
    validate_cities(CITIES)
    failures = []
    skipped = []
    for city in CITIES:
        if not FORCE_RERUN and city_already_computed(city):
            print(f"=== Skipping city (already computed): {city} ===", flush=True)
            skipped.append(city)
            continue

        code = run_city(city)
        if code != 0:
            failures.append((city, code))
            if STOP_ON_FAILURE:
                break

    if failures:
        print("\nFailures:")
        for city, code in failures:
            print(f"- {city}: exit code {code}")
        sys.exit(1)

    if skipped:
        print("\nSkipped cities:")
        for city in skipped:
            print(f"- {city}")

    print("\nAll city runs completed successfully.")

if __name__ == "__main__":
    main()
