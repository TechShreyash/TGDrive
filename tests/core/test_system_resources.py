import pytest

from tgdrive.core.system_resources import cpu_snapshot, memory_snapshot


def environment(tmp_path):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    cgroup.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 100 kB\n")
    return proc, cgroup


def test_memory_uses_host_available_and_visible_ancestor_headroom(tmp_path):
    proc, cgroup = environment(tmp_path)
    leaf = cgroup / "parent/worker"
    leaf.mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/parent/worker\n")
    for directory, limit, current in (
        (cgroup, "max", 800000),
        (leaf.parent, 700000, 650000),
        (leaf, 900000, 100000),
    ):
        (directory / "memory.max").write_text(str(limit))
        (directory / "memory.current").write_text(str(current))
    assert memory_snapshot(proc=proc, cgroup=cgroup) == {
        "scope": "cgroup",
        "source": "cgroup_v2",
        "limit_bytes": 700000,
        "used_bytes": 650000,
        "available_bytes": 50000,
        "cgroup_limit_bytes": 700000,
        "environment_total_bytes": 1024000,
        "limit_source": "cgroup_v2",
    }
    (leaf.parent / "memory.current").write_text("100000")
    assert memory_snapshot(proc=proc, cgroup=cgroup)["available_bytes"] == 102400


@pytest.mark.parametrize("current", ["missing", "invalid", "-1"])
def test_known_limit_with_unknown_usage_does_not_grant_headroom(tmp_path, current):
    proc, cgroup = environment(tmp_path)
    (cgroup / "memory.max").write_text("500000")
    if current != "missing":
        (cgroup / "memory.current").write_text(current)
    result = memory_snapshot(proc=proc, cgroup=cgroup)
    assert result["limit_bytes"] == 500000
    assert result["used_bytes"] is None
    assert result["available_bytes"] is None


def test_missing_linux_memory_is_explicitly_unknown(tmp_path):
    result = memory_snapshot(proc=tmp_path, cgroup=tmp_path)
    assert result == dict(
        scope="unavailable",
        source="unavailable",
        limit_bytes=None,
        used_bytes=None,
        available_bytes=None,
        cgroup_limit_bytes=None,
        environment_total_bytes=None,
        limit_source="unavailable",
    )


def test_fractional_cpu_quota_affinity_and_ancestor_limits(tmp_path, monkeypatch):
    proc, cgroup = environment(tmp_path)
    leaf = cgroup / "worker"
    leaf.mkdir()
    (proc / "self/cgroup").write_text("0::/worker")
    monkeypatch.setattr("tgdrive.core.system_resources.os.cpu_count", lambda: 16)
    monkeypatch.setattr(
        "tgdrive.core.system_resources.os.sched_getaffinity", lambda _: {0, 1}
    )
    (leaf / "cpu.max").write_text("250000 100000")
    (cgroup / "cpu.max").write_text("50000 100000")
    assert cpu_snapshot(proc=proc, cgroup=cgroup) == {
        "scope": "cgroup",
        "source": "cgroup_v2",
        "capacity_cores": 0.5,
    }
    (cgroup / "cpu.max").write_text("max 100000")
    assert cpu_snapshot(proc=proc, cgroup=cgroup) == {
        "scope": "affinity",
        "source": "affinity",
        "capacity_cores": 2,
    }


@pytest.mark.parametrize("quota", ["bad", "1 0", "-1 100000", "max 100000"])
def test_invalid_cpu_quota_uses_measured_affinity(tmp_path, monkeypatch, quota):
    proc, cgroup = environment(tmp_path)
    (cgroup / "cpu.max").write_text(quota)
    monkeypatch.setattr("tgdrive.core.system_resources.os.cpu_count", lambda: 16)
    monkeypatch.setattr(
        "tgdrive.core.system_resources.os.sched_getaffinity", lambda _: {1}
    )
    assert cpu_snapshot(proc=proc, cgroup=cgroup)["capacity_cores"] == 1


def test_cgroup_membership_cannot_escape_visible_mount(tmp_path):
    proc, cgroup = environment(tmp_path)
    (proc / "self/cgroup").write_text("0::/../outside")
    assert memory_snapshot(proc=proc, cgroup=cgroup)["scope"] == "host"


@pytest.mark.parametrize("hard_limit", ["max", "2097152"])
def test_environment_budget_is_not_a_cgroup_hard_limit(tmp_path, hard_limit):
    proc, cgroup = environment(tmp_path)
    (cgroup / "memory.max").write_text(hard_limit)
    (cgroup / "memory.current").write_text("100000")
    value = memory_snapshot(proc=proc, cgroup=cgroup)
    assert value["scope"] == "cgroup"
    assert value["source"] == "cgroup_v2"
    assert value["limit_bytes"] == value["environment_total_bytes"] == 1024000
    assert value["cgroup_limit_bytes"] == (None if hard_limit == "max" else 2097152)
    assert value["used_bytes"] == 100000
    assert value["limit_source"] == "proc_meminfo"


