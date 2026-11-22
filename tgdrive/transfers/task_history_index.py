"""Rebuildable MongoDB read model for numbered transfer history."""

from __future__ import annotations

import re
import secrets
import unicodedata
from typing import Any

from pymongo import UpdateOne

HISTORY_INDEX = "transfer_history_index"
_BATCH = 256
_ORDER = [("created_at", -1), ("task_id", -1)]
_STATUS = ("completed", "cancelled", "error", "queued", "cancelling", "running")
_RANK = {status: rank for rank, status in enumerate(_STATUS)}


def task_identity(row):
    folder = row.get("folder_upload")
    if (
        row.get("kind") in {"upload", "receiving"}
        and isinstance(folder, dict)
        and isinstance(folder.get("id"), str)
        and folder["id"]
    ):
        return "folder:" + folder["id"]
    return "job:" + row["id"]


def _fold(name):
    return unicodedata.normalize("NFC", str(name)).casefold()


def _task(row):
    identity = task_identity(row)
    folder = row.get("folder_upload") or {}
    in_folder = identity.startswith("folder:")
    return {
        "task_id": identity,
        "created_at": (
            folder.get("created_at", row["created_at"])
            if in_folder
            else row["created_at"]
        ),
        "folder_id": folder.get("id"),
        "status_rank": _RANK[row["status"]],
        "name": _fold(row.get("name", "")),
        "folder_name": _fold(folder.get("name", "")) if in_folder else "",
    }


def _rank_filter(status):
    if status == "finished":
        return {"$lte": _RANK["error"]}
    if status == "running":
        return {"$in": [_RANK["cancelling"], _RANK["running"]]}
    return _RANK[status]


def _matches_rank(rank, status):
    if status == "all":
        return True
    if status == "finished":
        return rank <= _RANK["error"]
    if status == "running":
        return rank >= _RANK["cancelling"]
    return rank == _RANK[status]


