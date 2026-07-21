from __future__ import annotations

import pytest

from .release_support import ReleaseFixture, build_release_fixture


@pytest.fixture
def release_fixture(tmp_path) -> ReleaseFixture:
    return build_release_fixture(tmp_path)
