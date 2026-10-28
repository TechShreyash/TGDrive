"""Owner-only transfer destinations resolved from stable catalog identities."""

from tgdrive.core.errors import DriveError, TrashedItemError


def location_record(node, path, chain, *, stored):
    is_item = stored and node["id"] != "root"
    folders = chain[:-1] if is_item else chain
    folder_path = (path.rsplit("/", 1)[0] or "/") if is_item else path
    folder_label = " / ".join(["My Files", *(row["name"] for row in folders[1:])])
    return {
        "available": True,
        "folder_path": folder_path,
        "folder_label": folder_label,
        "item_id": node["id"] if is_item else None,
        "item_name": node["name"] if is_item else None,
        "display_path": folder_label + (" / " + node["name"] if is_item else ""),
    }


def locate_transfer(store, job):
    try:
        return store.transfer_location(
            job.get("id"),
            job.get("destination", "/"),
            completed=job.get("status") == "completed"
            and job.get("kind") not in {"torrent", "extract"},
        )
    except TrashedItemError:
        return {"available": False, "reason": "Item is in Trash"}
    except DriveError:
        return {"available": False, "reason": "Location is no longer available"}


def enrich_locations(store, jobs):
    for job in jobs:
        job["location"] = locate_transfer(store, job)
    return jobs
