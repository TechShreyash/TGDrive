"""Name/path rules, search keys and portable snapshot tree validation."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tgdrive.core.errors import (
    DuplicateNameError,
    InvalidNameError,
    InvalidPathError,
    ValidationError,
)
from tgdrive.storage.file_parts import validate_parts, validate_poster, validate_sha256

SCHEMA_VERSION = 5
ROOT_ID = "root"
# Operating systems reject longer names when a file is downloaded.
MAX_NAME_LENGTH = 255
# Results per search request; paging continues without a total limit.
MAX_SEARCH_RESULTS = 500
_SHARE_UNSET = object()

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("upload_date must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("upload_date must be valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("upload_date must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _share_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    ):
        raise ValidationError(
            "Link label must be at most 80 characters without controls"
        )
    return unicodedata.normalize("NFC", value).strip()


def _validate_id(value: object, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError("item id must contain only URL-safe opaque characters")
    if value == ROOT_ID and not allow_root:
        raise ValidationError("the reserved root id cannot be used by a child")
    return value


def _validate_channel_id(value: object) -> int:
    if type(value) is not int or value == 0 or not -(2**63) <= value < 2**63:
        raise ValidationError("storage_channel_id must be a non-zero 64-bit integer")
    return value


def _normalize_name(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidNameError("name must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise InvalidNameError("name cannot be empty, '.' or '..'")
    if normalized != normalized.strip():
        raise InvalidNameError("name cannot start or end with whitespace")
    if len(normalized) > MAX_NAME_LENGTH:
        raise InvalidNameError(f"name cannot exceed {MAX_NAME_LENGTH} characters")
    if "/" in normalized or "\\" in normalized or "\x00" in normalized:
        raise InvalidNameError("name cannot contain path separators or NUL")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise InvalidNameError("name cannot contain control characters or surrogates")
    return normalized


def _name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _fold(value: str) -> str:
    """Search form: case- and accent-insensitive, so ``cafe`` finds ``Café``."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", stripped).casefold()


_CHUNK = re.compile(r"[^\W_]+")
_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


def _tokens(name: str) -> list[str]:
    """Word starts for prefix search: separators, camelCase and letter/digit."""
    words = set()
    for chunk in _CHUNK.findall(name):
        words.add(_fold(chunk))
        words.update(_fold(part) for part in _SPLIT.split(chunk) if part)
    return sorted(words)


def _grams(value: str) -> list[str]:
    """Three-character pieces of a folded name for indexed mid-word search."""
    return sorted({value[i : i + 3] for i in range(len(value) - 2)})


def _validate_file_rename(current_name: str, new_name: str) -> None:
    """Keep an existing file suffix when changing its display name."""
    separator = current_name.rfind(".")
    if 0 < separator < len(current_name) - 1:
        extension = current_name[separator:]
        new_separator = new_name.rfind(".")
        new_extension = (
            new_name[new_separator:] if 0 < new_separator < len(new_name) - 1 else ""
        )
        if extension.lower() != new_extension.lower():
            raise InvalidNameError(
                f"The file extension cannot be changed. Keep the {extension} extension."
            )


def _validate_share_hashes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("share_hashes must be a list")
    hashes: list[str] = []
    for token_hash in value:
        if not isinstance(token_hash, str) or not _HASH_RE.fullmatch(token_hash):
            raise ValidationError("share token hashes must be lowercase SHA-256 hex")
        if token_hash in hashes:
            raise ValidationError("duplicate share token hash on item")
        hashes.append(token_hash)
    return hashes


def _strict_int(value: object, field_name: str, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= (1 << 63) - 1
    ):
        raise ValidationError(
            f"{field_name} must be a signed 64-bit integer >= {minimum}"
        )
    return value


