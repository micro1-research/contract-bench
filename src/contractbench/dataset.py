"""The dataset: where it is, how to fetch it, what is in it.

Resolution order: an explicit path, then `$CONTRACTBENCH_DIR`, then `./benchmark`. `fetch` downloads the Hugging
Face dataset (full runnable Harbor tasks, so nothing has to be rehydrated). `index` reads every `task.toml` with the
standard library only. `validate` checks that the download is usable: no Git LFS pointer files, every document opens,
and the protocol's assumption holds that a scenario's five Turn 1 tasks share one byte-identical input.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
REQUIRED_FILES = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "environment/app/contract.docx",
    "tests/judge.py",
    "tests/rubrics.json",
    "tests/baseline/contract.docx",
)
FETCH_RECORD = ".contractbench-fetch.json"


@dataclass(frozen=True)
class TaskInfo:
    name: str
    path: Path
    scenario: str
    turn: int
    attorney: str
    responds_to: str
    split: str
    side: str
    author: str
    represented_party: str
    counterparty: str
    expert_id: int
    total_weight: int
    n_rubrics: int
    agreement_type: str
    jurisdiction: str
    playbook: str

    @property
    def input_task(self) -> str:
        """The task whose input this task shares: Turn 1 siblings map to the scenario's `-t1-a` task."""
        return f"{self.scenario}-t1-a" if self.turn == 1 else self.name

    @property
    def tests_dir(self) -> Path:
        return self.path / "tests"

    @property
    def input_docx(self) -> Path:
        return self.path / "environment" / "app" / "contract.docx"

    @property
    def baseline_docx(self) -> Path:
        return self.path / "tests" / "baseline" / "contract.docx"

    @property
    def rubrics_path(self) -> Path:
        return self.path / "tests" / "rubrics.json"


@dataclass
class Dataset:
    root: Path
    tasks: dict[str, TaskInfo]
    fetch_record: dict = field(default_factory=dict)

    # ---- selection -------------------------------------------------------------------------------------------- #
    def subset(self, which: str = "all") -> list[TaskInfo]:
        if which in ("all", "full"):
            return sorted(self.tasks.values(), key=lambda t: t.name)
        if which not in ("public", "private"):
            raise ValueError(f"unknown set {which!r}: use public, private or full")
        return sorted((t for t in self.tasks.values() if t.split == which), key=lambda t: t.name)

    def inputs(self, which: str = "all", globs: list[str] | None = None) -> list[TaskInfo]:
        """One task per distinct agent input: the `-t1-a` task of each scenario and every Turn 2 task."""
        import fnmatch

        chosen = [t for t in self.subset(which) if t.name == t.input_task]
        if globs:
            chosen = [t for t in chosen if any(fnmatch.fnmatch(t.name, g) for g in globs)]
        return chosen

    def siblings(self, input_task: str) -> list[TaskInfo]:
        """The tasks graded from one input: five rubric sets for a Turn 1 input, one for Turn 2."""
        t = self.tasks[input_task]
        if t.turn == 2:
            return [t]
        return sorted((x for x in self.tasks.values() if x.scenario == t.scenario and x.turn == 1), key=lambda x: x.name)

    def scenarios(self) -> dict[str, list[TaskInfo]]:
        out: dict[str, list[TaskInfo]] = {}
        for t in sorted(self.tasks.values(), key=lambda t: t.name):
            out.setdefault(t.scenario, []).append(t)
        return out

    def registry_count(self) -> int:
        """Number of tasks the dataset's registry.json lists (0 when there is none): the size of the full set."""
        reg = self.root / "registry.json"
        if not reg.exists():
            return 0
        try:
            return sum(len(e.get("tasks", [])) for e in json.loads(reg.read_text()))
        except (ValueError, AttributeError, TypeError):
            return 0

    def is_complete(self) -> bool:
        """True when every task the registry lists is present (a pattern fetch is a subset)."""
        n = self.registry_count()
        return n == 0 or n == len(self.tasks)

    def reference(self, task: str) -> Path | None:
        p = self.root / "references" / f"{task}.docx"
        return p if p.exists() else None

    def rubrics(self, task: str) -> dict:
        return json.loads(self.tasks[task].rubrics_path.read_text())

    # ---- identity ---------------------------------------------------------------------------------------------- #
    def digest(self) -> str:
        """A fingerprint of the task set: names, task.toml and rubrics. Recorded in run.json."""
        h = hashlib.sha256()
        for t in sorted(self.tasks.values(), key=lambda t: t.name):
            h.update(t.name.encode())
            h.update(sha256(t.path / "task.toml").encode())
            h.update(sha256(t.rubrics_path).encode())
        return h.hexdigest()

    def describe(self) -> dict:
        return {
            "root": str(self.root),
            "repo_id": self.fetch_record.get("repo_id"),
            "revision": self.fetch_record.get("revision"),
            "commit_sha": self.fetch_record.get("commit_sha"),
            "n_tasks": len(self.tasks),
            "n_scenarios": len(self.scenarios()),
            "splits": sorted({t.split for t in self.tasks.values()}),
            "task_digest": self.digest(),
        }


