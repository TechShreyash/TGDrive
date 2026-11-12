"""Owner retention policy regressions; execution awaits separate owner approval."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from tests.observability.test_performance_preferences import (
    mongo_backend as mongo_backend,
)
from tests.observability.test_performance_preferences import runtime
from tests.support.app_fixtures import make_settings
from tgdrive.replication.replica_settings import _performance_defaults
from tgdrive.settings.performance_preferences import (
    PerformancePreferences,
    PerformanceSettings,
)


@pytest.mark.asyncio
async def test_retention_is_persisted_live_applied_and_changes_etag(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    before = await service.snapshot()
    capacity = (
        settings.cache_disk_bytes,
        settings.cache_memory_bytes,
        settings.cache_disk_limit_bytes,
    )
    state.preloads.reconfigure = AsyncMock(wraps=state.preloads.reconfigure)
    try:
        after = await service.save(
            {"cache_ttl_seconds": 3600}, expected=before["revision"]
        )
        assert before["defaults"]["cache_ttl_seconds"] == 86400
        assert after["cache_ttl_seconds"] == 3600
        assert after["revision"] != before["revision"]
        assert settings.cache_ttl_seconds == 3600
        assert (
            settings.cache_disk_bytes,
            settings.cache_memory_bytes,
            settings.cache_disk_limit_bytes,
        ) == capacity
        state.preloads.reconfigure.assert_awaited_once()
        assert state.preloads.cache.disk.ttl == 3600
        saved = mongo_backend.db.settings.find_one({"_id": "performance-preferences"})[
            "document"
        ]
        assert saved["cache_ttl_seconds"] == 3600
        reloaded = PerformanceSettings(settings, state)
        assert (await reloaded.snapshot())["cache_ttl_seconds"] == 3600
    finally:
        await state.preloads.close()


@pytest.mark.parametrize(
    "value", [None, 0, 59, True, "3600", 60.0, float("inf"), 2**53]
)
def test_invalid_retention_values_are_rejected(value):
    with pytest.raises(ValidationError):
        PerformancePreferences(cache_ttl_seconds=value)


def test_missing_legacy_retention_normalizes_to_its_original_default(tmp_path):
    settings = make_settings(tmp_path)
    settings.cache_ttl_seconds = 700
    defaults = _performance_defaults(settings)
    assert defaults["cache_ttl_seconds"] == 86400
    assert (
        PerformancePreferences.model_validate(
            defaults | {"cache_ttl_seconds": 3600}
        ).cache_ttl_seconds
        == 3600
    )
