"""Restricted migration of TGDrive's legacy ``drive.data`` pickle format.

Only the three historical metadata classes are admitted.  No function, builtin,
third-party dill helper, or arbitrary application class can be resolved while
unpickling. The inert objects are then copied into the current SQLite store and
validated by :class:`tgdrive.store.DriveStore`.
"""

from __future__ import annotations

import io
import os
import pickle  # nosec B403
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from tgdrive.errors import CorruptStoreError, LegacyImportError, ValidationError
from tgdrive.store import (
    MAX_STORE_BYTES,
    MAX_TREE_DEPTH,
    MAX_TREE_ITEMS,
    ROOT_ID,
    SCHEMA_VERSION,
    DriveStore,
)


class _LegacyNewDriveData:
    """Inert state container used instead of importing the historical class."""


class _LegacyFolder:
    """Inert state container used instead of importing the historical class."""


class _LegacyFile:
    """Inert state container used instead of importing the historical class."""


_ALLOWED_GLOBALS = {
    ("utils.directoryHandler", "NewDriveData"): _LegacyNewDriveData,
    ("utils.directoryHandler", "Folder"): _LegacyFolder,
    ("utils.directoryHandler", "File"): _LegacyFile,
}


class RestrictedLegacyUnpickler(pickle.Unpickler):
    """Unpickler that resolves only TGDrive's three old data containers."""

    def find_class(self, module: str, name: str) -> type:
        allowed = _ALLOWED_GLOBALS.get((module, name))
        if allowed is None:
            raise pickle.UnpicklingError(f"forbidden pickle global: {module}.{name}")
        return allowed

    def persistent_load(self, pid: object) -> object:
        raise pickle.UnpicklingError("persistent pickle references are forbidden")


def _read_source(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO,
) -> bytes:
    try:
        if isinstance(source, (bytes, bytearray, memoryview)):
            raw = bytes(source)
        elif isinstance(source, (str, os.PathLike)):
            with Path(source).open("rb") as handle:
                raw = handle.read(MAX_STORE_BYTES + 1)
        elif hasattr(source, "read"):
            raw = source.read(MAX_STORE_BYTES + 1)
            if not isinstance(raw, bytes):
                raise LegacyImportError("legacy source must be opened in binary mode")
        else:
            raise LegacyImportError("unsupported legacy source type")
    except OSError as exc:
        raise LegacyImportError(f"could not read legacy metadata: {exc}") from exc
    if len(raw) > MAX_STORE_BYTES:
        raise LegacyImportError(
            f"legacy metadata exceeds the {MAX_STORE_BYTES}-byte safety limit"
        )
    if not raw:
        raise LegacyImportError("legacy metadata is empty")
    return raw


def restricted_load(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO,
) -> object:
    """Load one restricted legacy pickle object and reject trailing payloads."""

    stream = io.BytesIO(_read_source(source))
    try:
        value = RestrictedLegacyUnpickler(stream).load()
    except (pickle.PickleError, EOFError, AttributeError, TypeError, ValueError) as exc:
        raise LegacyImportError(f"legacy pickle rejected: {exc}") from exc
    if stream.read(1):
        raise LegacyImportError("trailing data after the legacy pickle is forbidden")
    return value


def _required_attr(value: object, name: str) -> Any:
    state = getattr(value, "__dict__", None)
    if not isinstance(state, dict) or name not in state:
        raise LegacyImportError(f"legacy object is missing required field '{name}'")
    return state[name]


