"""Owner-only transfer destinations resolved from stable catalog identities."""

from tgdrive.core.errors import DriveError, TrashedItemError


def location_record(node, path, chain, *, stored):
    is_item = stored and node["_id"] != "root"
    folders = chain[:-1] if is_item else chain
    folder_path = (path.rsplit("/", 1)[0] or "/") if is_item else path
    folder_label = " / ".join(["My Files", *(row["name"] for row in folders[1:])])
    return {
        "available": True,
        "folder_path": folder_path,
        "folder_label": folder_label,
        "item_id": node["_id"] if is_item else None,
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
    except DriveError as error:
        return unavailable_location(error)


def unavailable_location(error):
    return {
        "available": False,
        "reason": "Item is in Trash"
        if isinstance(error, TrashedItemError)
        else "Location is no longer available",
    }


def enrich_locations(store, jobs):
    if not jobs:
        return jobs
    try:
        locations = store.transfer_locations(
            [
                (
                    job.get("id"),
                    job.get("destination", "/"),
                    job.get("status") == "completed"
                    and job.get("kind") not in {"torrent", "extract"},
                )
                for job in jobs
            ]
        )
    except DriveError:
        # A corrupt chain or invalid legacy ID must not hide healthy locations.
        locations = [locate_transfer(store, job) for job in jobs]
    for job, location in zip(jobs, locations, strict=True):
        job["location"] = (
            unavailable_location(location)
            if isinstance(location, DriveError)
            else location
        )
    return jobs
