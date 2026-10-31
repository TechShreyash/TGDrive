import asyncio
from types import SimpleNamespace

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
    return dict(values=base | values, raised_at=0)


def signals(at, floods=0, timeouts=0, sent=0):
    return dict(at=at, floods=floods, timeouts=timeouts, bytes=sent)


def test_memory_pressure_shrinks_cache_with_hysteresis():
    first = decide(measurements(), state(), None, 0)
    assert first["values"]["cache_memory_bytes"] == 2 * GIB
    assert first["values"]["staging_budget_bytes"] == 45 * GIB
    assert first["values"]["min_free_bytes"] == 5 * GIB
    steady = decide(measurements(int(7.8 * GIB)), first, None, 5)
    assert steady["values"]["cache_memory_bytes"] == 2 * GIB
    tight = decide(measurements(2 * GIB), steady, None, 10)
    assert tight["values"]["cache_memory_bytes"] == GIB // 2
    assert "25%" in tight["reasons"]["cache_memory_bytes"]


def test_floodwait_halves_and_cpu_caps_preload():
    previous = state(preload_max_jobs=9) | {"signals": signals(0)}
    result = decide(measurements(), previous, signals(5, floods=1), 100)
    assert result["values"]["telegram_request_limit"] == 2
    assert result["values"]["preload_max_jobs"] == 4
    result = decide(
        measurements(),
        result | {"values": result["values"] | dict.fromkeys(CONCURRENCY, 1)},
        signals(10, floods=1, timeouts=1),
        200,
    )
    assert result["values"]["telegram_request_limit"] == 1


def test_improvement_adds_one_only_after_cooldown():
    previous = state() | {"signals": signals(0), "raised_at": 0}
    early = decide(measurements(), previous, signals(5, sent=100), 30)
    assert early["values"]["telegram_upload_pipeline"] == 4
    later = decide(measurements(), early, signals(10, sent=200), 61)
    assert later["values"]["telegram_upload_pipeline"] == 5
    again = decide(measurements(), later, signals(15, sent=300), 70)
    assert again["values"]["telegram_upload_pipeline"] == 5


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

    settings = SimpleNamespace(resolved_data_dir="/", telegram_uploads_per_bot=2)
    scheduler = SimpleNamespace(floods=0, timeouts=0, bytes=0)
    uploads = []

    async def set_uploads_per_bot(value):
        uploads.append(value)

    runtime = SimpleNamespace(
        pool=SimpleNamespace(
            _slots=[
                SimpleNamespace(client=SimpleNamespace(_media_scheduler=scheduler))
            ],
            set_uploads_per_bot=set_uploads_per_bot,
        )
    )
    item = Governor(settings, runtime)
    monkeypatch.setattr(item, "_service", Service)
    monkeypatch.setattr(governor, "measure", lambda _: measurements())

    asyncio.run(item.tick(now=0))
    assert len(applied) == 1
    assert applied[0]["cache_memory_bytes"] == 2 * GIB
    asyncio.run(item.tick(now=5))
    assert len(applied) == 1
    scheduler.floods = 1
    asyncio.run(item.tick(now=10))
    assert applied[-1]["telegram_request_limit"] == 1
    assert item.snapshot()["reasons"]["telegram_request_limit"].startswith("halved")
    # Uploads per bot is tuned too, through the pool's live setter.
    assert uploads == [1] and settings.telegram_uploads_per_bot == 1
    # The largest accepted file follows the staging budget; no fixed cap.
    assert applied[0]["max_file_size"] == applied[0]["staging_budget_bytes"]