def _legacy_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise LegacyImportError("legacy upload_date must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyImportError(f"invalid legacy upload_date: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _convert_tree(root: object) -> dict[str, Any]:
    seen_objects: set[int] = set()
    item_count = 0

    def convert(value: object, *, is_root: bool, depth: int) -> dict[str, Any]:
        nonlocal item_count
        if depth > MAX_TREE_DEPTH:
            raise LegacyImportError(
                f"legacy tree exceeds maximum depth of {MAX_TREE_DEPTH}"
            )
        identity = id(value)
        if identity in seen_objects:
            raise LegacyImportError("legacy tree contains a cycle or reused node")
        seen_objects.add(identity)
        item_count += 1
        if item_count > MAX_TREE_ITEMS:
            raise LegacyImportError(
                f"legacy tree exceeds maximum item count of {MAX_TREE_ITEMS}"
            )

        if type(value) is _LegacyFile:
            if is_root:
                raise LegacyImportError("legacy root must be a folder")
            declared_type = _required_attr(value, "type")
            if declared_type != "file":
                raise LegacyImportError("legacy File has an invalid type field")
            return {
                "id": _required_attr(value, "id"),
                "message_id": _required_attr(value, "file_id"),
                "name": _required_attr(value, "name"),
                "share_hashes": [],
                "size": _required_attr(value, "size"),
                "trashed": _required_attr(value, "trash"),
                "type": "file",
                "upload_date": _legacy_timestamp(_required_attr(value, "upload_date")),
            }

        if type(value) is not _LegacyFolder:
            raise LegacyImportError(
                f"unexpected legacy tree node: {type(value).__name__}"
            )
        declared_type = _required_attr(value, "type")
        if declared_type != "folder":
            raise LegacyImportError("legacy Folder has an invalid type field")
        raw_children = _required_attr(value, "contents")
        if not isinstance(raw_children, dict):
            raise LegacyImportError("legacy folder contents must be a dictionary")
        folder_id = _required_attr(value, "id")
        folder_name = _required_attr(value, "name")
        if is_root and (folder_id != ROOT_ID or folder_name != "/"):
            raise LegacyImportError("legacy root identity is invalid")
        children: dict[str, dict[str, Any]] = {}
        for child_key, child in raw_children.items():
            if not isinstance(child_key, str):
                raise LegacyImportError("legacy child keys must be strings")
            converted = convert(child, is_root=False, depth=depth + 1)
            if converted["id"] != child_key:
                raise LegacyImportError("legacy child key does not match its item id")
            children[child_key] = converted
        return {
            "children": children,
            "id": folder_id,
            "name": folder_name,
            # Old six-character plaintext share credentials are intentionally not
            # migrated.  New shares use one-time 256-bit tokens stored as hashes.
            "share_hashes": [],
            "trashed": False if is_root else _required_attr(value, "trash"),
            "type": "folder",
            "upload_date": _legacy_timestamp(_required_attr(value, "upload_date")),
        }

    return convert(root, is_root=True, depth=0)


def convert_legacy(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO,
    destination: str | os.PathLike[str] | None = None,
    *,
    overwrite: bool = False,
) -> DriveStore:
    """Convert a restricted legacy tree to a dirty current-schema :class:`DriveStore`.

    This offline compatibility helper is never invoked during startup. If a
    destination is supplied it must not exist; all rows are imported together in
    one SQLite transaction. Existing databases are never overwritten.
    """

    legacy = restricted_load(source)
    if type(legacy) is not _LegacyNewDriveData:
        raise LegacyImportError("legacy top-level object must be NewDriveData")
    raw_contents = _required_attr(legacy, "contents")
    if not isinstance(raw_contents, dict) or set(raw_contents) != {"/"}:
        raise LegacyImportError("legacy drive must contain exactly one '/' root")
    try:
        payload = {
            "backed_up_revision": 0,
            "revision": 1,
            "root": _convert_tree(raw_contents["/"]),
            "schema_version": SCHEMA_VERSION,
        }
        return DriveStore._from_payload(
            payload,
            storage_path=destination,
            persist=destination is not None,
            overwrite=overwrite,
        )
    except LegacyImportError:
        raise
    except (CorruptStoreError, ValidationError, TypeError, RecursionError) as exc:
        raise LegacyImportError(f"legacy metadata validation failed: {exc}") from exc