# ---- helpers ----------------------------------------------------------------------------------------------------- #
def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_lfs_pointer(p: Path) -> bool:
    try:
        if p.stat().st_size > 1024:
            return False
        with open(p, "rb") as f:
            return f.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def resolve(explicit: str | os.PathLike | None = None) -> Path:
    """Find the dataset directory without touching the network."""
    candidates = [Path(explicit)] if explicit else []
    if os.environ.get("CONTRACTBENCH_DIR"):
        candidates.append(Path(os.environ["CONTRACTBENCH_DIR"]))
    candidates.append(Path("benchmark"))
    for c in candidates:
        c = c.expanduser()
        if (c / "tasks").is_dir():
            return c.resolve()
    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"no dataset found (tried {tried}); run `contractbench fetch` or set CONTRACTBENCH_DIR")


def fetch(
    dest: Path | None = None,
    revision: str | None = paths.DEFAULT_REVISION,
    patterns: list[str] | None = None,
    repo_id: str = paths.HF_REPO_ID,
    token: str | None = None,
) -> Path:
    """Download the dataset (or a subset by glob) from Hugging Face into `dest` and record what was fetched."""
    from huggingface_hub import HfApi, snapshot_download

    dest = (dest or paths.benchmark_dir()).expanduser().resolve()
    token = token or os.environ.get("HF_TOKEN") or None
    info = HfApi(token=token).dataset_info(repo_id, revision=revision)
    snapshot_download(
        repo_id=repo_id, repo_type="dataset", revision=revision, local_dir=str(dest), allow_patterns=patterns, token=token
    )
    record = {
        "repo_id": repo_id,
        "revision": revision,
        "commit_sha": info.sha,
        "patterns": patterns,
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    (dest / FETCH_RECORD).write_text(json.dumps(record, indent=1) + "\n")
    return dest


def _task_info(task_dir: Path) -> TaskInfo:
    cfg = tomllib.loads((task_dir / "task.toml").read_text())
    m = cfg.get("metadata") or {}
    name = task_dir.name
    declared = (cfg.get("task") or {}).get("name", "")
    if declared.split("/")[-1] != name:
        raise ValueError(f"{task_dir}: [task].name {declared!r} does not match the directory name")
    missing = [k for k in ("scenario", "turn", "attorney", "split", "side", "author") if k not in m]
    if missing:
        raise ValueError(f"{task_dir}: task.toml metadata lacks {missing}")
    return TaskInfo(
        name=name,
        path=task_dir,
        scenario=str(m["scenario"]),
        turn=int(m["turn"]),
        attorney=str(m["attorney"]),
        responds_to=str(m.get("responds_to", "")),
        split=str(m["split"]),
        side=str(m["side"]),
        author=str(m["author"]),
        represented_party=str(m.get("represented_party", "")),
        counterparty=str(m.get("counterparty", "")),
        expert_id=int(m.get("expert_id", 0)),
        total_weight=int(m.get("total_weight", 0)),
        n_rubrics=int(m.get("n_rubrics", 0)),
        agreement_type=str(m.get("agreement_type", "")),
        jurisdiction=str(m.get("jurisdiction", "")),
        playbook=str(m.get("playbook", "")),
    )


def index(root: Path) -> Dataset:
    """Read every task directory under `root/tasks`."""
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"{root} has no tasks/ directory")
    tasks = {}
    for d in sorted(p for p in tasks_dir.iterdir() if p.is_dir() and (p / "task.toml").exists()):
        tasks[d.name] = _task_info(d)
    if not tasks:
        raise FileNotFoundError(f"{tasks_dir} holds no tasks")
    rec = {}
    if (root / FETCH_RECORD).exists():
        rec = json.loads((root / FETCH_RECORD).read_text())
    return Dataset(root=root, tasks=tasks, fetch_record=rec)


