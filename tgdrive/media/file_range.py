"""Read an authorized range from a Telegram-backed file."""

from fastapi import HTTPException

from tgdrive.media.logical_media import PartLease, part_ranges
from tgdrive.media.media_reader import media_cache_for, stream_pooled_media
from tgdrive.media.previews import lease_media
from tgdrive.media.ranges import ByteRange, plan_telegram_chunks
from tgdrive.storage.file_parts import validate_parts


async def read_range(pool, channel, data, start, count, authorize, *, background=False):
    await authorize()

    def register(parts=None):
        if not callable(getattr(pool, "download_capacity", None)):
            return
        media_cache_for(pool)
        pool._media_read_cache.register_file(
            channel_id=channel,
            message_id=data["message_id"],
            size=data["size"],
            name=data.get("name", ""),
            parts=parts,
            purpose=data.get("purpose", "original" if data.get("name") else "unknown"),
        )

    selection = ByteRange(start, start + count - 1, data["size"], is_partial=True)
    result = bytearray()
    if data.get("parts") is not None:
        parts = validate_parts(
            data["parts"], size=data["size"], message_id=data["message_id"]
        )
        register(parts)
        for part, interval in part_ranges(parts, selection):
            lease = await PartLease(
                pool, channel, part, authorize=authorize, background=background
            ).open()
            stream = lease.read(interval)
            try:
                async for block in stream:
                    result.extend(block)
            finally:
                await stream.aclose()
                await lease.close()
    else:
        async with lease_media(
            pool, channel, data["message_id"], background=background
        ) as (
            client,
            message,
            media,
        ):
            if media.file_size != data["size"]:
                raise HTTPException(409, "The stored file changed. Reopen it.")
            register()
            plan = plan_telegram_chunks(selection)
            stream = stream_pooled_media(
                pool,
                client,
                message,
                channel=channel,
                offset=plan.offset // plan.chunk_size,
                limit=plan.part_count,
                authorize=authorize,
                background=background,
            )
            try:
                index = 0
                async for block in stream:
                    first = plan.first_part_cut if index == 0 else 0
                    last = (
                        plan.last_part_cut
                        if index == plan.part_count - 1
                        else len(block)
                    )
                    result.extend(block[first:last])
                    index += 1
                    if index >= plan.part_count:
                        break
            finally:
                await stream.aclose()
    if len(result) != count:
        raise HTTPException(
            409, "The file ended early. Reopen it or download the original."
        )
    await authorize()
    return bytes(result)
