import asyncio
import shutil

import pytest

from tests.support.book_samples import _pdf_objects, _pdf_stream
from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_archives import dispose, finish, manager_for, source
from tgdrive.core import encryption
from tgdrive.transfers.archives import start_pdf_password
from tgdrive.transfers.tasks import TransferStatus

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("qpdf", "prlimit", "pdftotext")),
    reason="Requires the native PDF tools included in Docker",
)


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


async def run_tool(name, *arguments):
    executable = shutil.which(name)
    assert executable
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


async def test_add_change_and_remove_password_keep_page_text_and_source(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(encryption, "_key", bytes(32))
    data = _pdf_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            _pdf_stream(b"BT /F1 18 Tf 50 180 Td (The page stays the same.) Tj ET"),
        ]
    )
    manager, ledger, _ = await manager_for(tmp_path)
    initial = source(manager, ledger, "Original.pdf", data)
    original_path = manager.store.resolve_id(initial["id"])
    path = original_path
    try:
        for operation, current, new in (
            ("add", None, "first secret"),
            ("change", "first secret", "second secret"),
            ("remove", "second secret", None),
        ):
            job = await start_pdf_password(
                manager,
                path,
                "/",
                operation,
                password="incorrect" if operation == "change" else current,
                new_password=new,
            )
            await finish(job)
            if operation == "change":
                assert job.status is TransferStatus.ERROR, job.error
                assert job.error == "The current PDF password is incorrect."
                assert not manager.store.has_file_operation(job.id)
                assert job.as_dict()["pdf_password_operation"] == "change"
                with pytest.raises(ValueError, match="current PDF password"):
                    await manager.retry_job(job.id)
                await manager.retry_job(job.id, password=current, new_password=new)
                await finish(job)
            assert job.status is TransferStatus.COMPLETED, job.error
            received = manager.store.transfer_location(job.id, "/", completed=True)
            path = manager.store.resolve_id(received["item_id"])
            file = manager.store.get_file(path)
            message = ledger.messages[file["message_id"]]
            rendered = tmp_path / "check.pdf"
            rendered.write_bytes(message.content)
            denied, _, _ = await run_tool("qpdf", str(rendered), "--requires-password")
            assert denied == (0 if new else 2)
            code, text, error = await run_tool(
                "pdftotext", "-upw", new or "", str(rendered), "-"
            )
            assert code == 0, error
            assert text.strip() == b"The page stays the same."
            if current:
                denied, _, _ = await run_tool(
                    "qpdf",
                    str(rendered),
                    "--password=" + current,
                    "--requires-password",
                )
                assert denied == (0 if new else 2)
            assert "first secret" not in repr(job.context)
            assert "second secret" not in repr(job.context)
            message.content = encryption.crypt(
                file["parts"][0]["nonce"], 0, message.content
            )
        original = manager.store.get_file(original_path)
        assert ledger.messages[original["message_id"]].content == data
        before = manager.store.list_directory("/")
        manager.settings.upload_limit = 128
        failed = await start_pdf_password(
            manager,
            original_path,
            "/",
            "add",
            password=None,
            new_password="write limit",
        )
        await finish(failed)
        assert failed.status is TransferStatus.ERROR
        assert "output size" in (failed.error or "")
        assert manager.store.list_directory("/") == before
        assert not manager.store.has_file_operation(failed.id)
        directory = manager.cache_dir / "archives" / failed.id
        assert list(directory.iterdir()) == [directory / "source.pdf"]
        assert (directory / "source.pdf").read_bytes() == data
        assert ledger.messages[original["message_id"]].content == data
    finally:
        await dispose(manager)
