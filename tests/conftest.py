"""Synthetic fixtures: tiny Word documents assembled from XML, task directories with a stub grader that mimics the
real one's command line and outputs, and Harbor-shaped job trees. No network, no Docker, no judge calls."""

from __future__ import annotations

import json
import os
import shutil
import stat
import textwrap
import zipfile
from pathlib import Path

import pytest

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

STUB_JUDGE = textwrap.dedent('''\
    """Stub of the dataset grader: same CLI and outputs, no judges.

    The deliverable's bytes drive the outcome: a document containing the marker GATEFAIL fails the gate; any
    other document that differs from the baseline passes. STUB_SURVIVORS (env, default 3) sets how many judges
    "survive"; STUB_SCORE (env, default 0.5) sets the weighted score; STUB_FAIL_RUBRICS lists rubric ids that FAIL.
    """
    import argparse, json, os, sys
    from pathlib import Path

    HERE = Path(__file__).resolve().parent
    PANEL = [j for j in os.environ.get("JUDGE_PANEL", "a/x,b/y,c/z").split(",") if j]

    def main():
        ap = argparse.ArgumentParser()
        ap.add_argument("--contract", required=True); ap.add_argument("--baseline"); ap.add_argument("--out-dir", required=True)
        ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--render", default="change-list")
        a = ap.parse_args()
        out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
        task = json.loads((HERE / "rubrics.json").read_text())
        import zipfile
        def body(p):
            try:
                return zipfile.ZipFile(p).read("word/document.xml")
            except Exception:
                return b""
        data = body(a.contract)
        base = body(a.baseline) if a.baseline and Path(a.baseline).exists() else b""
        if b"GATEFAIL" in data or data == base:
            reason = "no net-new work by the declared author" if data == base else "untracked base-text change"
            g = {"task_id": task["task_id"], "gate": {"passed": False, "reason": reason, "flags": []},
                 "score": {"weighted": 0.0, "per_rubric": []}, "judge_mode": "panel", "survivors": []}
            (out / "grade.json").write_text(json.dumps(g)); (out / "reward.json").write_text(json.dumps({"reward": 0.0}))
            return 0
        (out / "change_list.md").write_text("# change list\\n" + "x" * 400)
        if a.dry_run:
            print(json.dumps({"gate": "passed", "redlines": 3, "touched_paragraphs": 2, "comments_added": 1}))
            return 0
        n = int(os.environ.get("STUB_SURVIVORS", "3")); survivors = PANEL[:n]
        fails = set(filter(None, os.environ.get("STUB_FAIL_RUBRICS", "").split(",")))
        per = [{"rubric_id": r["id"], "verdict": "FAIL" if r["id"] in fails else "PASS", "weight": r["weight"],
                "is_penalty": r["weight"] < 0, "category": r.get("category", ""), "criteria": r["criteria"],
                "justification": "stub"} for r in task["rubrics"]]
        pos = [p for p in per if not p["is_penalty"]]
        earned = sum(p["weight"] for p in pos if p["verdict"] == "PASS"); total = sum(p["weight"] for p in pos)
        weighted = float(os.environ.get("STUB_SCORE", earned / total if total else 0))
        g = {"task_id": task["task_id"], "judge_mode": "panel" if n >= 2 else "single-fallback", "judges": PANEL,
             "survivors": survivors, "gate": {"passed": True, "flags": []},
             "score": {"weighted": weighted, "earned_weight": earned, "total_weight": total, "per_rubric": per,
                       "redlines": 3, "median_insertion_chars": 12, "touched_paragraphs": 2, "comments_added": 1,
                       "n_escalations": 1, "n_escalate_rubrics": 1}}
        (out / "judges").mkdir(exist_ok=True)
        for j in survivors:
            (out / "judges" / (j.replace("/", "-") + ".json")).write_text(json.dumps({"verdicts": per}))
        (out / "grade.json").write_text(json.dumps(g)); (out / "reward.json").write_text(json.dumps({"reward": weighted}))
        return 0

    if __name__ == "__main__":
        sys.exit(main())
''')


def make_docx(path: Path, body_xml: str) -> Path:
    """A minimal .docx whose word/document.xml body is `body_xml` (w: prefix bound)."""
    doc = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body_xml}</w:body></w:document>'
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return path


