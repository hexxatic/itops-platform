"""AI suggestions from a file (ADR 0015): only values backed by a quote found in the file
survive, and runs execute in the background and are recorded."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from _helpers import MakeModule, perms

from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.suggest import SuggestField, checked, dates_in, extract_suggestions

FIELDS = [
    SuggestField("ends_on", "End date", "date"),
    SuggestField("starts_on", "Start date", "date"),
    SuggestField("cost", "Cost", "number"),
    SuggestField("notice_days", "Notice period in days", "number", prefill=False),
    SuggestField("auto_renews", "Renews automatically", "bool"),
]
PAGES = [
    "Schedule of Products\nSophos MDR Complete 525 users",
    "Start Date 04/22/2026 Expiry Date May 16, 2027\nTotal $14,250.00\n"
    "Either party may cancel with thirty (30) days written notice.",
]


class FakeLLM:
    name = "fake-7b"

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(
        self, prompt: str, *, system: str | None = None, max_tokens: int = 1024,
        json: bool = False,
    ) -> str:  # fmt: skip
        self.prompts.append(prompt)
        return "Here you go: " + __import__("json").dumps(self.answer)


def test_dates_are_found_in_any_common_format() -> None:
    assert dates_in("from 04/22/2026 to May 16, 2027, renewed 2027-05-17 or 1st June 2027") == {
        dt.date(2026, 4, 22), dt.date(2027, 5, 16), dt.date(2027, 5, 17), dt.date(2027, 6, 1),
    }  # fmt: skip
    date = SuggestField("d", "D", "date")
    assert checked(date, "2027-05-16", "Expiry Date 05/16/2027") == "2027-05-16"
    assert checked(date, "2026-04-01", "Expiry Date 05/16/2027") is None  # not in its quote
    money = SuggestField("c", "C", "number")
    assert checked(money, "$14,250", "Total $14,250.00") == 14250
    assert checked(money, 30, "thirty (30) days") == 30


async def test_only_quoted_values_survive_and_get_their_page() -> None:
    llm = FakeLLM({
        "ends_on": {"value": "2027-05-16", "quote": "Expiry Date May 16, 2027"},
        "starts_on": {"value": "2026-04-01", "quote": "Start Date 04/22/2026"},  # wrong value
        "cost": {"value": 14250, "quote": "Total $14,250.00"},
        "notice_days": {"value": 30, "quote": "cancel with thirty (30) days written notice"},
        "auto_renews": {"value": True, "quote": "renews automatically every year"},  # not there
    })  # fmt: skip
    found, dropped = await extract_suggestions(llm, PAGES, FIELDS)
    assert {s.key: (s.value, s.page) for s in found} == {
        "ends_on": ("2027-05-16", 2), "cost": (14250, 2), "notice_days": (30, 2),
    }  # fmt: skip
    assert dropped == 2
    assert "[Page 2]" in llm.prompts[0] and '"notice_days"' in llm.prompts[0]


async def test_runs_happen_in_the_background_and_are_recorded(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("suggmod", manifest_extra=perms("suggmod"))
    settings = Settings(database_url_override=database_url, files_dir=str(tmp_path),
                        ai_llm_features=["suggmod.read_files"])  # fmt: skip
    app = create_app(settings, modules=[mod])
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        llm = FakeLLM({"cost": {"value": "14,250", "quote": "Total $14,250.00"}})
        runtime.ai._llm = llm  # pyright: ignore[reportPrivateUsage]
        stored = await mod.files.save("\n".join(PAGES).encode(), mime_type="text/plain",
                                      filename="schedule.txt", attachment=True)  # fmt: skip
        assert mod.ai.can_suggest("read_files") and not mod.ai.can_suggest("other")
        run_id = await mod.ai.suggest("read_files", stored.id, FIELDS, entity=("thing", "7"))
        await runtime.suggestions.wait()
        run = await mod.ai.suggestion_run(run_id)
        assert run is not None and run.status == "done" and run.model == "fake-7b"
        assert [(s.key, s.value, s.page) for s in run.suggestions] == [("cost", 14250, 1)]
        await mod.ai.suggestions_reviewed(run_id, {"cost": 14000})
        runs = await mod.ai.suggestion_runs("thing", "7")
        assert runs[0].id == run_id and runs[0].status == "reviewed"
        assert json.loads(json.dumps(runs[0].fields[3].__dict__))["prefill"] is False
