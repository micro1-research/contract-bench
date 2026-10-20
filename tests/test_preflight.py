from pathlib import Path

from contractbench.cli import main


def test_preflight_without_harbor(workdir: Path, dataset_dir: Path, monkeypatch):
    """The manual smoke workflow runs preflight on a machine without Harbor, Docker or keys."""
    monkeypatch.setenv("CONTRACTBENCH_DIR", str(dataset_dir))
    monkeypatch.delenv("HARBOR_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert main(["preflight", "--no-docker", "--no-keys", "--no-harbor"]) == 0
    assert main(["preflight", "--no-docker", "--no-keys"]) == 1  # Harbor missing is a failure unless skipped


def test_preflight_build_requires_harbor(workdir: Path, dataset_dir: Path, monkeypatch):
    monkeypatch.setenv("CONTRACTBENCH_DIR", str(dataset_dir))
    assert main(["preflight", "--no-docker", "--no-keys", "--no-harbor", "--build"]) == 1
