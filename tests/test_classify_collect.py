import json
from pathlib import Path

from contractbench import dataset as dsmod
from contractbench.classify import classify
from contractbench.collect import collect, load_ledger
from contractbench.harbor_io import read_trial, summarize_opencode_log
from contractbench.ledger import Run, TrialEntry, load_protocol, need_by_input, number_attempts
from tests.conftest import make_job, make_trial, opencode_events

INFRA = {"CancelledError", "ApiRateLimitError", "SandboxBuildFailedError"}


def _run(workdir: Path, ds, run_id="r1") -> Run:
    protocol = load_protocol()
    run = Run(run_id).create(ds.describe(), protocol, "public", [], "0.18.0", "0.1.0")
    run.record_model(
        "m1",
        {
            "id": "openai/m1",
            "display": "M1",
            "n_concurrent": 1,
            "output_token_max": 1000,
            "opencode_options": {},
            "agent_kwargs": {},
        },
    )
    return run


def test_opencode_summary_and_trial_reader(tmp_path: Path, dataset_dir: Path):
    ds = dsmod.index(dataset_dir)
    job = make_job(tmp_path / "jobs", "j", "openai/m1")
    t = make_trial(
        job,
        ds.tasks["msa-acme-beta-t1-a"].path,
        "msa-acme-beta-t1-a__0000001",
        events=opencode_events("length", tools=0, reasoning=1, text=0, errors=["rate limit exceeded"]),
    )
    s = summarize_opencode_log(t / "agent" / "opencode.txt")
    assert s.n_steps == 1 and s.last_finish_reason == "length" and s.error_messages == ["rate limit exceeded"]
    assert s.total_input_tokens == 150 and s.total_output_tokens == 20
    rec = read_trial(t)
    assert rec.finished and rec.task_name == "msa-acme-beta-t1-a" and rec.cost_usd == 0.5 and rec.wall_sec == 600


def test_classification_statuses(tmp_path: Path, dataset_dir: Path):
    ds = dsmod.index(dataset_dir)
    inp = ds.tasks["msa-acme-beta-t1-a"]
    job = make_job(tmp_path / "jobs", "j", "openai/m1")
    cases = {
        "unfinished": dict(finished=False),
        "infra": dict(exception="ApiRateLimitError"),
        "timeout": dict(exception="AgentTimeoutError"),
        "no_deliverable": dict(deliverable=None),
        "length_stop": dict(deliverable="same", events=opencode_events("length")),
        "ended_in_thinking": dict(deliverable="same", events=opencode_events("stop", tools=0, reasoning=2, text=0)),
        "no_edit_finished": dict(deliverable="same", events=opencode_events("stop")),
        "gate_rejected": dict(deliverable="gatefail"),
        "success": dict(deliverable="changed"),
    }
    for i, (expected, kw) in enumerate(cases.items()):
        t = make_trial(job, inp.path, f"t__{i:07d}", **kw)
        c = classify(read_trial(t), inp, tmp_path / "gate" / expected, infra_types=INFRA)
        assert c.status == expected, (expected, c)
        assert c.counted_failure == (expected in ("length_stop", "ended_in_thinking", "no_edit_finished", "gate_rejected"))
        assert c.rerun == (expected in ("infra", "timeout", "no_deliverable", "unfinished"))
    # an agent error event that is a provider failure is infrastructure, even with an exception type Harbor raised
    t = make_trial(
        job,
        inp.path,
        "t__0000099",
        exception="NonZeroAgentExitCodeError",
        events=opencode_events("stop", errors=["503 Service Unavailable"]),
    )
    assert classify(read_trial(t), inp, None, infra_types=INFRA).status == "infra"
    # the same exception with a changed deliverable and a non-infrastructure error is still a finished attempt
    t = make_trial(
        job,
        inp.path,
        "t__0000098",
        exception="NonZeroAgentExitCodeError",
        events=opencode_events("stop", errors=["tool foo failed"]),
    )
    assert classify(read_trial(t), inp, tmp_path / "gate" / "x", infra_types=INFRA).status == "success"
    # timeouts count as failures under the "counted" policy
    t = make_trial(job, inp.path, "t__0000097", exception="AgentTimeoutError")
    assert classify(read_trial(t), inp, None, infra_types=INFRA, timeout_policy="counted").status == "no_edit_finished"


