"""Bounded QPDF rewrites. Passwords travel only through stdin."""

import asyncio
import json
import os
import secrets
import shutil
import signal
from contextlib import suppress
from typing import Any

from tgdrive.transfers.manager import TransferCancelled, TransferError

MIB = 1024**2
MAX_SOURCE_BYTES = 256 * MIB


def validate_passwords(operation, password, new_password):
    for value in (password, new_password):
        if value is not None and (
            not value or len(value.encode("utf-8")) > 127 or chr(0) in value
        ):
            raise ValueError("Use PDF passwords of 1 to 127 UTF-8 bytes without NUL.")
    if operation in {"change", "remove"} and not password:
        raise ValueError("Enter the current PDF password.")
    if operation in {"add", "change"} and not new_password:
        raise ValueError("Enter a new PDF password.")
    if operation == "change" and password == new_password:
        raise ValueError("Enter a different new PDF password.")


def qpdf_executable():
    path = shutil.which("qpdf")
    if not path or not shutil.which("prlimit"):
        raise ValueError("PDF password changes require qpdf and prlimit on the server.")
    return path


async def rewrite_pdf(
    source, output, operation, password, new_password, *, limit, cancelled
):
    executable = qpdf_executable()

    async def run(options):
        process = await asyncio.create_subprocess_exec(
            "prlimit",
            f"--fsize={limit}",
            f"--as={512 * MIB}",
            "--cpu=60",
            "--core=0",
            "--",
            executable,
            "--job-json-file=/dev/stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        work = asyncio.create_task(process.communicate(json.dumps(options).encode()))
        cancellation = asyncio.create_task(cancelled.wait())
        try:
            async with asyncio.timeout(90):
                await asyncio.wait(
                    (work, cancellation), return_when=asyncio.FIRST_COMPLETED
                )
            if cancelled.is_set():
                raise TransferCancelled("Transfer cancelled")
            await work
            return process.returncode
        except TimeoutError as error:
            raise TransferError("The PDF took too long to process.") from error
        finally:
            cancellation.cancel()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.gather(work, cancellation, return_exceptions=True)

    options: dict[str, Any] = {"inputFile": str(source)}
    if password:
        options["password"] = password
    # QPDF reports password status without writing an output or echoing the password.
    status = await run(options | {"requiresPassword": ""})
    if status not in {0, 2, 3}:
        raise TransferError("Could not check the PDF password protection.")
    if status != 2 and operation == "add":
        raise TransferError("This PDF already has protection. Choose Change password.")
    if status == 2 and operation != "add":
        raise TransferError("This PDF has no password to change or remove.")
    if status == 0:
        raise TransferError("The current PDF password is incorrect.")
    options["outputFile"] = str(output)
    if operation == "remove":
        options["decrypt"] = ""
    else:
        options["encrypt"] = {
            "userPassword": new_password,
            "ownerPassword": secrets.token_urlsafe(32),
            "256bit": {},
        }
    written = await run(options)
    if written == -signal.SIGXFSZ:
        raise TransferError("The PDF copy exceeds the allowed output size.")
    if written != 0:
        raise TransferError(
            "Could not save the PDF copy. Check the file and free storage."
        )
