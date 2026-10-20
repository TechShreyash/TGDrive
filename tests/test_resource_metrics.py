import pytest

from tgdrive.resource_metrics import ResourceMetrics


def setup_counters(tmp_path, monkeypatch, *, quota="200000 100000"):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "net").mkdir(parents=True)
    cgroup.mkdir()
    (cgroup / "cpu.max").write_text(quota)
    (cgroup / "cpu.stat").write_text("usage_usec 1000000\n")
    (proc / "stat").write_text("cpu 100 0 0 100 0 0 0 0 0 0\n")
    (proc / "net/route").write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
        "eth0 00000000 0100000A 0003 0 0 100 00000000\n"
    )
    network(proc, 1000, 2000)
    monkeypatch.setattr("tgdrive.resource_metrics.os.cpu_count", lambda: 8)
    monkeypatch.setattr(
        "tgdrive.resource_metrics.os.sched_getaffinity", lambda _: set(range(8))
    )
    now = [100.0]
    monkeypatch.setattr("tgdrive.resource_metrics.time.monotonic", lambda: now[0])
    return ResourceMetrics(proc=proc, cgroup=cgroup), proc, cgroup, now


def network(proc, received, sent):
    (proc / "net/dev").write_text(
        f"eth0: {received} 0 0 0 0 0 0 0 {sent} 0 0 0 0 0 0 0\n"
        "lo: 999999999 0 0 0 0 0 0 0 999999999 0 0 0 0 0 0 0\n"
        "docker0: 999999999 0 0 0 0 0 0 0 999999999 0 0 0 0 0 0 0\n"
    )


def test_current_cpu_respects_container_quota_and_network_counts_url_like_ingress(
    tmp_path, monkeypatch
):
    metrics, proc, cgroup, now = setup_counters(tmp_path, monkeypatch)
    first = metrics.sample()
    assert first["cpu"]["percent"] is None
    assert first["network"]["received_bytes"] == 0
    now[0] += 2
    (cgroup / "cpu.stat").write_text("usage_usec 3000000\n")
    network(proc, 1000 + 1024**3, 6000)
    sample = metrics.sample()
    assert sample["cpu"] == {"percent": 50, "scope": "cgroup", "capacity_cores": 2}
    assert sample["network"]["received_bytes"] == 1024**3
    assert sample["network"]["received_bytes_per_second"] == 1024**3 / 2
    assert sample["network"]["sent_bytes_per_second"] == 2000
    assert sample["network"]["interface"] == "eth0"
    # Finished URL import: current rate is zero; the observed total remains.
    now[0] += 3
    idle = metrics.sample()["network"]
    assert idle["received_bytes_per_second"] == 0
    assert idle["received_bytes"] == 1024**3


def test_missing_cgroup_uses_host_busy_cpu_not_load_average(tmp_path, monkeypatch):
    metrics, proc, cgroup, now = setup_counters(tmp_path, monkeypatch)
    (cgroup / "cpu.stat").unlink()
    metrics.sample()
    now[0] += 3
    (proc / "stat").write_text("cpu 150 0 0 150 0 0 0 0 0 0\n")
    assert metrics.sample()["cpu"] == {"percent": 50, "scope": "host"}


@pytest.mark.parametrize("elapsed", [-3, 40])
def test_gap_clock_jump_and_reset_do_not_fabricate_resource_rates(
    tmp_path, monkeypatch, elapsed
):
    metrics, proc, _cgroup, now = setup_counters(tmp_path, monkeypatch)
    metrics.sample()
    now[0] += elapsed
    network(proc, 1500, 3000)
    value = metrics.sample()
    assert value["cpu"]["percent"] is None
    assert value["network"]["received_bytes_per_second"] is None
    now[0] += 3
    network(proc, 10, 20)
    assert metrics.sample()["network"]["sent_bytes_per_second"] is None
    network(proc, 100, 200)
    now[0] += 3
    assert metrics.sample()["network"]["received_bytes_per_second"] == 30


def test_unavailable_metrics_are_unknown_not_zero(tmp_path):
    value = ResourceMetrics(proc=tmp_path, cgroup=tmp_path).sample()
    assert value["cpu"]["percent"] is None
    assert not value["network"]["available"]
    assert value["network"]["received_bytes_per_second"] is None