@dataclass(slots=True)
class _FileNode:
    id: str
    name: str
    message_id: int
    size: int
    upload_date: str
    trashed: bool = False
    starred: bool = False
    share_hashes: list[str] = field(default_factory=list)
    parts: list[dict[str, Any]] | None = None
    sha256: str | None = None
    poster: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.id = _validate_id(self.id)
        self.name = _normalize_name(self.name)
        self.message_id = _strict_int(self.message_id, "message_id", 1)
        self.size = _strict_int(self.size, "size", 0)
        self.upload_date = _validate_timestamp(self.upload_date)
        if not isinstance(self.trashed, bool):
            raise ValidationError("trashed must be a boolean")
        if type(self.starred) is not bool:
            raise ValidationError("starred must be a boolean")
        self.share_hashes = _validate_share_hashes(self.share_hashes)
        self.sha256 = validate_sha256(self.sha256)
        if self.parts is not None:
            self.parts = validate_parts(
                self.parts, size=self.size, message_id=self.message_id
            )
        self.poster = validate_poster(
            self.poster, message_id=self.message_id, parts=self.parts
        )


@dataclass(slots=True)
class _FolderNode:
    id: str
    name: str
    upload_date: str
    trashed: bool = False
    starred: bool = False
    share_hashes: list[str] = field(default_factory=list)
    children: dict[str, _Node] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = _validate_id(self.id, allow_root=self.id == ROOT_ID)
        if self.id == ROOT_ID:
            if self.name != "/":
                raise ValidationError("root folder name must be '/'")
        else:
            self.name = _normalize_name(self.name)
        self.upload_date = _validate_timestamp(self.upload_date)
        if not isinstance(self.trashed, bool):
            raise ValidationError("trashed must be a boolean")
        if type(self.starred) is not bool:
            raise ValidationError("starred must be a boolean")
        self.share_hashes = _validate_share_hashes(self.share_hashes)
        if not isinstance(self.children, dict):
            raise ValidationError("folder children must be an object")


_Node = _FolderNode | _FileNode


