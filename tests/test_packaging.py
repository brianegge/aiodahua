"""Packaging invariants.

A PyPI version can never be reused, so a mismatch between the packaged version
and the one the library reports is the kind of mistake that is expensive to
undo. Cheaper to fail here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import aiodahua

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["project"]


class TestVersion:
    def test_matches_pyproject(self):
        assert aiodahua.__version__ == _project()["version"]

    def test_is_a_release_number(self):
        parts = aiodahua.__version__.split(".")
        assert len(parts) == 3, aiodahua.__version__
        assert all(p.isdigit() for p in parts), aiodahua.__version__


class TestPublicApi:
    def test_all_names_are_importable(self):
        """__all__ entries that do not resolve break `from aiodahua import X`
        for consumers without failing anything here otherwise."""
        missing = [n for n in aiodahua.__all__ if not hasattr(aiodahua, n)]
        assert missing == [], missing

    def test_client_and_brand_entry_points_present(self):
        for name in ("DahuaClient", "identify_brand", "DahuaUnsafeOperationError"):
            assert name in aiodahua.__all__, name
