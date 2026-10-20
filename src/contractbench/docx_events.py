"""Tracked-change events in a Word document, for the surgicality and edit-volume metrics.

Definitions (the report's Equations 1 to 3):
- A paragraph's *untouched length* u_p is the number of characters in `w:t` runs that sit outside any revision
  wrapper (`w:ins`, `w:del`, `w:moveFrom`, `w:moveTo`), at any depth.
- Walking a paragraph's direct children in order, a `w:del` (or `w:moveFrom`) immediately followed by a `w:ins`
  (or `w:moveTo`) by the same author is one *replace* event; comment markers, bookmarks and proofing flags between
  them are transparent; any other element, or a revision by another author, flushes a pending deletion as its own
  event. An unpaired insertion or deletion is one event. An event's size is max(deleted chars, inserted chars).
- An event is *inline* when size < threshold x u_p (threshold 0.30); when u_p is 0 it is block.
- A paragraph is *touched* when any descendant revision carries the actor's author string; a *cluster* (a
  "redlined passage") is a maximal run of consecutive touched paragraphs in document order, tables included.

Only `lxml` is needed. Authors are matched by substring, so "Reviewing Counsel" selects a model's marks whatever
party it represented.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
INLINE_THRESHOLD = 0.30
REVISION = {"ins", "del", "moveFrom", "moveTo"}
DELETIONS = {"del", "moveFrom"}
INSERTIONS = {"ins", "moveTo"}
TRANSPARENT = {"commentRangeStart", "commentRangeEnd", "commentReference", "bookmarkStart", "bookmarkEnd", "proofErr"}


def qn(local: str) -> str:
    return f"{{{W}}}{local}"


def local(el) -> str:
    return etree.QName(el).localname


def load_document(docx: Path):
    try:
        with zipfile.ZipFile(docx) as z:
            return etree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, OSError, etree.XMLSyntaxError):
        return None


def paragraphs(root) -> list:
    return list(root.iter(qn("p")))


def untouched_length(p) -> int:
    total = 0

    def walk(node, inside: bool) -> None:
        nonlocal total
        tag = local(node)
        if tag == "t" and not inside:
            total += len(node.text or "")
            return
        nxt = inside or tag in REVISION
        for child in node.iterchildren():
            if isinstance(child.tag, str):
                walk(child, nxt)

    walk(p, False)
    return total


def inserted_chars(el) -> int:
    return sum(len(t.text or "") for t in el.iter(qn("t")))


def deleted_chars(el) -> int:
    return sum(len(t.text or "") for t in el.iter(qn("delText")))


def author_of(el) -> str:
    return el.get(qn("author"), "") or ""


@dataclass
class Event:
    paragraph: int
    author: str
    kind: str  # insert | delete | replace
    deleted: int
    inserted: int
    size: int
    untouched: int
    inline: bool


def paragraph_events(
    p, index: int, author_contains: str | None, threshold: float = INLINE_THRESHOLD, untouched: int | None = None
) -> tuple[list[Event], int, int]:
    """Events of one paragraph for the actor; also returns (n_insertions, n_deletions) seen for the actor."""
    u = untouched_length(p) if untouched is None else untouched
    events: list[Event] = []
    pending: tuple[int, str] | None = None
    n_ins = n_del = 0

    def emit(kind: str, author: str, deleted: int, inserted: int) -> None:
        size = max(deleted, inserted)
        inline = u > 0 and size / u < threshold
        events.append(Event(index, author, kind, deleted, inserted, size, u, inline))

    for child in p.iterchildren():
        if not isinstance(child.tag, str):
            continue
        tag = local(child)
        if tag in TRANSPARENT:
            continue
        if tag not in REVISION:
            if pending is not None:
                emit("delete", pending[1], pending[0], 0)
                pending = None
            continue
        author = author_of(child)
        if author_contains is not None and author_contains not in author:
            if pending is not None:
                emit("delete", pending[1], pending[0], 0)
                pending = None
            continue
        if tag in DELETIONS:
            if pending is not None:
                emit("delete", pending[1], pending[0], 0)
            n_del += 1
            pending = (deleted_chars(child), author)
        else:
            n_ins += 1
            ins = inserted_chars(child)
            if pending is not None and pending[1] == author:
                emit("replace", author, pending[0], ins)
            else:
                if pending is not None:
                    emit("delete", pending[1], pending[0], 0)
                emit("insert", author, 0, ins)
            pending = None
    if pending is not None:
        emit("delete", pending[1], pending[0], 0)
    return events, n_ins, n_del


def touched(p, author_contains: str | None) -> bool:
    for el in p.iter():
        if isinstance(el.tag, str) and local(el) in REVISION:
            if author_contains is None or author_contains in author_of(el):
                return True
    return False


def clusters(flags: list[bool]) -> int:
    n, prev = 0, False
    for f in flags:
        if f and not prev:
            n += 1
        prev = f
    return n


@dataclass
class DocProfile:
    events: int
    inline: int
    block: int
    clusters: int
    touched: int
    ops: int
    avg_edit_len: float
    total_chars: int
    n_paragraphs: int
    insert_lengths: list[int] = field(default_factory=list)
    touched_idx: list[int] = field(default_factory=list)

    @property
    def inline_share(self) -> float | None:
        return self.inline / self.events if self.events else None

    def to_dict(self) -> dict:
        return {
            "events": self.events,
            "inline": self.inline,
            "block": self.block,
            "inline_share": self.inline_share,
            "clusters": self.clusters,
            "touched": self.touched,
            "ops": self.ops,
            "avg_edit_len": self.avg_edit_len,
            "total_chars": self.total_chars,
            "n_paragraphs": self.n_paragraphs,
            "median_insertion_chars": (sorted(self.insert_lengths)[len(self.insert_lengths) // 2] if self.insert_lengths else 0),
        }


def profile(docx: Path, author_contains: str | None, threshold: float = INLINE_THRESHOLD) -> DocProfile | None:
    root = load_document(docx)
    if root is None:
        return None
    evs: list[Event] = []
    flags: list[bool] = []
    ops = 0
    ins_lengths: list[int] = []
    for i, p in enumerate(paragraphs(root)):
        e, n_ins, n_del = paragraph_events(p, i, author_contains, threshold)
        evs.extend(e)
        ops += n_ins + n_del - sum(1 for x in e if x.kind == "replace")
        ins_lengths.extend(x.inserted for x in e if x.kind in ("insert", "replace"))
        flags.append(touched(p, author_contains))
    sizes = [x.size for x in evs]
    n_inline = sum(1 for x in evs if x.inline)
    return DocProfile(
        events=len(evs),
        inline=n_inline,
        block=len(evs) - n_inline,
        clusters=clusters(flags),
        touched=sum(flags),
        ops=ops,
        avg_edit_len=mean(sizes) if sizes else 0.0,
        total_chars=sum(sizes),
        n_paragraphs=len(flags),
        insert_lengths=ins_lengths,
        touched_idx=[i for i, f in enumerate(flags) if f],
    )


def revision_authors(docx: Path) -> dict[str, int]:
    """Author -> number of revision marks in the document body."""
    root = load_document(docx)
    out: dict[str, int] = {}
    if root is None:
        return out
    for el in root.iter():
        if isinstance(el.tag, str) and local(el) in REVISION:
            a = author_of(el)
            out[a] = out.get(a, 0) + 1
    return out


def reference_author(reference: Path, baseline: Path) -> str:
    """The author of a reference redline's own marks: the most frequent author absent from the input document
    (Turn 2 inputs already carry the counterparty's marks)."""
    ra, ba = revision_authors(reference), revision_authors(baseline)
    new = {a: n for a, n in ra.items() if a not in ba} or dict(ra)
    return max(new, key=new.get) if new else ""
