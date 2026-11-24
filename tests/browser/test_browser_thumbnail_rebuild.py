"""Local production-bundle UI with synthetic rebuild progress and generated manga."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e
