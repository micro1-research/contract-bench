"""What happened to a trial, in the protocol's terms.

Counted failures (`length_stop`, `ended_in_thinking`, `no_edit_finished`, `gate_rejected`) are the model's own
failures: they count as attempts and are never replaced. Re-run statuses (`infra`, `timeout`, `no_deliverable`,
`unfinished`) are not the model's doing and are retried, a bounded number of times.

Order of evidence: the Harbor exception type, then the agent's own error events, then the deliverable (missing,
unchanged, or changed), and for a changed deliverable the grader's validity gate run in dry mode.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .dataset import TaskInfo
from .harbor_io import OpencodeSummary, TrialRecord, summarize_opencode_log

COUNTED_FAILURES = ("length_stop", "ended_in_thinking", "no_edit_finished", "gate_rejected")
RERUN = ("infra", "timeout", "no_deliverable", "unfinished")
TIMEOUT_TYPES = {"AgentTimeoutError"}
INFRA_MESSAGE_RE = re.compile(
    r"(rate.?limit|429|overloaded|5\d\d|ECONNRESET|ETIMEDOUT|socket hang up|connection (reset|closed|refused)|"
    r"invalid.?api.?key|authentication|unauthorized|quota|insufficient_quota|service unavailable)",
    re.IGNORECASE,
)


@dataclass
class Classification:
    status: str
    reason: str = ""
    counted_failure: bool = False
    rerun: bool = False
    gate: dict = field(default_factory=dict)
    deliverable: dict = field(default_factory=dict)
    opencode: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _finish(status: str, reason: str = "", **extra) -> Classification:
    return Classification(
        status=status, reason=reason, counted_failure=status in COUNTED_FAILURES, rerun=status in RERUN, **extra
    )


def gate_dry_run(deliverable: Path, input_task: TaskInfo, out_dir: Path, python: str | None = None, timeout: int = 900) -> dict:
    """Run the input task's grader with --dry-run on a temporary copy of its tests/ folder. Returns
    {"ran": True, "passed": bool, "reason": str, "flags": [...], "diagnostics": {...}}; the dry run makes no judge
    calls and writes change_list.md into out_dir when the gate passes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cb-gate-"))
    try:
        tests = work / "tests"
        shutil.copytree(input_task.tests_dir, tests, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cmd = [
            python or sys.executable,
            "-B",
            str(tests / "judge.py"),
            "--contract",
            str(deliverable),
            "--baseline",
            str(tests / "baseline" / "contract.docx"),
            "--out-dir",
            str(out_dir),
            "--dry-run",
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"}
        )
        (out_dir / "dry_run.log").write_text((r.stdout or "") + (r.stderr or ""))
        grade = out_dir / "grade.json"
        if grade.exists():  # the grader writes grade.json before the dry-run check only on a gate failure
            g = json.loads(grade.read_text())
            gate = g.get("gate") or {}
            if gate.get("passed") is False:
                return {
                    "ran": True,
                    "passed": False,
                    "reason": str(gate.get("reason") or "gate failed"),
                    "flags": list(gate.get("flags") or []),
                    "diagnostics": {},
                }
        m = re.search(r"\{.*\}", r.stdout or "", flags=re.S)
        if m:
            try:
                diag = json.loads(m.group(0))
            except ValueError:
                diag = {}
            if diag.get("gate") == "passed":
                return {"ran": True, "passed": True, "reason": "", "flags": [], "diagnostics": diag}
        return {
            "ran": True,
            "passed": False,
            "reason": f"grader dry run gave no verdict (exit {r.returncode})",
            "flags": [],
            "diagnostics": {},
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def classify(
    trial: TrialRecord,
    input_task: TaskInfo,
    gate_out_dir: Path | None,
    *,
    infra_types: set[str],
    timeout_policy: str = "rerun",
    run_gate: bool = True,
    python: str | None = None,
) -> Classification:
    if not trial.finished:
        return _finish("unfinished", "no result.json yet")
    oc: OpencodeSummary | None = summarize_opencode_log(trial.opencode_log) if trial.opencode_log else None
    oc_dict = asdict(oc) if oc else {}
    et = trial.exception_type
    if et == "CancelledError":
        return _finish("unfinished", "cancelled", opencode=oc_dict)
    if et in infra_types:
        return _finish("infra", f"{et}: {trial.exception_message or ''}".strip(), opencode=oc_dict)
    if et in TIMEOUT_TYPES:
        status = "timeout" if timeout_policy == "rerun" else "no_edit_finished"
        return _finish(status, f"{et}", opencode=oc_dict)
    if et and oc and oc.error_messages and all(INFRA_MESSAGE_RE.search(m) for m in oc.error_messages):
        return _finish("infra", f"{et}: {oc.error_messages[-1]}", opencode=oc_dict)
    # the deliverable decides from here
    if trial.artifact is None:
        return _finish("no_deliverable", f"{et or 'no exception'}; artifacts/app/contract.docx missing", opencode=oc_dict)
    sha = sha256_file(trial.artifact)
    deliverable = {
        "present": True,
        "sha256": sha,
        "bytes": trial.artifact.stat().st_size,
        "identical_to_input": sha == sha256_file(input_task.input_docx),
    }
    if deliverable["identical_to_input"]:
        reason = (oc.last_finish_reason if oc else None) or "unknown"
        if reason == "length":
            return _finish(
                "length_stop",
                "the agent stopped at its output limit without changing the document",
                deliverable=deliverable,
                opencode=oc_dict,
            )
        if oc and oc.last_step_reasoning_parts and not oc.last_step_tool_calls and not oc.last_step_text_parts:
            return _finish(
                "ended_in_thinking",
                "the agent's last turn was reasoning only; the document is unchanged",
                deliverable=deliverable,
                opencode=oc_dict,
            )
        return _finish(
            "no_edit_finished",
            f"the agent finished (reason {reason}) without changing the document",
            deliverable=deliverable,
            opencode=oc_dict,
        )
    if not run_gate or gate_out_dir is None:
        return _finish("success", "deliverable changed; gate not run", deliverable=deliverable, opencode=oc_dict)
    gate = gate_dry_run(trial.artifact, input_task, gate_out_dir, python=python)
    if gate["passed"]:
        return _finish("success", "", gate=gate, deliverable=deliverable, opencode=oc_dict)
    return _finish("gate_rejected", gate["reason"], gate=gate, deliverable=deliverable, opencode=oc_dict)
