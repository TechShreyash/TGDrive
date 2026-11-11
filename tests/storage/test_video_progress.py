from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from bson import Int64

from tests.support.mongo_fixtures import new_store
from tgdrive.core.errors import NotFoundError, TrashedItemError, ValidationError
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.video_progress import VideoProgressError, validate_video_state


@pytest.fixture
def video(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(
        "tgdrive.storage.mongo_store.time", SimpleNamespace(time=lambda: clock[0])
    )
    store = new_store(tmp_path / "drive")
    item = store.add_file("/", "movie.mp4", 7, 3)
    try:
        yield store, item["id"], clock
    finally:
        store.close()


def test_writer_ordering_final_save_and_duplicates_preserve_latest(video, monkeypatch):
    store, identity, clock = video
    revision = store.revision
    assert store.video_progress(identity) is None
    first = store.begin_video_progress(identity)
    assert first["progress"] is None
    saves = []
    update = store.db.nodes.update_one

    def observe(*args, **kwargs):
        saves.append(args)
        return update(*args, **kwargs)

    monkeypatch.setattr(store.db.nodes, "update_one", observe)
    saved = store.save_video_progress(identity, first["session_id"], 1, 40, 200)
    with pytest.raises(VideoProgressError) as limited:
        store.save_video_progress(identity, first["session_id"], 2, 41, 200)
    assert (limited.value.status, limited.value.retry_after) == (429, 5)
    assert store.video_progress(identity) == saved and len(saves) == 1
    closed = store.save_video_progress(
        identity, first["session_id"], 2, 41, 200, final=True
    )
    assert len(saves) == 2
    clock[0] += 10
    assert (
        store.save_video_progress(identity, first["session_id"], 2, 41, 200, final=True)
        == closed
    )
    assert len(saves) == 2
    with pytest.raises(VideoProgressError, match="newer video position"):
        store.save_video_progress(identity, first["session_id"], 1, 40, 200)
    with pytest.raises(VideoProgressError) as ended:
        store.save_video_progress(identity, first["session_id"], 3, 42, 200)
    assert ended.value.code == "VIDEO_PROGRESS_SESSION_CHANGED"
    second = store.begin_video_progress(identity)
    assert second["progress"] == closed and second["session_id"] != first["session_id"]
    store.save_video_progress(identity, second["session_id"], 1, 80, 200)
    with pytest.raises(VideoProgressError) as replaced:
        store.save_video_progress(
            identity, first["session_id"], 100, 12, 200, final=True
        )
    assert replaced.value.code == "VIDEO_PROGRESS_SESSION_CHANGED"
    assert store.video_progress(identity)["position_seconds"] == 80
    assert store.revision == revision


def test_session_creation_is_paced_and_read_does_not_replace_writer(video):
    store, identity, clock = video
    first = store.begin_video_progress(identity)
    with pytest.raises(VideoProgressError) as limited:
        store.begin_video_progress(identity)
    assert limited.value.retry_after == 1
    assert store.video_progress(identity) is None
    clock[0] += 1
    reopened = MongoStore(store.backend)
    try:
        second = reopened.begin_video_progress(identity)
        with pytest.raises(VideoProgressError):
            store.save_video_progress(identity, first["session_id"], 1, 10, 200)
        reopened.save_video_progress(identity, second["session_id"], 1, 20, 200)
        assert store.video_progress(identity)["position_seconds"] == 20
    finally:
        reopened.close()


def test_progress_survives_move_and_trash_restore_and_dies_with_node(video):
    store, identity, _clock = video
    token = store.begin_video_progress(identity)["session_id"]
    store.save_video_progress(identity, token, 1, 20, 200)
    folder = store.create_folder("/", "Folder")
    store.move("/" + identity, "/" + folder["id"])
    assert store.video_progress(identity)["position_seconds"] == 20
    store.set_trashed("/" + folder["id"], True)
    with pytest.raises(TrashedItemError):
        store.video_progress(identity)
    store.set_trashed("/" + folder["id"], False)
    assert store.video_progress(identity)["position_seconds"] == 20
    store.set_trashed("/" + identity, True)
    intent = store.begin_deletion("/" + identity, channel_id=-1001234567890)
    store.finish_deletion(intent["id"])
    assert store.db.nodes.find_one({"_id": identity}) is None
    with pytest.raises(NotFoundError):
        store.begin_video_progress(identity)


def test_save_cannot_outlive_ownership_or_create_an_orphan(video, monkeypatch):
    store, identity, _clock = video
    token = store.begin_video_progress(identity)["session_id"]
    before = copy.deepcopy(store.db.nodes.find_one({"_id": identity}))

    def lost(_session):
        raise DatabaseUnavailable("Owner changed")

    monkeypatch.setattr(store.backend, "fence", lost)
    with pytest.raises(DatabaseUnavailable):
        store.save_video_progress(identity, token, 1, 20, 200)
    assert store.db.nodes.find_one({"_id": identity}) == before
    with pytest.raises(NotFoundError):
        store.begin_video_progress("missing-file")
    assert store.db.nodes.find_one({"_id": "missing-file"}) is None


@pytest.mark.parametrize(
    "position,duration",
    [
        (float("nan"), 200),
        (float("inf"), 200),
        (-1, 200),
        (20, 0),
        (201, 200),
        (True, 200),
        (20, 10**400),
    ],
)
def test_invalid_seconds_never_replace_progress(video, position, duration):
    store, identity, _clock = video
    token = store.begin_video_progress(identity)["session_id"]
    with pytest.raises(ValidationError):
        store.save_video_progress(identity, token, 1, position, duration)
    assert store.video_progress(identity) is None


def test_zero_and_bson_integer_sequences_are_readable(video):
    store, identity, _clock = video
    token = store.begin_video_progress(identity)["session_id"]
    result = store.save_video_progress(
        identity, token, Int64(2**32), 0, 200, final=True
    )
    assert result["position_seconds"] == 0
    validate_video_state(store.db.nodes.find_one({"_id": identity}))


def test_video_summaries_use_one_snapshot_batch_and_follow_visibility(
    video, monkeypatch
):
    store, identity, _clock = video
    writer = store.begin_video_progress(identity)["session_id"]
    saved = store.save_video_progress(identity, writer, 1, 20, 200)
    text = store.add_file("/", "source.txt", 8, 3)["id"]
    text_writer = store.begin_video_progress(text)["session_id"]
    store.save_video_progress(text, text_writer, 1, 20, 200)
    folder = store.create_folder("/", "Moved")
    store.move("/" + identity, "/" + folder["id"])
    calls = []
    find, aggregate = store._find, store.db.nodes.aggregate

    def observed_find(collection, query, projection=None):
        assert store._session is not None
        calls.append((collection, query, projection))
        return find(collection, query, projection)

    def observed_ancestors(*args, **kwargs):
        assert kwargs["session"] is store._session is not None
        calls.append("ancestors")
        return aggregate(*args, **kwargs)

    monkeypatch.setattr(store, "_find", observed_find)
    monkeypatch.setattr(store.db.nodes, "aggregate", observed_ancestors)
    assert store.video_progress_summaries([]) == {} and calls == []
    assert store.video_progress_summaries(
        [identity, identity, text, folder["id"], "missing"]
    ) == {identity: saved}
    assert len(calls) == 2 and calls[-1] == "ancestors"
    assert calls[0][0] == "nodes"
    assert set(calls[0][2]) == {"_id", "name", "kind", "video_progress"}
    monkeypatch.setattr(store, "_find", find)
    monkeypatch.setattr(store.db.nodes, "aggregate", aggregate)
    store.set_trashed("/" + folder["id"], True)
    assert store.video_progress_summaries([identity]) == {}
    with pytest.raises(ValidationError):
        store.video_progress_summaries([identity] * 129)
