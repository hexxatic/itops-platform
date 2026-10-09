"""Which release is running (shown in /health and the page footer).

A release bundle (tools/deploy/Build-ITOpsRelease.ps1) carries ``release.json`` next to
its portable Python: ``<release>/release.json`` and ``<release>/python/python.exe``. A
development checkout has no such file and reports ``dev``.
"""

from __future__ import annotations

import json
import sys
from functools import cache
from pathlib import Path
from typing import Any

from itops import __version__


def read_release(path: Path) -> dict[str, Any]:
    """The release info at ``path``, or the dev fallback if it's missing or unreadable."""
    try:
        data: dict[str, Any] = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return {"version": "dev", "core": __version__}
    return {
        "version": str(data.get("version") or "unknown"),
        "commit": data.get("commit"),
        "built": data.get("built"),
        "core": __version__,
    }


@cache
def release_info() -> dict[str, Any]:
    return read_release(Path(sys.base_prefix).parent / "release.json")


def release_version() -> str:
    return str(release_info()["version"])
