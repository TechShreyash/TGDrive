from pathlib import Path
from types import SimpleNamespace

import pytest

from tgdrive.storage import capacity


def measured(tmp_path, monkeypatch, *, backing_free=20):
    guest, probe = tmp_path / "guest", tmp_path / "probe"
    guest.mkdir()
    probe.mkdir()
    monkeypatch.setattr(
        capacity.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(
            total=1000 if Path(path) == guest else 100,
            used=100 if Path(path) == guest else 100 - backing_free,
            free=900 if Path(path) == guest else backing_free,
        ),
    )
    return guest, probe


def test_backing_limits_headroom_without_replacing_guest_totals(tmp_path, monkeypatch):
    guest, probe = measured(tmp_path, monkeypatch)
    result = capacity.measure_capacity(guest, probe_directory=probe)
    snapshot = result.as_dict(reserve_bytes=5, outstanding_bytes=3)
    assert snapshot["filesystem"]["total_bytes"] == 1000
    assert snapshot["filesystem"]["free_bytes"] == 900
    assert snapshot["backing"]["free_bytes"] == 20
    assert snapshot["effective"] == {
        "available": True,
        "total_bytes": 100,
        "free_bytes": 20,
        "headroom_bytes": 12,
    }
    assert snapshot["scope"] == "backing_filesystem"
    assert result.probe_directory == probe.resolve()
    assert len(result.constraint_ids) == 1  # Both paths really share this tmp FS.
    capacity.require_headroom(result, 12, reserve_bytes=5, outstanding_bytes=3)
    with pytest.raises(OSError) as error:
        capacity.require_headroom(result, 13, reserve_bytes=5, outstanding_bytes=3)
    assert error.value.errno == 28


def test_unconfigured_backing_is_unknown_not_failed(tmp_path, monkeypatch):
    guest, _ = measured(tmp_path, monkeypatch)
    result = capacity.measure_capacity(guest)
    snapshot = result.as_dict(reserve_bytes=5)
    assert snapshot["scope"] == "filesystem_only"
    assert snapshot["backing"]["configured"] is False
    assert snapshot["backing"]["available"] is False
    assert snapshot["effective"]["headroom_bytes"] == 895


@pytest.mark.parametrize("kind", ["missing", "file", "nonempty", "symlink"])
def test_configured_invalid_probe_never_falls_back_to_guest(
    tmp_path, monkeypatch, kind
):
    guest, probe = measured(tmp_path, monkeypatch)
    if kind == "missing":
        probe.rmdir()
    elif kind == "file":
        probe.rmdir()
        probe.write_text("synthetic")
    elif kind == "nonempty":
        (probe / "entry").touch()
    else:
        probe.rmdir()
        probe.symlink_to(guest, target_is_directory=True)
    result = capacity.measure_capacity(guest, probe_directory=probe)
    snapshot = result.as_dict()
    assert snapshot["filesystem"]["free_bytes"] == 900
    assert snapshot["scope"] == "backing_unavailable"
    assert snapshot["effective"] == {
        "available": False,
        "total_bytes": None,
        "free_bytes": None,
        "headroom_bytes": None,
    }
    with pytest.raises(capacity.CapacityUnavailable):
        capacity.require_headroom(result, 0)


def test_measured_zero_is_distinct_from_unavailable(tmp_path, monkeypatch):
    guest, probe = measured(tmp_path, monkeypatch, backing_free=0)
    result = capacity.measure_capacity(guest, probe_directory=probe)
    assert result.as_dict()["effective"] == {
        "available": True,
        "total_bytes": 100,
        "free_bytes": 0,
        "headroom_bytes": 0,
    }
    with pytest.raises(OSError) as error:
        capacity.require_headroom(result, 1)
    assert error.value.errno == 28


def test_zero_required_bytes_still_enforces_existing_reserve(tmp_path, monkeypatch):
    guest, probe = measured(tmp_path, monkeypatch, backing_free=2)
    result = capacity.measure_capacity(guest, probe_directory=probe)
    assert capacity.usable_headroom(result, 3) == 0
    with pytest.raises(OSError) as error:
        capacity.require_headroom(result, 0, reserve_bytes=3)
    assert error.value.errno == 28


def test_probe_read_failure_is_unavailable_not_zero(tmp_path, monkeypatch):
    guest, probe = measured(tmp_path, monkeypatch)
    original = capacity.shutil.disk_usage

    def usage(path):
        if Path(path) == probe:
            raise PermissionError("synthetic")
        return original(path)

    monkeypatch.setattr(capacity.shutil, "disk_usage", usage)
    result = capacity.measure_capacity(guest, probe_directory=probe)
    assert result.effective_free_bytes is None
    assert result.as_dict()["filesystem"]["available"] is True
    with pytest.raises(capacity.CapacityUnavailable):
        capacity.require_headroom(result, 1)


def test_different_device_ids_are_reported_without_inferred_host_independence(
    tmp_path, monkeypatch
):
    guest, probe = measured(tmp_path, monkeypatch)
    original = capacity._measure

    def usage(path):
        value, _ = original(path)
        return value, 11 if Path(path) == guest else 22

    monkeypatch.setattr(capacity, "_measure", usage)
    result = capacity.measure_capacity(guest, probe_directory=probe)
    assert result.constraint_ids == {11, 22}
    assert result.effective_free_bytes == 20


def test_probe_selection_follows_explicit_most_specific_roots(tmp_path):
    settings = SimpleNamespace(
        resolved_data_dir=tmp_path / "data",
        resolved_cache_dir=tmp_path / "data" / "cache",
        data_backing_capacity_dir=tmp_path / "data-probe",
        cache_backing_capacity_dir=tmp_path / "cache-probe",
    )
    assert (
        capacity.probe_for_directory(settings, tmp_path / "data" / "new" / "directory")
        == settings.data_backing_capacity_dir
    )
    assert (
        capacity.probe_for_directory(settings, tmp_path / "data" / "cache" / "new")
        == settings.cache_backing_capacity_dir
    )
    with pytest.raises(capacity.CapacityUnavailable):
        capacity.probe_for_directory(settings, tmp_path / "outside")


def test_equal_root_probe_conflict_fails_unless_shared_identity_is_proven(
    tmp_path, monkeypatch
):
    settings = SimpleNamespace(
        resolved_data_dir=tmp_path,
        resolved_cache_dir=tmp_path,
        data_backing_capacity_dir=tmp_path / "one",
        cache_backing_capacity_dir=tmp_path / "two",
    )
    with pytest.raises(capacity.CapacityUnavailable):
        capacity.probe_for_directory(settings, tmp_path / "new")
    settings.data_backing_capacity_dir.mkdir()
    settings.cache_backing_capacity_dir.mkdir()
    assert capacity.probe_for_directory(settings, tmp_path / "new") in {
        settings.data_backing_capacity_dir,
        settings.cache_backing_capacity_dir,
    }
    monkeypatch.setattr(
        capacity,
        "_measure",
        lambda path: (capacity._Usage(100, 0, 100), 1 if path.name == "one" else 2),
    )
    with pytest.raises(capacity.CapacityUnavailable):
        capacity.probe_for_directory(settings, tmp_path / "new")


def test_unconfigured_outside_target_keeps_filesystem_only_behavior(tmp_path):
    assert capacity.probe_for_directory(SimpleNamespace(), tmp_path / "new") is None