def load(explicit: str | os.PathLike | None = None) -> Dataset:
    return index(resolve(explicit))


def validate(ds: Dataset, deep: bool = True) -> list[str]:
    """Problems that make the dataset unusable for the protocol. Empty list means fine."""
    problems: list[str] = []
    reg = ds.root / "registry.json"
    if reg.exists() and not ds.fetch_record.get("patterns"):  # a pattern fetch holds a chosen subset on purpose
        try:
            for entry in json.loads(reg.read_text()):
                for t in entry.get("tasks", []):
                    if not (ds.root / t["path"]).is_dir():
                        problems.append(f"registry.json lists {t['path']} which is missing")
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f"registry.json unreadable: {e}")
    for t in ds.tasks.values():
        for rel in REQUIRED_FILES:
            p = t.path / rel
            if not p.exists():
                problems.append(f"{t.name}: missing {rel}")
            elif is_lfs_pointer(p):
                problems.append(
                    f"{t.name}: {rel} is a Git LFS pointer file, not the document (install git-lfs or use `contractbench fetch`)"
                )
        if deep:
            for rel in ("environment/app/contract.docx", "tests/baseline/contract.docx"):
                p = t.path / rel
                if p.exists() and not is_lfs_pointer(p):
                    try:
                        if zipfile.ZipFile(p).testzip() is not None:
                            problems.append(f"{t.name}: {rel} is a corrupt document")
                    except zipfile.BadZipFile:
                        problems.append(f"{t.name}: {rel} is not a Word document")
        if t.turn == 2 and t.responds_to and t.responds_to not in ds.tasks:
            problems.append(f"{t.name}: responds_to {t.responds_to} is not in the dataset")
    # the protocol's assumption: every scenario present with Turn 1 tasks has five that share one input
    for scenario, ts in ds.scenarios().items():
        t1 = [t for t in ts if t.turn == 1]
        if t1 and len(t1) != 5:  # Turn 2 tasks are independent inputs; only the Turn 1 sibling set must be complete
            problems.append(f"{scenario}: {len(t1)} Turn 1 tasks (expected 5)")
        if t1 and f"{scenario}-t1-a" not in ds.tasks:
            problems.append(f"{scenario}: no -t1-a task to run as the Turn 1 input")
        if deep and len(t1) > 1:
            digests = set()
            for t in t1:
                h = hashlib.sha256()
                h.update(sha256(t.path / "instruction.md").encode())
                env = t.path / "environment"
                for f in sorted(p for p in env.rglob("*") if p.is_file()):
                    h.update(str(f.relative_to(env)).encode())
                    h.update(sha256(f).encode())
                digests.add(h.hexdigest())
            if len(digests) != 1:
                problems.append(
                    f"{scenario}: the five Turn 1 tasks do not share one input; the one-rollout-per-input "
                    f"protocol cannot be applied"
                )
    return problems
