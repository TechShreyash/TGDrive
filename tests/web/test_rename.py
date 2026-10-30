"""File renames preserve extensions in both catalog implementations."""

import pytest

from tests.support.mongo_fixtures import new_store
from tgdrive.core.errors import InvalidNameError


@pytest.fixture
def store():
    instance = new_store()
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize(
    ("original", "renamed"),
    [
        ("earth.mp4", "earth"),
        ("earth.mp4", "earth.pdf"),
        ("earth.mp4", "earth.mp4.exe"),
        ("earth.mp4", "earth."),
        ("earth.mp4", ".mp4"),
        ("archive.tar.gz", "archive.tar"),
        (".config.json", ".config"),
        ("image.svg", "image.\u017fvg"),
        ("file.ss", "file.\u00df"),
    ],
)
def test_file_extension_change_is_rejected_without_mutation(store, original, renamed):
    item = store.add_file("/", original, 123, 456)
    path = "/" + item["id"]
    before = store.export()
    revision = store.revision

    with pytest.raises(InvalidNameError, match="extension cannot be changed"):
        store.rename(path, renamed)

    assert store.export() == before
    assert store.revision == revision
    assert store.get_file(path)["name"] == original


@pytest.mark.parametrize(
    ("original", "renamed"),
    [
        ("earth.mp4", "planet.mp4"),
        ("earth.mp4", "EARTH.MP4"),
        ("earth.MP4", "planet.mp4"),
        ("earth.mp4", "earth.final.mp4"),
        ("archive.tar.gz", "backup.gz"),
        (".config.json", ".settings.json"),
        ("README", "NOTES"),
        ("earth", "earth.mp4"),
        (".env", ".env.local"),
        (".env", "settings"),
        ("trailing.", "repaired.txt"),
    ],
)
def test_file_rename_preserves_metadata_and_accepts_supported_names(
    store, original, renamed
):
    item = store.add_file("/", original, 123, 456)
    path = "/" + item["id"]
    before = store.get_file(path)
    revision = store.revision

    assert store.rename(path, renamed)["name"] == renamed
    assert store.get_file(path) == before | {"name": renamed}
    assert store.revision == revision + 1


def test_unchanged_file_name_does_not_bump_revision(store):
    item = store.add_file("/", "earth.mp4", 123, 456)
    before = store.export()

    assert store.rename("/" + item["id"], "earth.mp4")["name"] == "earth.mp4"
    assert store.export() == before


@pytest.mark.parametrize("renamed", ["archive", "archive.zip"])
def test_folder_rename_can_change_or_remove_suffix(store, renamed):
    item = store.create_folder("/", "photos.mp4")
    path = "/" + item["id"]
    child = store.add_file(path, "earth.mp4", 123, 456)

    assert store.rename(path, renamed)["name"] == renamed
    assert store.get_directory(path)["name"] == renamed
    assert store.get_file(path + "/" + child["id"])["name"] == "earth.mp4"
