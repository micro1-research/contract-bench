from pathlib import Path

import pytest

from contractbench import dataset as dsmod
from tests.conftest import make_docx, make_task, para


def test_index_and_protocol_maps(dataset_dir: Path):
    ds = dsmod.index(dataset_dir)
    assert len(ds.tasks) == 13
    assert [t.name for t in ds.inputs("public")] == ["msa-acme-beta-t1-a", "msa-acme-beta-t2-a", "msa-acme-beta-t2-b"]
    assert [t.name for t in ds.siblings("msa-acme-beta-t1-a")] == [f"msa-acme-beta-t1-{x}" for x in "abcde"]
    assert [t.name for t in ds.siblings("msa-acme-beta-t2-b")] == ["msa-acme-beta-t2-b"]
    assert ds.tasks["msa-acme-beta-t1-c"].input_task == "msa-acme-beta-t1-a"
    assert ds.tasks["ip-gamma-delta-t1-a"].split == "private"
    assert ds.inputs("private", ["*-t2-*"]) and all(t.turn == 2 for t in ds.inputs("private", ["*-t2-*"]))
    assert ds.reference("msa-acme-beta-t1-b") is not None and ds.reference("nope") is None
    assert len(ds.digest()) == 64


def test_validate_clean(dataset_dir: Path):
    assert dsmod.validate(dsmod.index(dataset_dir), deep=True) == []


def test_validate_detects_pointer_and_divergent_siblings(dataset_dir: Path):
    ds = dsmod.index(dataset_dir)
    t = ds.tasks["msa-acme-beta-t1-b"]
    t.input_docx.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n")
    problems = dsmod.validate(ds, deep=True)
    assert any("LFS pointer" in p for p in problems)
    assert any("do not share one input" in p for p in problems)


def test_validate_requires_five_siblings(tmp_path: Path):
    root = tmp_path / "b"
    for letter in "abc":
        make_task(root, f"lease-x-y-t1-{letter}", "lease-x-y", 1, letter.upper())
    problems = dsmod.validate(dsmod.index(root), deep=True)
    assert any("3 Turn 1 tasks" in p for p in problems)


def test_resolve_order(dataset_dir: Path, monkeypatch, tmp_path: Path):
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    with pytest.raises(FileNotFoundError):
        dsmod.resolve()
    monkeypatch.setenv("CONTRACTBENCH_DIR", str(dataset_dir))
    assert dsmod.resolve() == dataset_dir.resolve()
    assert dsmod.resolve(dataset_dir) == dataset_dir.resolve()


def test_task_name_must_match_directory(tmp_path: Path):
    d = make_task(tmp_path / "b", "msa-a-b-t1-a", "msa-a-b", 1, "A")
    (d / "task.toml").write_text(
        (d / "task.toml").read_text().replace('name = "contractbench/msa-a-b-t1-a"', 'name = "contractbench/other"')
    )
    with pytest.raises(ValueError):
        dsmod.index(tmp_path / "b")


def test_corrupt_docx_is_reported(tmp_path: Path):
    root = tmp_path / "b"
    for letter in "abcde":
        make_task(root, f"msa-a-b-t1-{letter}", "msa-a-b", 1, letter.upper())
    (root / "tasks" / "msa-a-b-t1-a" / "environment" / "app" / "contract.docx").write_bytes(b"not a zip at all, long enough")
    problems = dsmod.validate(dsmod.index(root), deep=True)
    assert any("not a Word document" in p for p in problems)
    make_docx(root / "tasks" / "msa-a-b-t1-a" / "environment" / "app" / "contract.docx", para("x"))
