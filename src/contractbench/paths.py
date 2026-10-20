"""Where things live. Every directory can be moved with an environment variable; defaults are relative to the
current working directory, which the documentation assumes is the repository root."""

from __future__ import annotations

import os
from pathlib import Path

HF_REPO_ID = "micro1-inc/ContractBench"
DEFAULT_REVISION = "v1.0"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def configs_dir() -> Path:
    return repo_root() / "configs"


def benchmark_dir() -> Path:
    return Path(os.environ.get("CONTRACTBENCH_DIR") or "benchmark").expanduser().resolve()


def jobs_dir() -> Path:
    return Path(os.environ.get("CONTRACTBENCH_JOBS_DIR") or "jobs").expanduser().resolve()


def runs_dir() -> Path:
    return Path(os.environ.get("CONTRACTBENCH_RUNS_DIR") or "runs").expanduser().resolve()


def run_dir(run_id: str) -> Path:
    return runs_dir() / run_id
