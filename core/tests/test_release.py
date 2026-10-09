"""Which release is running: read from release.json next to the bundled Python."""

from __future__ import annotations

import json
from pathlib import Path

from itops import __version__
from itops.core.release import read_release


def test_a_release_bundle_reports_its_version(tmp_path: Path) -> None:
    f = tmp_path / "release.json"
    f.write_text(
        json.dumps({"version": "v0.2.1", "commit": "abc1234", "built": "2026-10-06T12:00Z"})
    )
    info = read_release(f)
    assert info == {"version": "v0.2.1", "commit": "abc1234", "built": "2026-10-06T12:00Z",
                    "core": __version__}  # fmt: skip


def test_no_or_broken_release_file_means_dev(tmp_path: Path) -> None:
    assert read_release(tmp_path / "missing.json")["version"] == "dev"
    broken = tmp_path / "release.json"
    broken.write_text("{not json")
    assert read_release(broken)["version"] == "dev"
