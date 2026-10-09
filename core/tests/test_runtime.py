"""Event bus, service registry access rules, and SDK runtime guards."""

from __future__ import annotations

import pytest
from _helpers import MakeModule

from itops.core.ai import AIService
from itops.core.bus import Event, EventBus
from itops.core.config import Settings
from itops.core.files import FileStore
from itops.core.registry import ServiceRegistry
from itops.core.runtime import Runtime
from itops.sdk import ModuleNotLoaded


def runtime() -> Runtime:
    s = Settings()
    return Runtime(
        settings=s,
        bus=EventBus(),
        registry=ServiceRegistry(),
        ai=AIService(s),
        files=FileStore(s.files_dir, max_bytes=1024),
    )


async def test_bus_delivers_and_isolates_failures() -> None:
    bus = EventBus()
    got: list[str] = []

    async def good(e: Event) -> None:
        got.append(e.payload["x"])

    async def bad(e: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe("a.happened", bad, subscriber="b")
    bus.subscribe("a.happened", good, subscriber="c")
    bus.publish(Event("a.happened", {"x": "1"}, source="a"))
    await bus.drain()
    assert got == ["1"]


async def test_publish_requires_declared_event(make_module: MakeModule) -> None:
    m = make_module("alpha", manifest_extra='[events]\nthing_done = "A thing was done"\n')
    rt = runtime()
    m.bind(rt)
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    rt.bus.subscribe("alpha.thing_done", handler, subscriber="test")
    m.publish("thing_done", {"id": 1})
    await rt.bus.drain()
    assert received[0].name == "alpha.thing_done"
    with pytest.raises(ValueError, match="not declared"):
        m.publish("undeclared", {})


def test_runtime_facilities_need_loading(make_module: MakeModule) -> None:
    m = make_module("alpha", manifest_extra='[events]\nx = "x"\n')
    with pytest.raises(ModuleNotLoaded):
        m.publish("x", {})


def test_services_limited_to_declared_dependencies(make_module: MakeModule) -> None:
    m = make_module("alpha", manifest_extra='optional = ["beta"]\n')
    rt = runtime()
    m.bind(rt)
    assert m.services.get("beta.search") is None  # declared but absent -> degrade
    rt.registry.register("beta.search", lambda: "hit")
    fn = m.services.get("beta.search")
    assert fn is not None and fn() == "hit"
    with pytest.raises(ValueError, match="not in this module's manifest"):
        m.services.get("gamma.search")


def test_llm_is_off_by_default_embeddings_are_on(make_module: MakeModule) -> None:
    m = make_module("alpha")
    m.bind(runtime())
    assert m.ai.llm("triage") is None  # tier 3: opt-in per feature
    assert m.ai.embeddings_available  # tier 2: local model, on by default


def test_ai_principal_never_holds_human_only_permissions(make_module: MakeModule) -> None:
    from itops.core import auth

    m = make_module(
        "alpha",
        manifest_extra=(
            '[permissions]\n"alpha.write" = "Draft"\n'
            '"alpha.publish" = { description = "Publish", human_only = true }\n'
        ),
    )
    assert m.manifest.human_only_permissions == {"alpha.publish"}
    auth.configure(Settings())
    auth.set_human_only(set(m.manifest.human_only_permissions))
    assert auth.require("alpha.publish").user == "dev"  # a person in the web UI: fine
    with auth.acting_via_ai():
        assert auth.require("alpha.write").via_ai  # AI can draft
        with pytest.raises(auth.PermissionDenied, match="human-only"):
            auth.require("alpha.publish")
    auth.set_human_only(set())


def test_tool_needing_human_only_permission_is_refused(make_module: MakeModule) -> None:
    from itops.core.loader import validate

    body = (
        '@module.tool(permission="alpha.publish")\n'
        'async def go() -> str:\n    """Go."""\n    return "x"\n'
    )
    perms = '[permissions]\n"alpha.publish" = { description = "P", human_only = true }\n'
    m = make_module("alpha", manifest_extra=perms, body=body)
    assert any("human-only" in p for p in validate(m))
