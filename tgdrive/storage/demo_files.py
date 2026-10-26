"""Stable demo content identity and request/job-scoped catalog write policy."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from tgdrive.core.errors import NotFoundError
from tgdrive.settings.managed_settings import SettingsProblem

DEMO_FILES_ID = "tgdrive-demo-files"
DEMO_FILES_NAME = "Demo Files"
restricted_writes: ContextVar[bool] = ContextVar(
    "demo_restricted_writes", default=False
)


class DemoFilesReadOnly(SettingsProblem):
    def __init__(self):
        super().__init__(
            "Demo Files is read-only. Use My Files outside this folder to upload "
            "or change files, or sign in with the full-access demo password.",
            code="DEMO_FILES_READ_ONLY",
            status=403,
        )


@contextmanager
def write_policy(restricted: bool):
    token = restricted_writes.set(restricted)
    try:
        yield
    finally:
        restricted_writes.reset(token)


def request_policy(request):
    from tgdrive.web.demo_access import is_demo_restricted

    restricted_writes.set(is_demo_restricted(request))


def check_ids(identities):
    """Called under the catalog transaction, including background file commits."""
    if restricted_writes.get() and DEMO_FILES_ID in identities:
        raise DemoFilesReadOnly()


def check_path(store, path, *, descendants=False, identity=None):
    """Preflight current identity before staging, network work, or job changes."""
    if not restricted_writes.get():
        return
    node_id = identity or (
        path.rstrip("/").rsplit("/", 1)[-1] if path != "/" else "root"
    )
    current = store.resolve_id(node_id)
    check_ids(current.strip("/").split("/"))
    if descendants:
        try:
            demo = store.resolve_id(DEMO_FILES_ID)
        except NotFoundError:
            return
        if current == "/" or demo.startswith(current.rstrip("/") + "/"):
            raise DemoFilesReadOnly()


def check_receipt(store, item):
    check_path(store, item["parent_id"], identity=item.get("parent_node_id"))


def check_transfer(store, record):
    context = record.context
    if context.get("kind") == "delete":
        for intent in store.pending_deletions():
            if intent["id"] == context.get("deletion_id"):
                check_path(
                    store, intent["path"], descendants=True, identity=intent["root_id"]
                )
                return
    if context.get("destination") or context.get("destination_id"):
        check_path(
            store,
            context.get("destination", "/"),
            identity=context.get("destination_id"),
        )


def annotate(record):
    """Annotate independent HTTP records; never add presentation fields to storage."""
    if isinstance(record, list):
        for item in record:
            annotate(item)
    elif isinstance(record, dict):
        identity = record.get("id")
        path = record.get("path", "")
        if identity == DEMO_FILES_ID:
            record["demo_files"] = True
        if restricted_writes.get() and (
            identity == DEMO_FILES_ID or DEMO_FILES_ID in path.strip("/").split("/")
        ):
            record["read_only"] = True
        for value in tuple(record.values()):
            if isinstance(value, (dict, list)):
                annotate(value)
    return record
