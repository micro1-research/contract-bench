import json
import os
from pathlib import Path

from contractbench import dataset as dsmod
from contractbench import grade as grademod
from contractbench import metrics as metricsmod
from contractbench import report, runner, surgicality
from contractbench.cli import main
from contractbench.collect import collect
from contractbench.ledger import Run, load_protocol
from tests.conftest import write_env


def _argv_log(wd: Path) -> list[list[str]]:
    p = Path(os.environ["FAKE_HARBOR_LOG"])
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_run_then_resume_with_fake_harbor(workdir: Path, dataset_dir: Path, fake_harbor, monkeypatch):
    write_env(workdir, OPENAI_API_KEY="sk-test-1234567890")
    monkeypatch.setenv("CONTRACTBENCH_DIR", str(dataset_dir))
    monkeypatch.setenv("FAKE_HARBOR_CRASH_AFTER", "4")  # the fake leaves trials after the 4th unfinished
    rc = main(["run", "--run-id", "r1", "--model", "openai/m1", "--set", "public", "-k", "2"])
    assert rc == 0
    run = Run("r1").load()
    assert "m1" in run.data["models"] and len(run.waves("m1")) == 1
    calls = _argv_log(workdir)
    first = [c for c in calls if c[:1] == ["run"]][0]
    assert first[first.index("-k") + 1] == "2" and "--disable-verification" in first
    assert any(a.startswith("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=") for a in first)
    # the tick after the wave resumed the job for the unfinished trials (the fake finishes them)
    assert any(c[:2] == ["job", "resume"] for c in calls)
    st = runner.status(run, dsmod.index(dataset_dir))
    assert st["m1"]["inputs"] == 3 and st["m1"]["complete"] == 3
    # running again with different settings is refused
    rc = main(["run", "--run-id", "r1", "--model", "openai/m1", "--output-token-max", "999"])
    assert rc == 1
    # resume is a no-op when complete
    assert main(["resume", "--run-id", "r1"]) == 0
    assert main(["status", "--run-id", "r1"]) == 0
    assert main(["run", "--run-id", "r2", "--model", "openai/m1", "--dry-run"]) == 0 and not Run("r2").waves()


def _graded_run(workdir: Path, dataset_dir: Path, monkeypatch, score_env: dict | None = None) -> tuple[Run, dsmod.Dataset]:
    """Collect a synthetic job tree with changed deliverables and grade it with the stub judge."""
    from contractbench.ledger import Wave
    from tests.conftest import make_job, make_trial

    ds = dsmod.index(dataset_dir)
    run = Run("g1").create(ds.describe(), load_protocol(), "public", [], "0.18.0", "0.1.0")
    run.record_model(
        "m1",
        {
            "id": "openai/m1",
            "display": "M1",
            "n_concurrent": 1,
            "output_token_max": 1000,
            "opencode_options": {},
            "agent_kwargs": {},
            "best_effort": False,
        },
    )
    job = make_job(run.jobs_root(), run.job_name("m1", 0), "openai/m1")
    inputs = [t.name for t in ds.inputs("public")]
    run.add_wave(Wave("m1", 0, job.name, str(job), "initial", inputs, 3, []))
    n = 0
    for inp in inputs:
        for i in range(3):
            n += 1
            make_trial(
                job,
                ds.tasks[inp].path,
                f"{inp}__{n:07d}",
                started=f"2026-10-16T10:{i:02d}:00+00:00",
                deliverable="changed" if not (inp.endswith("t2-b") and i == 2) else "gatefail",
            )
    collect(run, ds, log=lambda *_: None)
    for k, v in (score_env or {}).items():
        monkeypatch.setenv(k, v)
    write_env(workdir, ANTHROPIC_API_KEY="a-1234567890", OPENAI_API_KEY="o-1234567890", GEMINI_API_KEY="g-1234567890")
    return run, ds


