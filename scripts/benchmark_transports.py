"""Isolated Telegram validation; credentials are read only from a private JSON file.

Run from a private copy of this repository with its dependencies installed.
Required config keys: api_id, api_hash, bot_token, channel, channel_hash.
Optional manifest keys: size, sha256, parts (the existing TGDrive part records).
Only the benchmark's freshly uploaded message is deleted. Existing manifest
messages are read only. No TGDrive catalog, account settings or shares are edited.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import time
import zlib
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from urllib import parse, request

MIB = 1024**2


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def check_http(config, boundaries):
    """Check an existing share without logging its capability-bearing URL."""
    capability = config.get("http")
    if not capability:
        emit(
            "deployed_http_unavailable", reason="No existing share capability supplied"
        )
        return False
    origin = parse.urlsplit(capability["origin"])
    if origin.scheme not in {"http", "https"} or not origin.hostname:
        raise ValueError("An HTTP(S) share origin is required")
    size = config["manifest"]["size"]
    endpoint = (
        capability["origin"]
        + "/file?"
        + parse.urlencode({"path": capability["path"], "auth": capability["auth"]})
    )
    valid = True
    try:
        head = request.Request(  # noqa: S310 - scheme checked.
            endpoint, method="HEAD", headers={"Accept-Encoding": "gzip"}
        )
        with request.urlopen(head, timeout=45) as response:  # noqa: S310
            matched = (
                response.status == 200
                and response.headers.get("Content-Length") == str(size)
                and response.headers.get("Accept-Ranges") == "bytes"
            )
            emit(
                "deployed_head",
                status=response.status,
                content_length=response.headers.get("Content-Length"),
                content_type=response.headers.get("Content-Type"),
                accept_ranges=response.headers.get("Accept-Ranges"),
                content_encoding=response.headers.get("Content-Encoding"),
                transfer_encoding=response.headers.get("Transfer-Encoding"),
                valid=matched,
            )
            valid = valid and matched
    except Exception as error:
        emit(
            "deployed_head_failure",
            error_type=type(error).__name__,
            status=getattr(error, "code", None),
        )
        valid = False
    for boundary in boundaries:
        start, end = boundary["start"], boundary["end"]
        encoding = boundary.get("accept_encoding", "identity")
        began = time.perf_counter()
        probe = request.Request(  # noqa: S310 - scheme checked.
            endpoint,
            headers={
                "Range": f"bytes={start}-{end}",
                "Accept-Encoding": encoding,
            },
        )
        try:
            with request.urlopen(probe, timeout=90) as response:  # noqa: S310
                body = response.read(end - start + 2)
                html = (
                    body[:64].lstrip().lower().startswith((b"<!doctype html", b"<html"))
                )
                checksum = hashlib.sha256(body).hexdigest() == boundary["sha256"]
                matched = (
                    response.status == 206
                    and len(body) == end - start + 1
                    and response.headers.get("Content-Range")
                    == f"bytes {start}-{end}/{size}"
                    and checksum
                    and not html
                )
                emit(
                    "deployed_range",
                    start=start,
                    end=end,
                    status=response.status,
                    bytes=len(body),
                    content_range=response.headers.get("Content-Range"),
                    content_type=response.headers.get("Content-Type"),
                    content_length=response.headers.get("Content-Length"),
                    requested_encoding=encoding,
                    content_encoding=response.headers.get("Content-Encoding"),
                    transfer_encoding=response.headers.get("Transfer-Encoding"),
                    sha256_match=checksum,
                    html_body=html,
                    valid=matched,
                    seconds=round(time.perf_counter() - began, 3),
                )
                valid = valid and matched
        except Exception as error:
            emit(
                "deployed_range_failure",
                error_type=type(error).__name__,
                status=getattr(error, "code", None),
            )
            valid = False
    for encoding in ("identity", "gzip"):
        # A range response can retain length while a full 200 is transformed.
        # Only sample a bounded prefix; never download the complete public file.
        probe = request.Request(  # noqa: S310 - scheme checked.
            endpoint, headers={"Accept-Encoding": encoding}
        )
        try:
            with request.urlopen(probe, timeout=90) as response:  # noqa: S310
                body = response.read(65536)
                content_encoding = response.headers.get("Content-Encoding")
                prefix = body
                if content_encoding == "gzip":
                    prefix = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(
                        body, 65536
                    )
                html = (
                    prefix[:64]
                    .lstrip()
                    .lower()
                    .startswith((b"<!doctype html", b"<html"))
                )
                matched = (
                    response.status == 200
                    and response.headers.get("Content-Length") == str(size)
                    and response.headers.get("Accept-Ranges") == "bytes"
                    and content_encoding in (None, "identity")
                    and not html
                )
                emit(
                    "deployed_full_header_probe",
                    method="GET",
                    requested_encoding=encoding,
                    status=response.status,
                    content_length=response.headers.get("Content-Length"),
                    accept_ranges=response.headers.get("Accept-Ranges"),
                    content_encoding=content_encoding,
                    transfer_encoding=response.headers.get("Transfer-Encoding"),
                    wire_bytes_sampled=len(body),
                    sample_html=html,
                    full_body_read=False,
                    valid=matched,
                )
                valid = valid and matched
        except Exception as error:
            emit(
                "deployed_full_header_failure",
                requested_encoding=encoding,
                error_type=type(error).__name__,
                status=getattr(error, "code", None),
            )
            valid = False
    return valid


async def main(args):
    from telethon import types
    from telethon.errors import FloodWaitError

    from tgdrive.client_pool import ClientPool
    from tgdrive.logical_media import PartLease, part_ranges
    from tgdrive.media_reader import stream_pooled_media
    from tgdrive.ranges import ByteRange
    from tgdrive.telegram_clients import Client

    config = json.loads(args.config.read_text())
    directory = args.work_dir.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = directory / "owned-random-payload.bin"
    journal = directory / "owned-message.json"
    if payload.exists() or journal.exists():
        raise RuntimeError("Use an empty benchmark working directory")
    settings = SimpleNamespace(
        api_id=config["api_id"],
        api_hash=SimpleNamespace(get_secret_value=lambda: config["api_hash"]),
        resolved_data_dir=directory / "state",
        telegram_upload_pipeline=args.pipeline,
        telegram_request_limit=args.requests,
        telegram_parts_per_second=args.part_rate,
    )
    client = Client(
        name="isolated-validation", settings=settings, bot_token=config["bot_token"]
    )
    pool = ClientPool(
        [client],
        media_cache_dir=directory / "media-cache",
        media_per_bot=args.media_limit,
    )
    owned = []
    original_sent = client._sent

    async def observed_sent(channel, result):
        for update in result.updates:
            if isinstance(update, types.UpdateMessageID):
                owned.append(update.id)
        if owned:
            journal.write_text(json.dumps({"message_ids": owned}))
            journal.chmod(0o600)
        return await original_sent(channel, result)

    client._sent = observed_sent

    async def timed_read(label, iterator, expected_bytes, expected_hash):
        digest = hashlib.sha256()
        total = 0
        start = time.perf_counter()
        before = client._media_scheduler.snapshot()
        first_byte = None
        try:
            async for block in iterator:
                if first_byte is None:
                    first_byte = time.perf_counter() - start
                digest.update(block)
                total += len(block)
        except Exception as error:
            after = client._media_scheduler.snapshot()
            emit(
                label + "_failure",
                bytes=total,
                error_type=type(error).__name__,
                wait_seconds=getattr(error, "seconds", None),
                floods=after["floods"] - before["floods"],
                failed_attempts=after["failed"] - before["failed"],
            )
            raise
        finally:
            await iterator.aclose()
        elapsed = time.perf_counter() - start
        after = client._media_scheduler.snapshot()
        valid = total == expected_bytes and digest.hexdigest() == expected_hash
        emit(
            label,
            bytes=total,
            seconds=round(elapsed, 3),
            mib_s=round(total / MIB / elapsed, 3),
            sha256_match=valid,
            ttfb_seconds=round(first_byte, 4) if first_byte is not None else None,
            floods=after["floods"] - before["floods"],
            failed_attempts=after["failed"] - before["failed"],
            effective_parts_per_second=after["effective_parts_per_second"],
        )
        if not valid:
            raise RuntimeError("Benchmark read integrity mismatch")

    try:
        await pool.start()
        channel = config["channel"]
        peer = types.InputPeerChannel(-channel - 1000000000000, config["channel_hash"])
        await client.native.get_entity(peer)
        emit(
            "environment",
            home_dc=client.native.session.dc_id,
            endpoint=client.native.session.server_address,
            pipeline=args.pipeline,
            request_limit=args.requests,
            parts_per_second=args.part_rate,
            media_limit=args.media_limit,
        )
        size = args.payload_mib * MIB
        digest = hashlib.sha256()
        with payload.open("xb") as handle:
            for _ in range(args.payload_mib):
                block = os.urandom(MIB)
                handle.write(block)
                digest.update(block)
        expected = digest.hexdigest()
        start = time.perf_counter()
        before = client._media_scheduler.snapshot()
        message = await client.send_staged(
            channel,
            payload,
            file_name=f"TGDrive-validation-{secrets.token_hex(6)}.bin",
            random_id=secrets.randbits(63),
            expected_sha256=expected,
            caption="Temporary isolated TGDrive validation; automatically deleted.",
        )
        elapsed = time.perf_counter() - start
        after = client._media_scheduler.snapshot()
        emit(
            "upload",
            bytes=size,
            seconds=round(elapsed, 3),
            mib_s=round(size / MIB / elapsed, 3),
            file_dc=message.native.document.dc_id,
            floods=after["floods"] - before["floods"],
            failed_attempts=after["failed"] - before["failed"],
            effective_parts_per_second=after["effective_parts_per_second"],
        )
        rates = (
            []
            if args.skip_direct
            else dict.fromkeys([args.part_rate, *args.extra_download_rates])
        )
        for rate in rates:
            client.configure_transport(
                upload_pipeline=args.pipeline,
                request_limit=args.requests,
                parts_per_second=rate,
            )
            # The pooled reader owns retry/failover; preserve its separate probe.
            with suppress(FloodWaitError):
                await timed_read(
                    f"download_direct_rate_{rate}",
                    client.stream_media(
                        message, limit=math.ceil(size / MIB), offset=0, use_cache=False
                    ),
                    size,
                    expected,
                )
        client.configure_transport(
            upload_pipeline=args.pipeline,
            request_limit=args.requests,
            parts_per_second=args.part_rate,
        )
        for label in ("download_pooled_cold", "download_pooled_warm"):
            await timed_read(
                label,
                stream_pooled_media(
                    pool,
                    client,
                    message,
                    channel=channel,
                    limit=math.ceil(size / MIB),
                    offset=0,
                ),
                size,
                expected,
            )
        for limit in args.extra_media_limits:
            if limit == args.media_limit:
                continue
            if pool._media_read_cache is not None:
                await pool._media_read_cache.close()
            pool._media_read_cache = None
            pool.media_cache_dir = directory / f"media-cache-limit-{limit}"
            await pool.set_media_per_bot(limit)
            await timed_read(
                f"download_pooled_cold_media_{limit}",
                stream_pooled_media(
                    pool,
                    client,
                    message,
                    channel=channel,
                    limit=math.ceil(size / MIB),
                    offset=0,
                ),
                size,
                expected,
            )
        manifest = config.get("manifest")
        if manifest:
            boundaries = []
            for part in manifest["parts"][1:]:
                start = max(0, part["offset"] - MIB)
                end = min(manifest["size"] - 1, part["offset"] + MIB - 1)
                selected = ByteRange(start, end, manifest["size"], is_partial=True)
                boundary_digest = hashlib.sha256()
                boundary_bytes = 0
                for stored, interval in part_ranges(manifest["parts"], selected):
                    lease = await PartLease(pool, channel, stored).open()
                    iterator = lease.read(interval)
                    try:
                        async for block in iterator:
                            boundary_digest.update(block)
                            boundary_bytes += len(block)
                    finally:
                        await iterator.aclose()
                        await lease.close()
                emit(
                    "manifest_boundary",
                    start=start,
                    end=end,
                    bytes=boundary_bytes,
                    sha256=boundary_digest.hexdigest(),
                )
                boundaries.append(
                    dict(start=start, end=end, sha256=boundary_digest.hexdigest())
                )
            digest = hashlib.sha256()
            total = 0
            verified_parts = 0
            start = time.perf_counter()
            last_progress = start
            try:
                async with asyncio.timeout(args.manifest_timeout):
                    for part in manifest["parts"]:
                        lease = await PartLease(pool, channel, part).open()
                        iterator = lease.read(
                            ByteRange(
                                0,
                                part["size"] - 1,
                                part["size"],
                                is_partial=False,
                            )
                        )
                        part_digest = hashlib.sha256()
                        part_bytes = 0
                        try:
                            async for block in iterator:
                                digest.update(block)
                                part_digest.update(block)
                                total += len(block)
                                part_bytes += len(block)
                                now = time.perf_counter()
                                if now - last_progress >= 15:
                                    emit(
                                        "manifest_progress",
                                        bytes=total,
                                        total_bytes=manifest["size"],
                                        seconds=round(now - start, 1),
                                    )
                                    last_progress = now
                        finally:
                            await iterator.aclose()
                            await lease.close()
                        valid = (
                            part_bytes == part["size"]
                            and part_digest.hexdigest() == part["sha256"]
                        )
                        emit(
                            "manifest_part",
                            index=part["index"],
                            bytes=part_bytes,
                            sha256_match=valid,
                        )
                        if not valid:
                            raise RuntimeError(
                                "Existing manifest part checksum mismatch"
                            )
                        verified_parts += 1
                valid = (
                    total == manifest["size"]
                    and digest.hexdigest() == manifest["sha256"]
                )
                emit(
                    "manifest_complete",
                    bytes=total,
                    verified_parts=verified_parts,
                    seconds=round(time.perf_counter() - start, 3),
                    whole_sha256_match=valid,
                )
                if not valid:
                    raise RuntimeError("Existing logical file checksum mismatch")
            except TimeoutError:
                emit(
                    "manifest_timeout",
                    bytes=total,
                    total_bytes=manifest["size"],
                    verified_parts=verified_parts,
                    whole_sha256_match=None,
                    timeout_seconds=args.manifest_timeout,
                )
            if args.check_http:
                valid = await asyncio.to_thread(check_http, config, boundaries)
                if not valid:
                    raise RuntimeError("Deployed HTTP validation failed")
        stats = client._media_scheduler.snapshot()
        emit(
            "scheduler",
            completed=stats["completed"],
            failed=stats["failed"],
            floods=stats["floods"],
            bytes=stats["bytes"],
            effective_limit=stats["effective_limit"],
            effective_parts_per_second=stats["effective_parts_per_second"],
        )
    finally:
        if owned:
            try:
                await client.delete_messages(config["channel"], owned)
                journal.unlink(missing_ok=True)
                emit("cleanup_messages", deleted=len(owned), owned_only=True)
            except Exception as error:
                emit("cleanup_messages_failed", error_type=type(error).__name__)
        cache = pool._media_read_cache
        if cache is not None:
            await cache.close()
        await pool.stop(drain_timeout=15)
        payload.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--payload-mib", type=int, default=128)
    parser.add_argument("--pipeline", type=int, default=4)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--part-rate", type=int, default=48)
    parser.add_argument("--media-limit", type=int, default=3)
    parser.add_argument("--extra-media-limits", type=int, nargs="*", default=[])
    parser.add_argument("--extra-download-rates", type=int, nargs="*", default=[])
    parser.add_argument("--skip-direct", action="store_true")
    parser.add_argument("--manifest-timeout", type=int, default=600)
    parser.add_argument("--check-http", action="store_true")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        import uvloop
    except ImportError:
        pass
    else:
        uvloop.install()
    try:
        asyncio.run(main(arguments))
    except Exception as error:
        emit("failure", error_type=type(error).__name__)
        raise SystemExit(1) from None