def para(text: str, *revisions: str) -> str:
    """A paragraph with plain text followed by raw revision XML fragments."""
    return f"<w:p><w:r><w:t>{text}</w:t></w:r>{''.join(revisions)}</w:p>"


def ins(text: str, author: str = "Reviewing Counsel (Acme)") -> str:
    return f'<w:ins w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z"><w:r><w:t>{text}</w:t></w:r></w:ins>'


def dele(text: str, author: str = "Reviewing Counsel (Acme)") -> str:
    return f'<w:del w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>{text}</w:delText></w:r></w:del>'


DEFAULT_RUBRICS = [
    {
        "id": "r1",
        "criteria": "Edits the cap",
        "weight": 3,
        "category": "edit",
        "required_action": "Edit",
        "sub_action": None,
        "evaluation_dimension": "Standard Position Execution",
        "priority": "High",
    },
    {
        "id": "r2",
        "criteria": "Leaves clause 4 alone",
        "weight": 2,
        "category": "preserve",
        "required_action": "Preserve",
        "sub_action": None,
        "evaluation_dimension": "Negotiation Restraint",
        "priority": "Medium",
    },
    {
        "id": "r3",
        "criteria": "Escalates the indemnity",
        "weight": 1,
        "category": "escalate",
        "required_action": "Escalate",
        "sub_action": None,
        "evaluation_dimension": "Escalation Judgment",
        "priority": "Low",
    },
]


