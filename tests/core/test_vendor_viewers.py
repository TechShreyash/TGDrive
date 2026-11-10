"""Reject an archive directory where package metadata must be a file."""

import pytest

from scripts import vendor_viewers


def test_viewer_metadata_must_be_a_regular_file(monkeypatch, tmp_path):
    monkeypatch.setattr(vendor_viewers, "upstream_bytes", lambda *_: b"fixture")
    monkeypatch.setattr(
        vendor_viewers,
        "package_entries",
        lambda _: {"package/package.json": vendor_viewers.Entry(None, 0o755)},
    )

    with pytest.raises(ValueError, match="Expected regular package metadata"):
        vendor_viewers.generate(tmp_path, tmp_path / "output", "node", None)
