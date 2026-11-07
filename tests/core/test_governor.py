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
    assert cpus(str(cpu)) == 2
    cpu.write_text("max 100000\n")
    assert cpus(str(cpu)) >= 1
    assert cpus(str(tmp_path / "missing")) >= 1


def test_missing_memory_metrics_preserve_cache_and_allow_disk_policy(tmp_path):
    assert memory(str(tmp_path), str(tmp_path / "missing")) == (None, None)
    sample = measurements() | {"memory_limit": None, "memory_available": None}
    result = decide(sample, state(cache_memory_bytes=1024))
    assert result["values"]["cache_memory_bytes"] == 1024
    assert result["values"]["staging_budget_bytes"] == 45 * GIB


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
    assert first["values"]["cache_memory_bytes"] == 2 * GIB
    assert first["values"]["staging_budget_bytes"] == 45 * GIB
    assert first["values"]["min_free_bytes"] == 5 * GIB
    steady = decide(measurements(int(7.8 * GIB)), first)
    assert steady["values"]["cache_memory_bytes"] == 2 * GIB
    tight = decide(measurements(2 * GIB), steady)
    assert tight["values"]["cache_memory_bytes"] == GIB // 2
    assert "25%" in tight["reasons"]["cache_memory_bytes"]


def test_staging_and_media_cache_use_their_own_disk_budgets():
    sample = measurements() | {
        "cache_disk_total": 20 * GIB,
        "cache_disk_free": 5 * GIB,
    }
    result = decide(sample, state())
    assert result["values"]["staging_budget_bytes"] == 45 * GIB
    assert result["values"]["min_free_bytes"] == 5 * GIB
    assert result["values"]["cache_disk_bytes"] == 2 * GIB
    assert result["values"]["cache_min_free_bytes"] == GIB


def test_measure_finds_the_media_volume_before_the_cache_exists(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    media = tmp_path / "data"
    media.mkdir()
    measured = []

    def usage(path):
        measured.append(path)
        return SimpleNamespace(total=100 if path == staging else 20, free=10)

    monkeypatch.setattr(governor.shutil, "disk_usage", usage)
    sample = governor.measure(staging, media / "media-cache")
    assert measured == [staging, media]
    assert sample["disk_total"] == 100
    assert sample["cache_disk_total"] == 20


def test_preload_resource_cap_recovers_only_to_its_startup_ceiling():
    previous = state(preload_max_jobs=8)
    result = decide(measurements(), previous)
    assert result["values"]["telegram_request_limit"] == 4
    assert result["values"]["preload_max_jobs"] == 4
    result = decide(measurements() | {"cpus": 4}, result)
    assert result["values"]["preload_max_jobs"] == 8
    assert result["values"]["telegram_request_limit"] == 4


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
    assert applied[0]["cache_memory_bytes"] == 2 * GIB
    asyncio.run(item.tick())
    assert len(applied) == 1
    assert applied[-1]["telegram_request_limit"] == 2
    assert item.snapshot()["reasons"]["telegram_request_limit"] == "startup ceiling (2)"
    assert uploads == [] and settings.telegram_uploads_per_bot == 2
    assert measured_paths == [("/", Path("/data/media-cache"))] * 2
    runtime.mongo.db.command.assert_not_called()
    # The largest accepted file follows the staging budget; no fixed cap.
    assert applied[0]["max_file_size"] == applied[0]["staging_budget_bytes"]