class TaskHistoryIndex:
    def __init__(self, backend, jobs):
        self.backend = backend
        self.jobs = jobs
        self.state = backend.db.runtime
        self.tasks = backend.db.transfer_tasks

    def ensure_indexes(self):
        self.tasks.create_index([("generation", 1), *_ORDER])
        self.tasks.create_index([("generation", 1), ("status_rank", 1), *_ORDER])
        self.jobs.create_index(
            [("archive_task", 1)], partialFilterExpression={"archived": True}
        )

    def _header(self, session):
        return self.state.find_one({"_id": HISTORY_INDEX}, session=session)

    def _index_entry(self, archive, generation):
        item = _task(archive["summary"])
        identity = item["task_id"]
        updates = {
            "archive_task": identity,
            "search_name": item["name"],
            "archive_folder_name": item["folder_name"],
        }
        task_update = UpdateOne(
            {"_id": f"{generation}:{identity}"},
            {
                "$setOnInsert": {
                    "generation": generation,
                    "task_id": identity,
                    "folder_id": item["folder_id"],
                },
                "$min": {"created_at": item["created_at"]},
                "$max": {"status_rank": item["status_rank"]},
            },
            upsert=True,
        )
        return updates, task_update

    def rebuild_step(self):
        """Advance one bounded, fenced batch. Replaying each update is safe."""

        def step(session):
            self.backend.fence(session)
            header = self._header(session)
            if header is None:
                header = {
                    "_id": HISTORY_INDEX,
                    "generation": secrets.token_hex(12),
                    "after": None,
                    "ready": False,
                    "total_tasks": 0,
                }
                self.state.insert_one(header, session=session)
            generation = header["generation"]
            if "total_tasks" not in header:
                # A generation built by an earlier version already has rows.
                count = self.tasks.count_documents(
                    {"generation": generation}, session=session
                )
                self.state.update_one(
                    {"_id": HISTORY_INDEX},
                    {"$set": {"total_tasks": count}},
                    session=session,
                )
            if header["ready"]:
                return True
            query: dict[str, Any] = {"archived": True}
            if header["after"] is not None:
                query["_id"] = {"$gt": header["after"]}
            with (
                self.jobs.find(
                    query,
                    {
                        "summary.id": 1,
                        "summary.kind": 1,
                        "summary.name": 1,
                        "summary.status": 1,
                        "summary.created_at": 1,
                        "summary.folder_upload": 1,
                    },
                    session=session,
                )
                .sort("_id", 1)
                .limit(_BATCH) as cursor
            ):
                rows = list(cursor)
            members, tasks = [], []
            for archive in rows:
                if archive["summary"].get("kind") == "delete":
                    continue
                updates, task_update = self._index_entry(archive, generation)
                members.append(UpdateOne({"_id": archive["_id"]}, {"$set": updates}))
                tasks.append(task_update)
            added = 0
            if tasks:
                self.jobs.bulk_write(members, ordered=False, session=session)
                added = self.tasks.bulk_write(
                    tasks, ordered=False, session=session
                ).upserted_count
            # An archive inserted behind the cursor is indexed by archive().
            done = len(rows) < _BATCH
            self.state.update_one(
                {"_id": HISTORY_INDEX},
                {
                    "$set": {
                        "after": rows[-1]["_id"] if rows else header["after"],
                        "ready": done,
                    },
                    "$inc": {"total_tasks": added},
                },
                session=session,
            )
            return done

        return self.backend.transaction(step)

    def clean_step(self):
        """Reclaim a bounded batch from generations invalidated by restore."""

        def step(session):
            self.backend.fence(session)
            header = self._header(session)
            if header is None or not header["ready"]:
                return True
            with self.tasks.find(
                {"generation": {"$ne": header["generation"]}},
                {"_id": 1},
                session=session,
            ).limit(_BATCH) as cursor:
                identities = [row["_id"] for row in cursor]
            if identities:
                self.tasks.delete_many({"_id": {"$in": identities}}, session=session)
            return len(identities) < _BATCH

        return self.backend.transaction(step)

    def _adjust_total(self, session, header, change):
        if change and "total_tasks" in header:
            self.state.update_one(
                {"_id": HISTORY_INDEX},
                {"$inc": {"total_tasks": change}},
                session=session,
            )

    def archive(self, archive):
        def commit(session):
            self.backend.fence(session)
            header = self._header(session)
            document = archive
            if header is not None:
                updates, task_update = self._index_entry(archive, header["generation"])
                document = archive | updates
                added = self.tasks.bulk_write([task_update], session=session)
                self._adjust_total(session, header, added.upserted_count)
            self.jobs.replace_one({"_id": archive["_id"]}, document, session=session)

        self.backend.transaction(commit)

    def save_new_live(self, document):
        """Reusing a caller-supplied ID can retire an already archived member."""

        def commit(session):
            self.backend.fence(session)
            query = {"_id": document["_id"]}
            previous = self.jobs.find_one(query, {"archive_task": 1}, session=session)
            self.jobs.replace_one(query, document, upsert=True, session=session)
            head = (
                self._header(session)
                if previous and previous.get("archive_task")
                else None
            )
            if head is None:
                return
            identity = previous["archive_task"]
            generation = head["generation"]
            removed = self.tasks.delete_one(
                {"_id": f"{generation}:{identity}"}, session=session
            ).deleted_count
            added = 0
            with self.jobs.find(
                {"archived": True, "archive_task": identity},
                {"summary": 1},
                session=session,
            ) as members:
                updates = []
                for member in members:
                    _, task_update = self._index_entry(member, generation)
                    updates.append(task_update)
                    if len(updates) == _BATCH:
                        added += self.tasks.bulk_write(
                            updates, ordered=False, session=session
                        ).upserted_count
                        updates.clear()
                if updates:
                    added += self.tasks.bulk_write(
                        updates, ordered=False, session=session
                    ).upserted_count
            self._adjust_total(session, head, added - removed)

        self.backend.transaction(commit)

    def clear(self, query, keep_folders):
        def commit(session):
            self.backend.fence(session)
            header = self._header(session)
            if header is not None:
                removed = self.tasks.delete_many(
                    {
                        "generation": header["generation"],
                        "folder_id": {"$nin": sorted(keep_folders)},
                    },
                    session=session,
                ).deleted_count
                self._adjust_total(session, header, -removed)
            return self.jobs.delete_many(query, session=session).deleted_count

        return self.backend.transaction(commit)

    @staticmethod
    def _search(term):
        literal = {"$regex": re.escape(term)}
        return {"$or": [{"search_name": literal}, {"archive_folder_name": literal}]}

    def _search_tasks(self, filters, term, generation):
        return [
            {"$match": {"archived": True, **self._search(term)}},
            {"$group": {"_id": "$archive_task"}},
            {"$project": {"key": {"$concat": [generation, ":", "$_id"]}}},
            {
                "$lookup": {
                    "from": self.tasks.name,
                    "localField": "key",
                    "foreignField": "_id",
                    "as": "task",
                }
            },
            {"$unwind": "$task"},
            {"$replaceWith": "$task"},
            {"$match": filters},
        ]

    def numbered(self, live, *, status, page, limit, query, offset, exclude_folders):
        with self.backend.client.start_session(snapshot=True) as session:
            head = self._header(session)
            if head is None or not head["ready"] or "total_tasks" not in head:
                return None
            return self._numbered(
                session,
                head["generation"],
                live,
                status=status,
                page=page,
                limit=limit,
                query=query,
                offset=offset,
                excluded={"folder:" + identity for identity in exclude_folders},
                archived_total=head["total_tasks"],
            )

    def _numbered(
        self,
        session,
        generation,
        live,
        *,
        status,
        page,
        limit,
        query,
        offset,
        excluded,
        archived_total,
    ):
        overlays = {}
        for row in live:
            if row.get("kind") == "delete":
                continue
            item = _task(row)
            identity = item["task_id"]
            if identity in excluded:
                continue
            current = overlays.setdefault(
                identity,
                {
                    "task_id": identity,
                    "created_at": item["created_at"],
                    "status_rank": item["status_rank"],
                    "matches": False,
                },
            )
            current["created_at"] = min(current["created_at"], item["created_at"])
            current["status_rank"] = max(current["status_rank"], item["status_rank"])
            current["matches"] |= query in item["name"] or query in item["folder_name"]

        archived_overlaps = 0
        if overlays:
            with self.tasks.find(
                {"_id": {"$in": [f"{generation}:{identity}" for identity in overlays]}},
                session=session,
            ) as cursor:
                for archived in cursor:
                    archived_overlaps += 1
                    current = overlays[archived["task_id"]]
                    current["created_at"] = min(
                        current["created_at"], archived["created_at"]
                    )
                    current["status_rank"] = max(
                        current["status_rank"], archived["status_rank"]
                    )
            if query:
                with self.jobs.aggregate(
                    [
                        {
                            "$match": {
                                "archived": True,
                                "archive_task": {"$in": list(overlays)},
                                **self._search(query),
                            }
                        },
                        {"$group": {"_id": "$archive_task"}},
                    ],
                    session=session,
                ) as cursor:
                    for row in cursor:
                        overlays[row["_id"]]["matches"] = True

        extra = [
            row
            for row in overlays.values()
            if row["matches"] and _matches_rank(row["status_rank"], status)
        ]
        filters: dict[str, Any] = {"generation": generation}
        if excluded or overlays:
            filters["task_id"] = {"$nin": sorted(excluded | set(overlays))}
        if status != "all":
            filters["status_rank"] = _rank_filter(status)
        search = self._search_tasks(filters, query, generation) if query else None
        if search is not None:
            with self.jobs.aggregate(
                [*search, {"$count": "total"}], session=session
            ) as cursor:
                count = next(cursor, {}).get("total", 0)
        elif status == "all" and not excluded:
            count = archived_total - archived_overlaps
        else:
            count = self.tasks.count_documents(filters, session=session)
        total = count + len(extra)
        pages = max(1, (total + limit - 1) // limit)
        if offset is None:
            page = min(page, pages)
            offset = (page - 1) * limit
        selected = []
        if offset < total:
            selected = self._page(session, filters, search, extra, offset, limit)
        with self.jobs.find(
            {"archived": True, "archive_task": {"$in": selected}},
            {"summary": 1},
            session=session,
        ) as cursor:
            members = {row["summary"]["id"]: row["summary"] for row in cursor}
        order = {identity: index for index, identity in enumerate(selected)}
        for row in live:
            if task_identity(row) in order:
                members[row["id"]] = row
        return {
            "jobs": sorted(
                members.values(),
                key=lambda row: (
                    order[task_identity(row)],
                    row["created_at"],
                    row["id"],
                ),
            ),
            "page": page,
            "page_size": limit,
            "total_tasks": total,
            "total_pages": pages,
            "has_more": offset + limit < total,
            "offset": offset,
            "next_offset": min(total, offset + limit),
        }

    def _page(self, session, filters, search, extra, offset, limit):
        # At most len(extra) live tasks can fall before the page boundary.
        skip = max(0, offset - len(extra))
        size = limit + len(extra)
        if search is not None:
            stages = [*search, {"$sort": dict(_ORDER)}]
            if skip:
                stages.append({"$skip": skip})
            cursor = self.jobs.aggregate([*stages, {"$limit": size}], session=session)
        else:
            cursor = (
                self.tasks.find(filters, session=session)
                .sort(_ORDER)
                .skip(skip)
                .limit(size)
            )
        with cursor:
            rows = list(cursor)

        def key(row):
            return row["created_at"], row["task_id"]

        if skip and rows:
            # Live entries before this anchor have already passed the cut.
            anchor = key(rows[0])
            earlier = [row for row in extra if key(row) > anchor]
            skip += len(earlier)
            extra = [row for row in extra if key(row) <= anchor]
        start = offset - skip
        rows.extend(extra)
        rows.sort(key=key, reverse=True)
        return [row["task_id"] for row in rows[start : start + limit]]