def make_task(
    root: Path,
    name: str,
    scenario: str,
    turn: int,
    attorney: str,
    split: str = "public",
    rubrics: list[dict] | None = None,
    body: str | None = None,
    responds_to: str = "",
    agreement_type: str = "Master Services Agreement",
    playbook: str = "single-agreement",
    jurisdiction: str = "US",
    side: str = "A",
) -> Path:
    d = root / "tasks" / name
    (d / "environment" / "app").mkdir(parents=True, exist_ok=True)
    (d / "tests" / "baseline").mkdir(parents=True, exist_ok=True)
    rubrics = rubrics or DEFAULT_RUBRICS
    body = body or (para("Clause one text of the agreement.") + para("Clause two text of the agreement."))
    make_docx(d / "environment" / "app" / "contract.docx", body)
    shutil.copyfile(d / "environment" / "app" / "contract.docx", d / "tests" / "baseline" / "contract.docx")
    (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY app/ /app/\n")
    (d / "instruction.md").write_text(f"You represent Acme in a {agreement_type}.\n")
    total = sum(r["weight"] for r in rubrics if r["weight"] > 0)
    (d / "task.toml").write_text(
        textwrap.dedent(f'''\
        artifacts = ["/app/contract.docx"]

        [task]
        name = "contractbench/{name}"
        description = "test task"

        [metadata]
        benchmark = "ContractBench"
        scenario = "{scenario}"
        agreement_type = "{agreement_type}"
        jurisdiction = "{jurisdiction}"
        playbook = "{playbook}"
        turn = {turn}
        side = "{side}"
        represented_party = "Acme"
        counterparty = "Beta"
        author = "Reviewing Counsel (Acme)"
        attorney = "{attorney}"
        responds_to = "{responds_to}"
        expert_id = 7
        n_rubrics = {len(rubrics)}
        total_weight = {total}
        split = "{split}"
        ''')
    )
    (d / "tests" / "rubrics.json").write_text(
        json.dumps(
            {
                "task_id": name,
                "scenario_id": 1,
                "side": side,
                "level": turn,
                "author": "Reviewing Counsel (Acme)",
                "represented_party": "Acme",
                "counterparty": "Beta",
                "escalation_style": "comment_only",
                "rubrics": rubrics,
            }
        )
    )
    (d / "tests" / "judge.py").write_text(STUB_JUDGE)
    return d


def make_dataset(root: Path, scenarios: dict[str, dict] | None = None) -> Path:
    """Default: one public scenario (5 Turn 1 siblings + 2 Turn 2 tasks) and one private scenario (5 + 1)."""
    scenarios = scenarios or {"msa-acme-beta": {"split": "public", "t2": 2}, "ip-gamma-delta": {"split": "private", "t2": 1}}
    (root / "references").mkdir(parents=True, exist_ok=True)
    entries = []
    for scen, spec in scenarios.items():
        for letter in "abcde":
            rub = [dict(r, id=f"{letter}-{r['id']}") for r in DEFAULT_RUBRICS]
            make_task(root, f"{scen}-t1-{letter}", scen, 1, letter.upper(), spec["split"], rub)
            entries.append(f"{scen}-t1-{letter}")
            # reference redline: the attorney's marks on the shared input
            make_docx(
                root / "references" / f"{scen}-t1-{letter}.docx",
                para("Clause one text of the agreement.", dele("one", "Acme Legal"), ins("1", "Acme Legal"))
                + para("Clause two text of the agreement."),
            )
        for letter in "abcde"[: spec["t2"]]:
            body = para("Clause one text of the agreement.", ins("counterparty insert", "Beta Legal")) + para(
                "Clause two text of the agreement."
            )
            make_task(
                root,
                f"{scen}-t2-{letter}",
                scen,
                2,
                letter.upper(),
                spec["split"],
                body=body,
                responds_to=f"{scen}-t1-{letter}",
                side="B",
            )
            entries.append(f"{scen}-t2-{letter}")
            make_docx(
                root / "references" / f"{scen}-t2-{letter}.docx",
                body + para("Clause three.", ins("attorney reply", "Acme Legal")),
            )
    (root / "registry.json").write_text(
        json.dumps(
            [
                {
                    "name": "contractbench-test",
                    "version": "1.0",
                    "tasks": [{"name": f"contractbench/{e}", "path": f"tasks/{e}"} for e in entries],
                }
            ]
        )
    )
    return root


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    return make_dataset(tmp_path / "benchmark")


def opencode_events(
    reason: str = "tool-calls", tools: int = 1, reasoning: int = 0, text: int = 1, errors: list[str] | None = None
) -> str:
    lines = [json.dumps({"type": "step_start", "part": {}})]
    lines += [json.dumps({"type": "tool_use", "part": {}}) for _ in range(tools)]
    lines += [json.dumps({"type": "reasoning", "part": {}}) for _ in range(reasoning)]
    lines += [json.dumps({"type": "text", "part": {}}) for _ in range(text)]
    lines.append(
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "reason": reason,
                    "cost": 0.01,
                    "tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 50, "write": 0}},
                },
            }
        )
    )
    for e in errors or []:
        lines.append(json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": e}}}))
    return "\n".join(lines) + "\n"


def make_trial(
    job_dir: Path,
    task_dir: Path,
    trial_name: str,
    *,
    finished: bool = True,
    exception: str | None = None,
    deliverable: str | None = "changed",
    started: str = "2026-10-16T10:00:00+00:00",
    finished_at: str = "2026-10-16T10:10:00+00:00",
    events: str | None = None,
) -> Path:
    """deliverable: "changed" (input plus an edit), "same" (identical to the input), "gatefail", or None (missing)."""
    t = job_dir / trial_name
    (t / "agent").mkdir(parents=True, exist_ok=True)
    (t / "config.json").write_text(json.dumps({"task": {"path": str(task_dir)}, "trial_name": trial_name}))
    if deliverable is not None:
        src = task_dir / "environment" / "app" / "contract.docx"
        dst = t / "artifacts" / "app" / "contract.docx"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if deliverable == "same":
            shutil.copyfile(src, dst)
        else:
            body = para("Clause one text of the agreement.", dele("one"), ins("1")) + para(
                "Clause two text of the agreement." + (" GATEFAIL" if deliverable == "gatefail" else "")
            )
            make_docx(dst, body)
    (t / "agent" / "opencode.txt").write_text(events if events is not None else opencode_events())
    if finished:
        res = {
            "task_name": task_dir.name,
            "trial_name": trial_name,
            "started_at": started,
            "finished_at": finished_at,
            "agent_result": {"n_input_tokens": 1000, "n_cache_tokens": 10, "n_output_tokens": 200, "cost_usd": 0.5},
            "agent_info": {"name": "opencode", "version": "1.18.32"},
            "agent_execution": {"started_at": started, "finished_at": finished_at},
            "verifier_result": None,
            "exception_info": (
                {"exception_type": exception, "exception_message": "boom", "exception_traceback": "", "occurred_at": finished_at}
                if exception
                else None
            ),
        }
        (t / "result.json").write_text(json.dumps(res))
    return t


def make_job(jobs_root: Path, job_name: str, model_id: str) -> Path:
    job = jobs_root / job_name
    job.mkdir(parents=True, exist_ok=True)
    (job / "config.json").write_text(json.dumps({"job_name": job_name, "agents": [{"name": "opencode", "model_name": model_id}]}))
    return job


FAKE_HARBOR = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake `harbor`: records argv and fabricates a job with k finished trials per input."""
    import json, os, shutil, sys
    from pathlib import Path
    argv = sys.argv[1:]
    log = Path(os.environ["FAKE_HARBOR_LOG"]); log.open("a").write(json.dumps(argv) + "\\n")
    if argv[:1] == ["--version"]:
        print("harbor 0.18.0"); sys.exit(0)
    if argv[:2] == ["job", "resume"]:
        job = Path(argv[argv.index("-p") + 1])
        for t in job.iterdir():
            if t.is_dir() and not (t / "result.json").exists():
                (t / "result.json").write_text(json.dumps({"task_name": json.loads((t / "config.json").read_text())["task"]["path"].split("/")[-1],
                    "trial_name": t.name, "started_at": "2026-10-16T12:00:00+00:00", "finished_at": "2026-10-16T12:05:00+00:00",
                    "agent_result": {"n_input_tokens": 1, "n_cache_tokens": 0, "n_output_tokens": 1, "cost_usd": 0.1},
                    "agent_info": {"name": "opencode", "version": "x"}, "exception_info": None}))
        sys.exit(0)
    tasks_dir = Path(argv[argv.index("-p") + 1]); inputs = [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]
    k = int(argv[argv.index("-k") + 1]); jobs_dir = Path(argv[argv.index("-o") + 1]); name = argv[argv.index("--job-name") + 1]
    job = jobs_dir / name; job.mkdir(parents=True, exist_ok=True)
    (job / "config.json").write_text(json.dumps({"job_name": name, "agents": [{"model_name": argv[argv.index("-m") + 1]}]}))
    crash_after = int(os.environ.get("FAKE_HARBOR_CRASH_AFTER", "0") or 0); made = 0
    for inp in inputs:
        for i in range(k):
            t = job / f"{inp[:32]}__{i:07d}"; (t / "agent").mkdir(parents=True, exist_ok=True)
            (t / "config.json").write_text(json.dumps({"task": {"path": str(tasks_dir / inp)}}))
            (t / "artifacts" / "app").mkdir(parents=True, exist_ok=True)
            data = (tasks_dir / inp / "environment" / "app" / "contract.docx").read_bytes()
            (t / "artifacts" / "app" / "contract.docx").write_bytes(data)  # identical: a no-edit finish
            (t / "agent" / "opencode.txt").write_text(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {}}}) + "\\n")
            made += 1
            if crash_after and made > crash_after:
                continue  # leave the trial unfinished (no result.json) as a crash would
            (t / "result.json").write_text(json.dumps({"task_name": inp, "trial_name": t.name,
                "started_at": f"2026-10-16T10:{i:02d}:00+00:00", "finished_at": f"2026-10-16T10:{i:02d}:30+00:00",
                "agent_result": {"n_input_tokens": 1, "n_cache_tokens": 0, "n_output_tokens": 1, "cost_usd": 0.1},
                "agent_info": {"name": "opencode", "version": "x"}, "exception_info": None}))
    sys.exit(0)
''')


@pytest.fixture
def fake_harbor(tmp_path: Path, monkeypatch) -> Path:
    b = tmp_path / "bin" / "harbor"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(FAKE_HARBOR)
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HARBOR_BIN", str(b))
    monkeypatch.setenv("FAKE_HARBOR_LOG", str(tmp_path / "harbor-argv.log"))
    return b


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    """Run from an empty working directory with jobs/ and runs/ redirected under it."""
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.chdir(wd)
    monkeypatch.setenv("CONTRACTBENCH_JOBS_DIR", str(wd / "jobs"))
    monkeypatch.setenv("CONTRACTBENCH_RUNS_DIR", str(wd / "runs"))
    for k in (
        "CONTRACTBENCH_DIR",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "GROK_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    return wd


def write_env(wd: Path, **kv: str) -> None:
    (wd / ".env").write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
