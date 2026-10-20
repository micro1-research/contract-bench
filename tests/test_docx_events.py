from pathlib import Path

from contractbench import docx_events as de
from tests.conftest import dele, ins, make_docx, para

A = "Reviewing Counsel (Acme)"
B = "Beta Legal"


def _events(body: str, author=A):
    root = de.load_document(make_docx(Path("/tmp") / "x.docx", body)) if False else None  # placeholder, replaced below
    return root


def events_of(tmp_path: Path, body: str, author=A):
    p = make_docx(tmp_path / "doc.docx", body)
    root = de.load_document(p)
    out = []
    for i, par in enumerate(de.paragraphs(root)):
        evs, _, _ = de.paragraph_events(par, i, author)
        out.extend(evs)
    return out, de.profile(p, author)


def test_replace_pair_is_one_event_sized_by_the_larger_half(tmp_path: Path):
    text = "x" * 100
    evs, prof = events_of(tmp_path, para(text, dele("abc"), ins("abcdefghij")))
    assert len(evs) == 1 and evs[0].kind == "replace" and evs[0].size == 10 and evs[0].untouched == 100
    assert evs[0].inline  # 10 < 0.3 x 100
    assert prof.events == 1 and prof.ops == 1 and prof.clusters == 1 and prof.touched == 1


def test_block_when_large_or_no_untouched_text(tmp_path: Path):
    evs, _ = events_of(tmp_path, para("short", ins("a" * 50)))
    assert evs[0].kind == "insert" and not evs[0].inline
    evs, _ = events_of(tmp_path, "<w:p>" + ins("brand new paragraph") + "</w:p>")
    assert evs[0].untouched == 0 and not evs[0].inline


def test_pending_delete_flushed_by_plain_run_and_other_author(tmp_path: Path):
    body = para("y" * 100, dele("aaa") + "<w:r><w:t>plain</w:t></w:r>" + ins("bbb"))
    evs, _ = events_of(tmp_path, body)
    assert [e.kind for e in evs] == ["delete", "insert"]
    body = para("y" * 100, dele("aaa") + ins("bbb", B))  # another author's insert does not pair
    evs, _ = events_of(tmp_path, body)
    assert [e.kind for e in evs] == ["delete"]
    evs_b, _ = events_of(tmp_path, body, author=B)
    assert [e.kind for e in evs_b] == ["insert"]


def test_transparent_markers_do_not_break_pairs(tmp_path: Path):
    body = para("z" * 100, dele("aaa") + '<w:commentRangeStart w:id="0"/>' + ins("bbb"))
    evs, _ = events_of(tmp_path, body)
    assert [e.kind for e in evs] == ["replace"]


def test_nested_revision_counts_as_touched_but_not_as_event(tmp_path: Path):
    body = "<w:p><w:r><w:t>" + "q" * 50 + "</w:t></w:r><w:hyperlink>" + ins("link text") + "</w:hyperlink></w:p>" + para("other")
    evs, prof = events_of(tmp_path, body)
    assert evs == [] and prof.touched == 1 and prof.clusters == 1 and prof.events == 0


def test_untouched_length_excludes_revision_text_and_tables_are_walked(tmp_path: Path):
    body = "<w:tbl><w:tr><w:tc>" + para("cell text", ins("add")) + "</w:tc></w:tr></w:tbl>" + para("body")
    p = make_docx(tmp_path / "t.docx", body)
    root = de.load_document(p)
    pars = de.paragraphs(root)
    assert len(pars) == 2 and de.untouched_length(pars[0]) == len("cell text")
    prof = de.profile(p, A)
    assert prof.events == 1 and prof.n_paragraphs == 2


def test_clusters_and_reference_author(tmp_path: Path):
    assert de.clusters([True, True, False, True, False, False, True]) == 3
    assert de.clusters([]) == 0
    base = make_docx(tmp_path / "base.docx", para("one", ins("cp", B)) + para("two"))
    ref = make_docx(
        tmp_path / "ref.docx", para("one", ins("cp", B)) + para("two", ins("mine", "Acme Legal"), ins("more", "Acme Legal"))
    )
    assert de.reference_author(ref, base) == "Acme Legal"
    assert de.revision_authors(ref) == {B: 1, "Acme Legal": 2}