def test_legacy_memory_accounts_for_visible_ancestor_usage(tmp_path):
    proc, cgroup = environment(tmp_path)
    root = cgroup / "memory"
    leaf = root / "parent/worker"
    leaf.mkdir(parents=True)
    (proc / "self/cgroup").write_text("5:memory:/parent/worker\n")
    for directory, limit, current in (
        (root, 9223372036854771712, 900000),
        (leaf.parent, 700000, 650000),
        (leaf, 900000, 100000),
    ):
        (directory / "memory.limit_in_bytes").write_text(str(limit))
        (directory / "memory.usage_in_bytes").write_text(str(current))
    value = memory_snapshot(proc=proc, cgroup=cgroup)
    assert value["source"] == value["limit_source"] == "cgroup_v1"
    assert value["limit_bytes"] == value["cgroup_limit_bytes"] == 700000
    assert value["used_bytes"] == 650000
    assert value["available_bytes"] == 50000


@pytest.mark.parametrize(
    "state", ["unresolved", "invalid_limit", "missing_usage", "escape", "missing_leaf"]
)
def test_legacy_unknown_measurements_never_grant_host_headroom(tmp_path, state):
    proc, cgroup = environment(tmp_path)
    (proc / "self/cgroup").write_text(
        "5:memory:/../outside"
        if state == "escape"
        else "5:memory:/"
        if state in {"invalid_limit", "missing_usage"}
        else "5:memory:/worker"
    )
    if state != "unresolved":
        root = cgroup / "memory"
        root.mkdir()
        (root / "memory.limit_in_bytes").write_text(
            "invalid" if state == "invalid_limit" else "500000"
        )
        if state != "missing_usage":
            (root / "memory.usage_in_bytes").write_text("100000")
    value = memory_snapshot(proc=proc, cgroup=cgroup)
    assert value["source"] == "cgroup_v1"
    assert value["environment_total_bytes"] == 1024000
    assert value["available_bytes"] is None
    if state != "missing_usage":
        assert value["limit_bytes"] is None


def test_legacy_unlimited_sentinel_does_not_become_an_enormous_ceiling(tmp_path):
    proc, cgroup = environment(tmp_path)
    root = cgroup / "memory"
    root.mkdir()
    (proc / "self/cgroup").write_text("5:memory:/")
    (root / "memory.limit_in_bytes").write_text("9223372036854771712")
    (root / "memory.usage_in_bytes").write_text("100000")
    value = memory_snapshot(proc=proc, cgroup=cgroup)
    assert value["cgroup_limit_bytes"] is None
    assert value["limit_bytes"] == 1024000
    assert value["limit_source"] == "proc_meminfo"
    assert value["available_bytes"] == 102400


def test_legacy_fractional_cpu_quotas_and_combined_controller_mount(
    tmp_path, monkeypatch
):
    proc, cgroup = environment(tmp_path)
    root = cgroup / "cpu,cpuacct"
    leaf = root / "worker"
    leaf.mkdir(parents=True)
    (proc / "self/cgroup").write_text("4:cpuacct,cpu:/worker")
    monkeypatch.setattr("tgdrive.core.system_resources.os.cpu_count", lambda: 8)
    monkeypatch.setattr(
        "tgdrive.core.system_resources.os.sched_getaffinity", lambda _: {0, 1}
    )
    for directory, quota in ((root, "50000"), (leaf, "250000")):
        (directory / "cpu.cfs_quota_us").write_text(quota)
        (directory / "cpu.cfs_period_us").write_text("100000")
    assert cpu_snapshot(proc=proc, cgroup=cgroup) == {
        "scope": "cgroup",
        "source": "cgroup_v1",
        "capacity_cores": 0.5,
    }


def test_detected_unresolved_legacy_cpu_capacity_is_unknown(tmp_path):
    proc, cgroup = environment(tmp_path)
    (proc / "self/cgroup").write_text("4:cpu,cpuacct:/worker")
    assert cpu_snapshot(proc=proc, cgroup=cgroup)["capacity_cores"] is None


def test_governor_missing_linux_metrics_retains_unknown_memory(tmp_path):
    from tgdrive.core.governor import memory

    assert memory(cgroup=str(tmp_path), meminfo=str(tmp_path / "missing")) == (
        None,
        None,
    )
