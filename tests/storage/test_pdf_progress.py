from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from bson import Int64

from tests.support.mongo_fixtures import new_store
from tgdrive.core.errors import NotFoundError, TrashedItemError, ValidationError
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.pdf_progress import (
    PdfProgressError,
    page_record,
    validate_pdf_state,
)


@pytest.mark.parametrize("page,count", [(1, 1), (10_000, 10_000), (Int64(2), Int64(3))])
def test_pdf_page_record_accepts_bounded_integer_pages(page, count):
    assert page_record(page, count, 1000) == {
        "page_number": page,
        "page_count": count,
        "updated_at": 1000,
    }


@pytest.mark.parametrize(
    "page,count,updated",
    [
        (0, 1, 0),
        (2, 1, 0),
        (1, 10_001, 0),
        (True, 2, 0),
        (1, True, 0),
        (1.0, 2, 0),
        (1, 2.0, 0),
        (1, 2, float("nan")),
        (1, 2, float("inf")),
        (1, 2, -1),
        (1, 2, True),
    ],
)
def test_pdf_page_record_rejects_invalid_metadata(page, count, updated):
    with pytest.raises(ValidationError):
        page_record(page, count, updated)


def pdf_node():
    return {
        "kind": "file",
        "pdf_progress": page_record(2, 3, 1000),
        "pdf_progress_session": {
            "id": "a" * 32,
            "sequence": 1,
            "started_at": 999,
            "saved_at": 1000,
            "closed": False,
        },
    }


@pytest.mark.parametrize(
    "change", ["folder", "extra", "missing", "writer", "orphan", "sequence", "unused"]
)
def test_pdf_state_rejects_invalid_raw_recovery_fields(change):
    node = copy.deepcopy(pdf_node())
    if change == "folder":
        node["kind"] = "folder"
    elif change == "extra":
        node["pdf_progress"]["token"] = "private"
    elif change == "missing":
        del node["pdf_progress"]["page_count"]
    elif change == "writer":
        node["pdf_progress_session"]["id"] = "bad"
    elif change == "orphan":
        del node["pdf_progress"]
    elif change == "sequence":
        node["pdf_progress_session"]["sequence"] = True
    else:
        node["pdf_progress_session"]["sequence"] = 0
    with pytest.raises(ValidationError):
        validate_pdf_state(node)


def test_pdf_state_accepts_optional_progress_and_an_unused_writer():
    validate_pdf_state({"kind": "folder"})
    node = pdf_node()
    validate_pdf_state(node)
    del node["pdf_progress"]
    node["pdf_progress_session"].update(sequence=0, saved_at=None)
    validate_pdf_state(node)


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(
        "tgdrive.storage.mongo_store.time", SimpleNamespace(time=lambda: clock[0])
    )
    store = new_store(tmp_path / "drive")
    identity = store.add_file("/", "reading.pdf", 7, 3)["id"]
    try:
        yield store, identity, clock
    finally:
        store.close()


def test_pdf_writes_are_ordered_paced_idempotent_and_do_not_bump_revision(
    pdf, monkeypatch
):
    store, identity, clock = pdf
    revision = store.revision
    assert store.pdf_progress(identity) is None
    first = store.begin_pdf_progress(identity)
    writer = first["session_id"]
    assert first["progress"] is None
    with pytest.raises(PdfProgressError) as paced:
        store.begin_pdf_progress(identity)
    assert (paced.value.code, paced.value.retry_after) == (
        "PDF_PROGRESS_RATE_LIMITED",
        1,
    )
    writes = []
    update = store.db.nodes.update_one

    def observe(*args, **kwargs):
        writes.append(args)
        return update(*args, **kwargs)

    monkeypatch.setattr(store.db.nodes, "update_one", observe)
    saved = store.save_pdf_progress(identity, writer, 1, 2, 3)
    with pytest.raises(PdfProgressError) as paced:
        store.save_pdf_progress(identity, writer, 2, 3, 3)
    assert (paced.value.code, paced.value.retry_after) == (
        "PDF_PROGRESS_RATE_LIMITED",
        5,
    )
    assert store.pdf_progress(identity) == saved and len(writes) == 1
    final = store.save_pdf_progress(identity, writer, 2, 3, 3, final=True)
    assert len(writes) == 2
    assert store.save_pdf_progress(identity, writer, 2, 3, 3, final=True) == final
    assert len(writes) == 2
    with pytest.raises(PdfProgressError) as stale:
        store.save_pdf_progress(identity, writer, 1, 1, 3)
    assert stale.value.code == "PDF_PROGRESS_SEQUENCE_STALE"
    with pytest.raises(PdfProgressError) as closed:
        store.save_pdf_progress(identity, writer, 3, 1, 3)
    assert closed.value.code == "PDF_PROGRESS_SESSION_CHANGED"
    clock[0] += 1
    second = store.begin_pdf_progress(identity)
    assert second["progress"] == final
    with pytest.raises(PdfProgressError):
        store.save_pdf_progress(identity, writer, 99, 1, 3, final=True)
    assert (
        store.save_pdf_progress(identity, second["session_id"], 1, 1, 3)["page_number"]
        == 1
    )
    assert store.revision == revision


def test_pdf_move_trash_delete_and_writer_fencing(pdf, monkeypatch):
    store, identity, _clock = pdf
    writer = store.begin_pdf_progress(identity)["session_id"]
    saved = store.save_pdf_progress(identity, writer, 1, 2, 3)
    folder = store.create_folder("/", "Moved")
    store.move("/" + identity, "/" + folder["id"])
    assert store.pdf_progress(identity) == saved
    store.set_trashed("/" + folder["id"], True)
    with pytest.raises(TrashedItemError):
        store.pdf_progress(identity)
    store.set_trashed("/" + folder["id"], False)
    before = copy.deepcopy(store.db.nodes.find_one({"_id": identity}))

    def lost(_session):
        raise DatabaseUnavailable("Owner changed")

    with monkeypatch.context() as changes:
        changes.setattr(store.backend, "fence", lost)
        with pytest.raises(DatabaseUnavailable):
            store.save_pdf_progress(identity, writer, 2, 3, 3, final=True)
    assert store.db.nodes.find_one({"_id": identity}) == before
    store.set_trashed("/" + identity, True)
    deletion = store.begin_deletion("/" + identity, channel_id=-1001234567890)
    store.finish_deletion(deletion["id"])
    with pytest.raises(NotFoundError):
        store.begin_pdf_progress(identity)
    assert store.db.nodes.find_one({"_id": identity}) is None
