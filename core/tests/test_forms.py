"""Form input kept across the redirect after a failed save."""

from __future__ import annotations

import pytest

from itops.core import forms
from itops.core.forms import FormKeeper, KeptForm


def test_kept_forms_belong_to_their_user_and_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    keeper = FormKeeper()
    form = KeptForm({"name": ["DB-SRV01"], "f__kind": ["Server"], "f__on": ["a", "b", ""]})
    token = keeper.keep("pat", form)
    assert token
    kept = keeper.recall("pat", token)
    assert kept is not None and kept.get("name") == "DB-SRV01" and "name" in kept
    assert kept.fields() == {"kind": "Server", "on": ["a", "b"]}
    assert keeper.recall("pat", token) is kept  # a reload still shows it
    assert keeper.recall("someone-else", token) is None
    assert keeper.recall("pat", "not-a-token") is None
    assert keeper.recall("pat", None) is None

    clock = [1000.0]
    monkeypatch.setattr(forms.time, "monotonic", lambda: clock[0])
    token = keeper.keep("pat", form)
    clock[0] += forms.TTL_SECONDS + 1
    assert keeper.recall("pat", token) is None


def test_oversized_and_overflowing_forms() -> None:
    keeper = FormKeeper()
    assert keeper.keep("pat", KeptForm({"content": ["x" * (forms.MAX_CHARS + 1)]})) is None
    first = keeper.keep("pat", KeptForm({"n": ["0"]}))
    for i in range(forms.MAX_FORMS):
        keeper.keep("pat", KeptForm({"n": [str(i)]}))
    assert keeper.recall("pat", first) is None  # the oldest makes room
