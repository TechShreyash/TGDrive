from pathlib import Path
from types import SimpleNamespace

import pytest

from tgdrive.observability.performance_api import system_snapshot
from tgdrive.settings.management import ConnectionManager


@pytest.mark.parametrize("probe_available", [True, False])
def test_system_snapshot_keeps_guest_totals_and_reports_backing_scope(
    tmp_path, monkeypatch, probe_available
):
    probe = tmp_path / "probe"
    if probe_available:
        probe.mkdir()

    def usage(path):
        return SimpleNamespace(
            total=100 if Path(path) == probe else 1000,
            used=80 if Path(path) == probe else 100,
            free=20 if Path(path) == probe else 900,
        )

    monkeypatch.setattr("tgdrive.storage.capacity.shutil.disk_usage", usage)
    settings = SimpleNamespace(
        resolved_data_dir=tmp_path,
        resolved_cache_dir=tmp_path,
        data_backing_capacity_dir=probe,
        cache_min_free_bytes=5,
        staging_budget_bytes=100,
    )
    disk = system_snapshot(settings)["disk"]
    assert disk["available"] is True
    assert disk["total_bytes"] == 1000
    assert disk["free_bytes"] == 900
    assert disk["capacity"]["backing"]["configured"] is True
    effective = disk["capacity"]["effective"]
    assert effective["available"] is probe_available
    assert effective["free_bytes"] == (20 if probe_available else None)
    assert effective["headroom_bytes"] == (15 if probe_available else None)


def test_connection_stats_keep_guest_free_separate_from_unavailable_backing(tmp_path):
    manager = object.__new__(ConnectionManager)
    manager.settings = SimpleNamespace(
        resolved_cache_dir=tmp_path,
        cache_backing_capacity_dir=tmp_path / "missing",
        min_free_bytes=5,
        staging_budget_bytes=100,
    )
    manager.runtime = SimpleNamespace(
        mongo=SimpleNamespace(
            db=SimpleNamespace(receipts=SimpleNamespace(find=lambda _: []))
        )
    )
    stats = manager._stats()
    assert stats["free_disk_bytes"] > 0
    assert stats["disk_capacity"]["effective"] == {
        "available": False,
        "total_bytes": None,
        "free_bytes": None,
        "headroom_bytes": None,
    }
