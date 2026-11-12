import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tgdrive.core import governor
from tgdrive.core.governor import CONCURRENCY, GIB, Governor, cpus, decide, memory


def test_cgroup_parsing_handles_max_and_fallback(tmp_path):
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "memory.current").write_text("100\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8 kB\nMemFree: 1 kB\nMemAvailable: 4 kB\n")
    assert memory(str(tmp_path), str(meminfo)) == (8192, 4096)
    (tmp_path / "memory.max").write_text("1000\n")
    assert memory(str(tmp_path), str(meminfo)) == (1000, 900)
    cpu = tmp_path / "cpu.max"
    cpu.write_text("200000 100000\n")
    assert cpus(str(cpu)) == 2
    cpu.write_text("150000 100000\n")
    assert cpus(str(cpu)) == 1.5
    cpu.write_text("max 100000\n")
    capacity = cpus(str(cpu))
    assert capacity is not None and capacity > 0
    capacity = cpus(str(tmp_path / "missing"))
    assert capacity is not None and capacity > 0


def test_missing_memory_metrics_preserve_cache_and_allow_disk_policy(tmp_path):
    assert memory(str(tmp_path), str(tmp_path / "missing")) == (None, None)
    sample = measurements() | {"memory_limit": None, "memory_available": None}
    result = decide(sample, state(cache_memory_bytes=1024))
    assert result["values"]["cache_memory_bytes"] == 1024
    assert result["values"]["staging_budget_bytes"] == 16 * GIB


def measurements(available=8 * GIB):
    return dict(
        memory_limit=16 * GIB,
        memory_available=available,
        cpus=1,
        disk_total=100 * GIB,
        disk_free=50 * GIB,
    )


def state(**values):
    base = dict.fromkeys(CONCURRENCY, 4) | {"cache_memory_bytes": 0}
    return dict(values=base | values)