def test_grade_plan_skip_retry_and_metrics(workdir: Path, dataset_dir: Path, monkeypatch):
    run, ds = _graded_run(workdir, dataset_dir, monkeypatch)
    jobs = grademod.plan(run, ds)
    # 3 attempts x 5 sibling sets for the Turn 1 input, 3 + 2 for the two Turn 2 inputs (one gate failure excluded)
    assert len(jobs) == 15 + 3 + 2
    env = {**os.environ, "STUB_FAIL_RUBRICS": "a-r3"}
    counts = grademod.grade_all(run, ds, env, jobs, panel="a/x,b/y,c/z", parallel=4, retries=1, log=lambda *_: None)
    assert counts == {"planned": 20, "skipped": 0, "run": 20, "degraded": 0, "failed": 0}
    g = json.loads((run.dir / "grades" / "m1" / "msa-acme-beta-t1-a" / "attempt1" / "grade.json").read_text())
    assert g["score"]["weighted"] == 5 / 6  # rubric a-r3 (weight 1 of 6) failed
    # second pass skips everything
    counts = grademod.grade_all(run, ds, env, jobs, panel="a/x,b/y,c/z", parallel=4, retries=1, log=lambda *_: None)
    assert counts["skipped"] == 20 and counts["run"] == 0
    # a degraded panel is retried and then flagged
    one = [j for j in jobs if j.target.name == "msa-acme-beta-t2-a" and j.attempt == 1]
    import shutil

    shutil.rmtree(one[0].out_dir)  # a done grade is never re-run; clear it to exercise the degraded path
    env2 = {**env, "STUB_SURVIVORS": "2"}
    counts = grademod.grade_all(run, ds, env2, one, panel="a/x,b/y,c/z", parallel=1, retries=1, log=lambda *_: None)
    assert counts["degraded"] == 1 and counts["run"] == 2
    meta = json.loads((one[0].out_dir / "grade_meta.json").read_text())
    assert meta["degraded"] is True and len(meta["survivors"]) == 2
    # metrics
    res = metricsmod.compute(run, ds)
    row = res["leaderboard"][0]
    assert row["model"] == "M1" and row["public_score"] is not None and row["private_score"] is None
    assert row["full_score"] is None  # the run covers the public set only
    # score: Turn 1 grades = 5/6 on sets with a-r3 (only set a has it): 3 attempts x (1 set at 5/6 + 4 sets at 1.0)
    # Turn 2: 5 grades at 1.0 (rubric ids r1..r3 are not a-r3)
    expected = (3 * (5 / 6 + 4 * 1.0) + 5 * 1.0) / 20
    assert abs(row["public_score"] - expected) < 1e-12
    assert abs(row["turn1_score"] - (5 / 6 + 4) / 5) < 1e-12 and row["turn2_score"] == 1.0
    assert row["completion_rate_public"] == 8 / 9 and row["n_grades"] == 20 and row["degraded_grades"] == 1
    # spread: pstdev of [5/6, 1, 1, 1, 1] per attempt, in points
    import statistics as st

    assert abs(row["spread_points"] - 100 * st.pstdev([5 / 6, 1, 1, 1, 1])) < 1e-9
    assert res["slices"]["m1"]["required_action"]["Escalate"] < 1.0 < 1.0 + 1e-9
    assert res["failures"]["m1"]["gate_rejected"] == 1
    out = run.dir / "metrics"
    report.write_all(out, res)
    assert (
        (out / "leaderboard.csv")
        .read_text()
        .splitlines()[0]
        .startswith("model,private_score,public_score,full_score,completion_rate")
    )
    # compare against a "published" file with the same numbers passes, a different one fails
    pub = out / "pub.csv"
    pub.write_text(f"model,private_score,public_score,full_score,completion_rate\nM1,,{row['public_score']:.6f},,\n")
    ok, lines = report.compare(res, pub)
    assert ok and lines
    pub.write_text("model,private_score,public_score,full_score,completion_rate\nM1,,0.1,,\n")
    assert report.compare(res, pub)[0] is False
    written = report.eval_results_yaml(res, "org/Bench", out / "ev")
    assert written and "contractbench_public" in written[0].read_text()
    # surgicality: the reference edits are inline replacements; the synthetic model deliverable too
    surg = surgicality.compute(run, ds)
    assert surg["surgicality"]["attorneys"]["score"] == 1.0 and surg["surgicality"]["m1"]["score"] == 1.0
    assert surg["edit_volume"]["events"]["n"] == 1 and surg["edit_volume"]["events_t2"]["n"] == 2
    surgicality.write(out, surg, {"m1": "M1", "attorneys": "Attorneys"})
    assert (out / "surgicality.csv").exists() and (out / "edit_volume.csv").exists()
    # the CLI metrics command end to end
    monkeypatch.setenv("CONTRACTBENCH_DIR", str(dataset_dir))
    assert main(["metrics", "--run-id", "g1", "--compare", str(pub)]) == 2  # the altered published file differs
