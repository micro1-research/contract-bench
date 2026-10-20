"""The files the runner writes validate against schemas/."""

import json
from pathlib import Path

import jsonschema

from contractbench import dataset as dsmod
from contractbench import metrics as metricsmod
from contractbench.ledger import Run
from tests.test_runner_grade_metrics import _graded_run

ROOT = Path(__file__).resolve().parents[1]


def _schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text())


def test_schemas_are_valid_and_outputs_conform(workdir: Path, dataset_dir: Path, monkeypatch):
    for name in ("attempt.schema.json", "grade_meta.schema.json", "metrics_summary.schema.json"):
        jsonschema.Draft7Validator.check_schema(_schema(name))
    run, ds = _graded_run(workdir, dataset_dir, monkeypatch)
    import os

    from contractbench import grade as grademod

    jobs = grademod.plan(run, ds)
    grademod.grade_all(run, ds, dict(os.environ), jobs, panel="a/x,b/y,c/z", parallel=4, retries=0, log=lambda *_: None)
    for p in run.dir.glob("rollouts/*/*/attempt*/trial.json"):
        jsonschema.validate(json.loads(p.read_text()), _schema("attempt.schema.json"))
    for p in run.dir.glob("grades/*/*/attempt*/grade_meta.json"):
        jsonschema.validate(json.loads(p.read_text()), _schema("grade_meta.schema.json"))
    result = metricsmod.compute(Run("g1").load(), dsmod.index(dataset_dir))
    jsonschema.validate(json.loads(json.dumps(result, default=str)), _schema("metrics_summary.schema.json"))