def test_attempt_numbering_and_need():
    def e(status, start, name):
        return TrialEntry("m", "x-t1-a", name, "j", status, start, None)

    entries = [
        e("success", "2026-01-01T00:03", "d"),
        e("length_stop", "2026-01-01T00:01", "b"),
        e("infra", "2026-01-01T00:00", "a"),
        e("success", "2026-01-01T00:02", "c"),
        e("gate_rejected", "2026-01-01T00:04", "e5"),
        e("unfinished", None, "f"),
    ]
    number_attempts(entries, 3)
    by = {x.trial_name: x for x in entries}
    assert [by[n].attempt for n in ("b", "c", "d", "e5")] == [1, 2, 3, 4]
    assert by["a"].attempt is None and by["f"].attempt is None
    assert [by[n].counted for n in ("b", "c", "d", "e5")] == [True, True, True, False]
    need = need_by_input(entries, ["x-t1-a", "x-t2-a"], "m", 3, 3)
    assert need["x-t1-a"] == {"finished": 4, "need": 0, "reruns": 1, "unfinished": 1, "exhausted": False}
    assert need["x-t2-a"]["need"] == 3 and not need["x-t2-a"]["exhausted"]
    exhausted = [e("infra", "t1", "a"), e("infra", "t2", "b"), e("timeout", "t3", "c")]
    number_attempts(exhausted, 3)
    assert need_by_input(exhausted, ["x-t1-a"], "m", 3, 3)["x-t1-a"]["exhausted"]


def test_collect_layout(workdir: Path, dataset_dir: Path):
    ds = dsmod.index(dataset_dir)
    run = _run(workdir, ds)
    from contractbench.ledger import Wave

    job = make_job(run.jobs_root(), run.job_name("m1", 0), "openai/m1")
    run.add_wave(Wave("m1", 0, job.name, str(job), "initial", ["msa-acme-beta-t1-a", "msa-acme-beta-t2-a"], 2, []))
    t1 = ds.tasks["msa-acme-beta-t1-a"].path
    t2 = ds.tasks["msa-acme-beta-t2-a"].path
    make_trial(job, t1, "msa-acme-beta-t1-a__0000001", started="2026-10-16T10:00:00+00:00")
    make_trial(job, t1, "msa-acme-beta-t1-a__0000002", started="2026-10-16T09:00:00+00:00", deliverable="gatefail")
    make_trial(job, t1, "msa-acme-beta-t1-a__0000003", exception="ApiRateLimitError")
    make_trial(job, t2, "msa-acme-beta-t2-a__0000004", deliverable="same", events=opencode_events("length"))
    make_trial(job, t2, "msa-acme-beta-t2-a__0000005", finished=False)
    entries = collect(run, ds, log=lambda *_: None)
    assert len(entries) == 5
    att = run.dir / "rollouts" / "m1" / "msa-acme-beta-t1-a"
    # the earlier-started gate failure is attempt 1, the success attempt 2, the infra trial archived
    a1 = json.loads((att / "attempt1" / "trial.json").read_text())
    a2 = json.loads((att / "attempt2" / "trial.json").read_text())
    assert a1["status"] == "gate_rejected" and a1["counted"] and (att / "attempt1" / "gate" / "grade.json").exists()
    assert a2["status"] == "success" and (att / "attempt2" / "contract.docx").exists()
    assert (att / "attempt2" / "gate" / "change_list.md").exists()
    assert (att / "rerun" / f"{job.name}__msa-acme-beta-t1-a__0000003" / "trial.json").exists()
    rows = (run.dir / "manifest.csv").read_text().splitlines()
    assert rows[0].startswith("model_slug,model_id,input_task") and len(rows) == 1 + 3  # 3 numbered attempts
    led = load_ledger(run)
    assert sum(1 for e in led if e.status == "unfinished") == 1
    # a second collect is idempotent
    entries2 = collect(run, ds, log=lambda *_: None)
    assert [(e.trial_name, e.attempt) for e in entries2] == [(e.trial_name, e.attempt) for e in entries]