def test_memory_pressure_shrinks_cache_with_hysteresis():
    first = decide(measurements(), state())
    assert first["values"]["cache_memory_bytes"] == 256 * 1024**2
    assert first["values"]["staging_budget_bytes"] == 16 * GIB
    assert first["values"]["min_free_bytes"] == 5 * GIB
    steady = decide(measurements(int(7.8 * GIB)), first)
    assert steady["values"]["cache_memory_bytes"] == 256 * 1024**2
    tight = decide(measurements(GIB // 2), steady)
    assert tight["values"]["cache_memory_bytes"] == GIB // 8
    assert "25%" in tight["reasons"]["cache_memory_bytes"]


def test_staging_and_media_cache_use_their_own_disk_budgets():
    sample = measurements() | {
        "cache_disk_total": 20 * GIB,
        "cache_disk_free": 5 * GIB,
    }
    result = decide(sample, state())
    assert result["values"]["staging_budget_bytes"] == 16 * GIB
    assert result["values"]["min_free_bytes"] == 5 * GIB
    assert result["values"]["cache_disk_bytes"] == 2 * GIB
    assert result["values"]["cache_min_free_bytes"] == GIB


def test_measure_finds_the_media_volume_before_the_cache_exists(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    media = tmp_path / "data"
    media.mkdir()
    measured = []

    def usage(path):
        measured.append(path)
        return SimpleNamespace(total=100 if path == staging else 20, used=5, free=10)

    monkeypatch.setattr("tgdrive.storage.capacity.shutil.disk_usage", usage)
    sample = governor.measure(staging, media / "media-cache")
    assert measured == [staging, media]
    assert sample["disk_total"] == 100
    assert sample["cache_disk_total"] == 20


def test_reserve_uses_verified_backing_total_with_backing_free(tmp_path, monkeypatch):
    staging, media, probe = (tmp_path / name for name in ("staging", "media", "probe"))
    for directory in (staging, media, probe):
        directory.mkdir()

    def usage(path):
        total = 20 * GIB if path == probe else 1000 * GIB
        return SimpleNamespace(total=total, used=0, free=total)

    monkeypatch.setattr("tgdrive.storage.capacity.shutil.disk_usage", usage)
    sample = governor.measure(
        staging, media, cache_probe_directory=probe, data_probe_directory=probe
    )
    assert sample["disk_total"] == sample["cache_disk_total"] == 20 * GIB
    assert sample["disk_effective_free"] == 20 * GIB
    result = decide(sample, state())
    assert result["values"]["min_free_bytes"] == GIB
    assert result["values"]["cache_min_free_bytes"] == GIB
    assert result["values"]["staging_budget_bytes"] == 16 * GIB


def test_preload_resource_cap_recovers_only_to_its_startup_ceiling():
    previous = state(preload_max_jobs=8)
    result = decide(measurements(), previous)
    assert result["values"]["telegram_request_limit"] == 4
    assert result["values"]["preload_max_jobs"] == 4
    result = decide(measurements() | {"cpus": 4}, result)
    assert result["values"]["preload_max_jobs"] == 8
    assert result["values"]["telegram_request_limit"] == 4


def test_fractional_cpu_capacity_does_not_round_up_preload_jobs():
    result = decide(measurements() | {"cpus": 0.6}, state(preload_max_jobs=8))
    assert result["values"]["preload_max_jobs"] == 2
    result = decide(measurements() | {"cpus": None}, result)
    assert result["values"]["preload_max_jobs"] == 8


def test_repeated_samples_do_not_change_unrelated_concurrency():
    result = state()
    for available in (8 * GIB, GIB, 16 * GIB) * 20:
        result = decide(measurements(available), result)
        assert all(result["values"][key] == 4 for key in CONCURRENCY)


def test_tick_applies_through_performance_settings(monkeypatch):
    applied = []

    class Service:
        _lock = asyncio.Lock()
        value = SimpleNamespace(
            **dict.fromkeys(CONCURRENCY, 2),
            **dict.fromkeys(governor.BYTES, GIB),
            model_copy=lambda update: update,
        )

        async def _apply(self, value):
            applied.append(value)
            for key, item in value.items():
                setattr(settings, key, item)
                setattr(self.value, key, item)

    settings = SimpleNamespace(
        resolved_cache_dir="/",
        resolved_data_dir=Path("/data"),
        telegram_uploads_per_bot=2,
    )
    uploads = []
    measured_paths = []

    async def set_uploads_per_bot(value):
        uploads.append(value)

    runtime = SimpleNamespace(
        mongo=SimpleNamespace(db=SimpleNamespace(command=Mock())),
        pool=SimpleNamespace(
            set_uploads_per_bot=set_uploads_per_bot,
        ),
    )
    item = Governor(settings, runtime)
    monkeypatch.setattr(item, "_service", Service)
    monkeypatch.setattr(
        governor,
        "measure",
        lambda path, media: measured_paths.append((path, media)) or measurements(),
    )

    asyncio.run(item.tick())
    assert len(applied) == 1
    assert applied[0]["cache_memory_bytes"] == 256 * 1024**2
    asyncio.run(item.tick())
    assert len(applied) == 1
    assert applied[-1]["telegram_request_limit"] == 2
    assert item.snapshot()["reasons"]["telegram_request_limit"] == "startup ceiling (2)"
    assert uploads == [] and settings.telegram_uploads_per_bot == 2
    assert measured_paths == [("/", Path("/data/media-cache"))] * 2
    runtime.mongo.db.command.assert_not_called()
    # The largest accepted file follows the staging budget; no fixed cap.
    assert applied[0]["max_file_size"] == applied[0]["staging_budget_bytes"]


def test_owner_ceilings_bound_automatic_budgets_and_pressure():
    policy = dict(
        cache_disk_limit_bytes=3 * GIB,
        cache_memory_limit_bytes=100 * 1024**2,
        staging_limit_bytes=7 * GIB,
        protected_free_bytes=0,
    )
    result = decide(measurements(), state(), policy)
    assert result["values"]["cache_disk_bytes"] == 3 * GIB
    assert result["values"]["cache_memory_bytes"] == 100 * 1024**2
    assert result["values"]["staging_budget_bytes"] == 7 * GIB
    assert result["values"]["max_file_size"] == 7 * GIB
    assert result["values"]["min_free_bytes"] == 0
    assert result["values"]["cache_min_free_bytes"] == 0
    lowered = policy | {
        "cache_disk_limit_bytes": 29 * GIB // 10,
        "cache_memory_limit_bytes": 99 * 1024**2,
    }
    result = decide(measurements(), result, lowered)
    assert result["values"]["cache_disk_bytes"] == lowered["cache_disk_limit_bytes"]
    assert result["values"]["cache_memory_bytes"] == lowered["cache_memory_limit_bytes"]
    disabled = decide(
        measurements(),
        result,
        policy | {"cache_disk_limit_bytes": 0, "cache_memory_limit_bytes": 0},
    )
    assert disabled["values"]["cache_disk_bytes"] == 0
    assert disabled["values"]["cache_memory_bytes"] == 0
    tight = decide(measurements() | {"disk_free": GIB}, result, policy)
    assert tight["values"]["staging_budget_bytes"] == GIB


def test_automatic_ceilings_do_not_inherit_effective_values():
    result = decide(
        measurements(), state(cache_disk_bytes=400 * GIB, cache_memory_bytes=2 * GIB)
    )
    assert result["values"]["cache_disk_bytes"] == 10 * GIB
    assert result["values"]["cache_memory_bytes"] == 256 * 1024**2
    assert result["values"]["staging_budget_bytes"] == 16 * GIB


def test_effective_backing_headroom_limits_governor_and_failure_disables_retention():
    sample = measurements() | {
        "disk_effective_free": 6 * GIB,
        "cache_disk_effective_free": 7 * GIB,
    }
    result = decide(sample, state())
    assert result["values"]["staging_budget_bytes"] == GIB
    assert result["values"]["cache_disk_bytes"] == GIB
    unavailable = decide(
        sample
        | {
            "disk_effective_free": None,
            "cache_disk_effective_free": None,
        },
        result,
    )
    assert unavailable["values"]["cache_disk_bytes"] == 0
    assert unavailable["values"]["staging_budget_bytes"] == 1