def _parse_one(data: object) -> tuple[_Node, dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValidationError("each tree node must be an object")
    item_type = data.get("type")
    common_keys = {
        "id",
        "name",
        "share_hashes",
        "trashed",
        "type",
        "upload_date",
    }
    if item_type == "file":
        expected = common_keys | {"message_id", "size"}
        optional = {"parts", "sha256", "starred", "poster"}
        if not expected <= set(data) or set(data) - expected - optional:
            raise ValidationError("file node has missing or unknown fields")
        if "parts" in data and not isinstance(data["parts"], list):
            raise ValidationError("file parts must be an array")
        if "sha256" in data:
            validate_sha256(data["sha256"], optional=False)
        node = _FileNode(
            id=data["id"],
            name=data["name"],
            message_id=data["message_id"],
            size=data["size"],
            upload_date=data["upload_date"],
            trashed=data["trashed"],
            starred=data.get("starred", False),
            share_hashes=data["share_hashes"],
            parts=data.get("parts"),
            sha256=data.get("sha256"),
            poster=data.get("poster"),
        )
        return node, {}
    if item_type == "folder":
        expected = common_keys | {"children"}
        optional = {"starred"}
        if not expected <= set(data) or set(data) - expected - optional:
            raise ValidationError("folder node has missing or unknown fields")
        raw_children = data["children"]
        if not isinstance(raw_children, dict):
            raise ValidationError("folder children must be an object")
        node = _FolderNode(
            id=data["id"],
            name=data["name"],
            upload_date=data["upload_date"],
            trashed=data["trashed"],
            starred=data.get("starred", False),
            share_hashes=data["share_hashes"],
        )
        return node, raw_children
    raise ValidationError("node type must be 'file' or 'folder'")


def _parse_node(data: object) -> _Node:
    """Parse a portable tree with an explicit stack: depth has no limit."""
    root, children = _parse_one(data)
    pending = [(root, children)]
    while pending:
        folder, raw_children = pending.pop()
        for child_id, raw_child in raw_children.items():
            if not isinstance(child_id, str):
                raise ValidationError("child map keys must be strings")
            child, grandchildren = _parse_one(raw_child)
            if child.id != child_id:
                raise ValidationError("child map key does not match child id")
            folder.children[child_id] = child
            if isinstance(child, _FolderNode):
                pending.append((child, grandchildren))
    return root


def _validate_tree(root: _FolderNode) -> None:
    if root.id != ROOT_ID or root.name != "/" or root.trashed:
        raise ValidationError(
            "root must be an active folder with id 'root' and name '/'"
        )

    seen_ids: set[str] = {ROOT_ID}
    seen_hashes: set[str] = set(root.share_hashes)
    if len(seen_hashes) != len(root.share_hashes):
        raise ValidationError("duplicate root share token hash")
    pending = [root]
    while pending:
        folder = pending.pop()
        sibling_names: set[str] = set()
        for child_id, child in folder.children.items():
            if child_id != child.id:
                raise ValidationError("child map key does not match child id")
            if child.id in seen_ids:
                raise ValidationError(f"duplicate item id: {child.id}")
            seen_ids.add(child.id)
            key = _name_key(child.name)
            if not child.trashed and key in sibling_names:
                raise DuplicateNameError(
                    f"duplicate case-insensitive sibling name: {child.name}"
                )
            if not child.trashed:
                sibling_names.add(key)
            for token_hash in child.share_hashes:
                if token_hash in seen_hashes:
                    raise ValidationError(
                        "share token hash is assigned to multiple items"
                    )
                seen_hashes.add(token_hash)
            if isinstance(child, _FolderNode):
                pending.append(child)


def _normalize_path(path: object) -> tuple[tuple[str, ...], str]:
    if not isinstance(path, str) or not path:
        raise InvalidPathError("path must be a non-empty string")
    if not path.startswith("/"):
        raise InvalidPathError("path must be absolute")
    if "\\" in path or "\x00" in path or "//" in path:
        raise InvalidPathError("path contains a forbidden separator or NUL")
    if path == "/":
        return (), "/"
    normalized = path[:-1] if path.endswith("/") else path
    parts = tuple(normalized[1:].split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InvalidPathError("path contains an empty or traversal component")
    for part in parts:
        if not _ID_RE.fullmatch(part):
            raise InvalidPathError("path components must be item ids")
        if part == ROOT_ID:
            raise InvalidPathError("root is represented by '/', not '/root'")
    return parts, "/" + "/".join(parts)


def _join_path(parent_path: str, child_id: str) -> str:
    return f"/{child_id}" if parent_path == "/" else f"{parent_path}/{child_id}"


def _parent_path(full_path: str) -> str:
    if full_path == "/":
        return "/"
    parent = full_path.rsplit("/", 1)[0]
    return parent or "/"


class FolderTreeConflictError(ValidationError):
    """An idempotent folder preparation cannot safely be reused."""


def _folder_manifest(
    destination: object, root_name: object, folders: object
) -> dict[str, Any]:
    _, destination = _normalize_path(destination)
    root_name = _normalize_name(root_name)
    if not isinstance(folders, list):
        raise ValidationError("folders must be a list")
    paths: set[tuple[str, ...]] = set()
    siblings: set[tuple[tuple[str, ...], str]] = set()
    for segments in folders:
        if not isinstance(segments, list) or not segments:
            raise ValidationError(
                "relative folder paths must be non-empty segment arrays"
            )
        path = tuple(_normalize_name(segment) for segment in segments)
        key = (path[:-1], _name_key(path[-1]))
        if path in paths or key in siblings:
            raise DuplicateNameError(
                "folder tree has duplicate normalized sibling names"
            )
        paths.add(path)
        siblings.add(key)
    if any(path[:-1] and path[:-1] not in paths for path in paths):
        raise ValidationError("folder tree must include every ancestor folder")
    return {
        "destination": destination,
        "root_name": root_name,
        "folders": [
            list(path) for path in sorted(paths, key=lambda path: (len(path), path))
        ],
    }


def _manifest_json(manifest: dict[str, Any]) -> str:
    return json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
