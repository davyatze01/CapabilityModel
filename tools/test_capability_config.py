"""Sanity checks for config/capability.csv being the genuine source of truth for
which capabilities exist, their services, and their electre_weight/veto_threshold/
enabled settings -- across utils/capabilities.py, stages/capability_stage.py,
core/pipeline_runner.py, and exports/generate_experiment_shapefiles.py.

Does not touch the real config/capability.csv -- everything runs against
temporary files. Plain asserts, no pytest (matches this repo's convention).

Run it from the repo root as a module (plain script invocation puts tools/ on
sys.path instead of the repo root, and utils/ won't import):

    python -m tools.test_capability_config
"""

import csv
import tempfile
from pathlib import Path

from utils import capabilities as cap


def _write_capability_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["capability", "services", "electre_weight", "veto_threshold", "enabled"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_csv_is_the_source_of_truth_for_weight_veto_enabled():
    services = ["sport_and_movement", "scenic_views", "quietness", "cultural_activities", "nature_contact"]
    edited_weights = [0.5, 0.125, 0.125, 0.125, 0.125]  # deliberately non-uniform

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "capability.csv"
        _write_capability_csv(path, [
            {
                "capability": "restorativeness",
                "services": services,
                "electre_weight": edited_weights,
                "veto_threshold": 0.42,
                "enabled": "False",
            },
            {
                "capability": "nutrition",
                "services": ["eating_out", "food_access"],
                "electre_weight": "",  # blank -> falls back to equal weighting
                "veto_threshold": "",  # blank -> falls back to inf
                "enabled": "",  # blank -> falls back to true
            },
        ])

        capability_services, weights, veto, enabled = cap._load_capability_config(path)

        assert list(capability_services["restorativeness"]) == services
        assert weights["restorativeness"][services[0]] == 0.5, weights["restorativeness"]
        assert veto["restorativeness"] == 0.42, veto["restorativeness"]
        assert enabled["restorativeness"] is False, enabled["restorativeness"]

        assert weights["nutrition"] == {"eating_out": 0.5, "food_access": 0.5}
        assert veto["nutrition"] == cap._DEFAULT_V
        assert enabled["nutrition"] is True

        # "care" was not in this CSV at all -- it must not appear, since capability
        # set membership now comes entirely from the file, not a Python default.
        assert "care" not in capability_services

    print("[OK] test_csv_is_the_source_of_truth_for_weight_veto_enabled")


def test_capability_set_is_fully_csv_driven():
    """An analyst can add, remove, or rename capabilities purely through the CSV."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "capability.csv"
        _write_capability_csv(path, [
            {
                "capability": "mobility",
                "services": ["eating_out"],
                "electre_weight": [1.0],
                "veto_threshold": "",
                "enabled": "True",
            },
        ])

        capability_services, weights, veto, enabled = cap._load_capability_config(path)

        assert list(capability_services.keys()) == ["mobility"]
        assert capability_services["mobility"] == ["eating_out"]
        assert weights["mobility"] == {"eating_out": 1.0}

    print("[OK] test_capability_set_is_fully_csv_driven")


def test_unknown_service_is_rejected_when_validated():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "capability.csv"
        _write_capability_csv(path, [
            {
                "capability": "restorativeness",
                "services": ["not_a_real_service"],
                "electre_weight": [1.0],
                "veto_threshold": "",
                "enabled": "",
            },
        ])
        try:
            cap._load_capability_config(path, valid_services={"sport_and_movement"})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for a service outside valid_services")
    print("[OK] test_unknown_service_is_rejected_when_validated")


def test_missing_file_raises():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "does_not_exist.csv"
        try:
            cap._load_capability_config(path)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for a missing capability.csv")
    print("[OK] test_missing_file_raises")


def test_capability_stage_skips_disabled_capability():
    from core.config import PipelineConfig
    from core.pipeline_types import PipelineContext, ServiceNodeResult, ServiceStageResult
    from stages import capability_stage as stage

    all_needed_services = [s for services in cap.CAPABILITY_SERVICES.values() for s in services]
    fake_scores = {s: 0.6 for s in all_needed_services}
    svc = ServiceStageResult(node_results=[
        ServiceNodeResult(node_id=1, lat=0.0, lon=0.0, service_scores=dict(fake_scores)),
        ServiceNodeResult(node_id=2, lat=0.0, lon=0.0, service_scores=dict(fake_scores)),
    ])

    import os

    saved_enabled = dict(stage.cap.CAPABILITY_ENABLED)
    saved_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # capability_stage.py writes to a hardcoded relative "experiments/" dir --
            # run inside a throwaway cwd so this test never touches the repo's real
            # experiments/capability_experiments_recap.csv.
            os.chdir(tmp)

            cfg = PipelineConfig()
            cfg.enable_progress = False
            out_path = Path(tmp) / "capability.csv"
            ctx = PipelineContext(
                config=cfg,
                graph=None,
                nodes_with_coords=[],
                workers=1,
                output_paths={"capabilities": str(out_path)},
                capability_services=dict(cap.CAPABILITY_SERVICES),
            )

            # Force "care" disabled for this run, without touching the real config file.
            stage.cap.CAPABILITY_ENABLED = dict(saved_enabled)
            stage.cap.CAPABILITY_ENABLED["care"] = False

            result = stage.run_capability_stage(ctx, svc)

            with open(result.output_paths["capabilities"], newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                written = list(reader)

            assert "capability_care" not in fieldnames, fieldnames
            assert len(written) == 2
            for row in written:
                assert row["capability_restorativeness"] != ""
                assert row["capability_nutrition"] != ""

            recap_path = Path("experiments") / "capability_experiments_recap.csv"
            with open(recap_path, newline="", encoding="utf-8") as f:
                recap_reader = csv.DictReader(f)
                recap_fieldnames = recap_reader.fieldnames
                recap_rows = list(recap_reader)
            assert "avg_capability_care" not in recap_fieldnames, recap_fieldnames
            last = recap_rows[-1]
            assert last["avg_capability_restorativeness"] != "0.0", last
    finally:
        os.chdir(saved_cwd)
        stage.cap.CAPABILITY_ENABLED = saved_enabled

    print("[OK] test_capability_stage_skips_disabled_capability")


def test_capability_stage_uses_configured_capability_set():
    """capability_stage.py must compute exactly the capabilities in
    CAPABILITY_SERVICES, not a fixed restorativeness/nutrition/care set --
    proves the capability set itself, not just weight/veto/enabled, is CSV
    driven end to end."""
    from core.config import PipelineConfig
    from core.pipeline_types import PipelineContext, ServiceNodeResult, ServiceStageResult
    from stages import capability_stage as stage

    fake_scores = {"eating_out": 0.7, "food_access": 0.9}
    svc = ServiceStageResult(node_results=[
        ServiceNodeResult(node_id=1, lat=0.0, lon=0.0, service_scores=dict(fake_scores)),
    ])

    import os

    saved_services = dict(stage.cap.CAPABILITY_SERVICES)
    saved_weights = dict(stage.cap.CAP_ELECTRE_W)
    saved_enabled = dict(stage.cap.CAPABILITY_ENABLED)
    saved_electre_params = dict(stage.cap._ELECTRE_PARAMS)
    saved_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)

            # Replace the whole capability set with a single, renamed capability that
            # doesn't exist in the real config -- if this flows through, the set is
            # genuinely CSV driven rather than assuming restorativeness/nutrition/care.
            stage.cap.CAPABILITY_SERVICES = {"mobility": ["eating_out", "food_access"]}
            stage.cap.CAP_ELECTRE_W = {"mobility": {"eating_out": 0.5, "food_access": 0.5}}
            stage.cap.CAPABILITY_ENABLED = {"mobility": True}
            stage.cap._ELECTRE_PARAMS = {"mobility": {"v": float("inf")}}

            cfg = PipelineConfig()
            cfg.enable_progress = False
            out_path = Path(tmp) / "capability.csv"
            ctx = PipelineContext(
                config=cfg,
                graph=None,
                nodes_with_coords=[],
                workers=1,
                output_paths={"capabilities": str(out_path)},
                capability_services={"mobility": ["eating_out", "food_access"]},
            )

            result = stage.run_capability_stage(ctx, svc)

            with open(result.output_paths["capabilities"], newline="", encoding="utf-8") as f:
                fieldnames = csv.DictReader(f).fieldnames

            assert "capability_mobility" in fieldnames, fieldnames
            assert "capability_restorativeness" not in fieldnames, fieldnames
            assert "capability_nutrition" not in fieldnames, fieldnames
            assert "capability_care" not in fieldnames, fieldnames
    finally:
        os.chdir(saved_cwd)
        stage.cap.CAPABILITY_SERVICES = saved_services
        stage.cap.CAP_ELECTRE_W = saved_weights
        stage.cap.CAPABILITY_ENABLED = saved_enabled
        stage.cap._ELECTRE_PARAMS = saved_electre_params

    print("[OK] test_capability_stage_uses_configured_capability_set")


if __name__ == "__main__":
    test_csv_is_the_source_of_truth_for_weight_veto_enabled()
    test_capability_set_is_fully_csv_driven()
    test_unknown_service_is_rejected_when_validated()
    test_missing_file_raises()
    test_capability_stage_skips_disabled_capability()
    test_capability_stage_uses_configured_capability_set()
    print("All capability-config tests passed.")
