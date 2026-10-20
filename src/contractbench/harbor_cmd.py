"""Harbor command lines. Pure functions: nothing here runs a process, so the runner's decisions can be tested by
inspecting argv.

Every run uses the same shape: the opencode agent, one job per model, verification disabled (grading happens
offline against all sibling rubric sets), no Harbor-side retries (the protocol decides what is re-run), and the
output token cap passed both as an agent environment variable and exported in the process environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .keys import OUTPUT_CAP_VAR
from .models import ResolvedModel

MIN_HARBOR = (0, 18, 0)


def harbor_bin() -> str:
    explicit = os.environ.get("HARBOR_BIN")
    if explicit:
        return explicit
    found = shutil.which("harbor")
    if not found:
        raise FileNotFoundError("harbor is not on PATH; install it (`uv tool install harbor`) or set HARBOR_BIN")
    return found


def harbor_version(binary: str | None = None) -> tuple[int, ...]:
    out = subprocess.run([binary or harbor_bin(), "--version"], capture_output=True, text=True, check=False)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout + out.stderr)
    if not m:
        raise RuntimeError(f"could not read the Harbor version from {out.stdout!r} {out.stderr!r}")
    return tuple(int(x) for x in m.groups())


def check_version(binary: str | None = None) -> tuple[int, ...]:
    v = harbor_version(binary)
    if v < MIN_HARBOR:
        raise RuntimeError(f"Harbor {'.'.join(map(str, v))} found; {'.'.join(map(str, MIN_HARBOR))} or later is required")
    return v


def run_argv(
    *,
    binary: str,
    tasks_dir: Path,
    inputs: list[str],
    model: ResolvedModel,
    k: int,
    n_concurrent: int,
    job_name: str,
    jobs_dir: Path,
) -> list[str]:
    argv = [binary, "run", "-p", str(tasks_dir)]
    for name in inputs:
        argv += ["-i", name]
    argv += [
        "-a",
        "opencode",
        "-m",
        model.id,
        "-k",
        str(k),
        "-n",
        str(n_concurrent),
        "-r",
        "0",
        "--agent-setup-timeout-multiplier",
        str(model.agent_setup_timeout_multiplier),
        "--ak",
        "opencode_config=" + json.dumps(model.opencode_config(), separators=(",", ":"), sort_keys=True),
    ]
    for key, value in sorted(model.agent_kwargs.items()):
        argv += ["--ak", f"{key}={value if isinstance(value, str) else json.dumps(value)}"]
    argv += [
        "--ae",
        f"{OUTPUT_CAP_VAR}={model.output_token_max}",
        "--job-name",
        job_name,
        "-o",
        str(jobs_dir),
        "--disable-verification",
        "-y",
    ]
    return argv


def resume_argv(*, binary: str, job_dir: Path, filter_error_types: list[str]) -> list[str]:
    argv = [binary, "job", "resume", "-p", str(job_dir)]
    for t in filter_error_types:
        argv += ["-f", t]
    return argv


def install_only_argv(
    *, binary: str, tasks_dir: Path, input_task: str, model: ResolvedModel, job_name: str, jobs_dir: Path
) -> list[str]:
    """Builds the task image and installs the agent without running it (image warm-up)."""
    return [
        binary,
        "run",
        "-p",
        str(tasks_dir),
        "-i",
        input_task,
        "-a",
        "opencode",
        "-m",
        model.id,
        "-k",
        "1",
        "--install-only",
        "--job-name",
        job_name,
        "-o",
        str(jobs_dir),
        "-y",
    ]


def nop_argv(*, binary: str, tasks_dir: Path, input_task: str, job_name: str, jobs_dir: Path) -> list[str]:
    """Runs the no-op agent once: proves Docker, the image and artifact capture work without any model key."""
    return [
        binary,
        "run",
        "-p",
        str(tasks_dir),
        "-i",
        input_task,
        "-a",
        "nop",
        "-k",
        "1",
        "--disable-verification",
        "--job-name",
        job_name,
        "-o",
        str(jobs_dir),
        "-y",
    ]
