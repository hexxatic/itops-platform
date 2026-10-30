"""Assets pages (server-rendered, core components only) and the form actions behind them.

Pages live at /assets/...; actions are POSTs under /api/assets/... that redirect back to
a page (plain forms, no JavaScript needed). Problems come back as ``?error=...``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from itops.sdk import Principal
from itops.sdk.export import Column, ExportProblem, export_response
from itops.sdk.types import TypeDef
from itops_assets import classes as cl
from itops_assets import devices as dv
from itops_assets import evidence as ev
from itops_assets import readiness as rd
from itops_assets import service
from itops_assets.app import module
from itops_assets.models import OsTarget, Proposal, Record
from itops_assets.service import AssetsError, known_fields, proposal_fields, record_fields

CanWrite = Annotated[Principal, Depends(module.require("assets.write"))]
CanPublish = Annotated[Principal, Depends(module.require("assets.publish"))]
CanRead = Annotated[Principal, Depends(module.require("assets.read"))]
CanTarget = Annotated[Principal, Depends(module.require("assets.targets"))]
CanClass = Annotated[Principal, Depends(module.require("assets.classes"))]
Text = Annotated[str, Form()]

LIST_COLUMNS = 4  # fields shown in a type's list, after the name
LIST_KINDS = ("text", "select", "date", "number", "url", "link")


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _noun(type_name: str) -> str:
    """A type name mid-sentence: 'Device' -> 'device', but 'SSL certificate' stays."""
    return type_name if type_name[1:2].isupper() else type_name[:1].lower() + type_name[1:]


def _back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


async def _record_or_404(s: Any, record_id: str) -> Record:
    record = await service.get(s, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return record


async def _type_or_404(type_key: str) -> tuple[str, TypeDef]:
    types = await service.all_types()
    view = types.get(type_key)
    if view is None or view.published is None:
        raise HTTPException(status_code=404, detail="Asset type not found")
    return view.published.definition.name, view.published.definition


async def _form_fields(request: Request) -> dict[str, Any]:
    """Field inputs (named f__<key>); repeated names = several values."""
    form = await request.form()
    out: dict[str, Any] = {}
    for key in {k for k in form if k.startswith("f__")}:
        values = [str(v) for v in form.getlist(key) if str(v).strip()]
        out[key[3:]] = values if len(values) > 1 else (values[0] if values else "")
    return out


def _cell(field_kind: str, value: Any, links: dict[str, Any]) -> Any:
    if value is None or value == "" or value == []:
        return ""
    items: list[Any] = value if isinstance(value, list) else [value]  # pyright: ignore[reportUnknownVariableType]
    if field_kind == "link":
        keys = [service.value_text(i) for i in items]
        return ", ".join(links.get(k, {}).get("text", "(deleted)") for k in keys)
    return ", ".join(service.value_text(i) for i in items)


# ---- pages --------------------------------------------------------------------------------


@module.page("/", template="index.html", title="Assets", nav="Assets", permission="assets.read")
async def home(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        counts = await service.counts_by_type(s)
        pending = len(await service.pending_proposals(s)) if _can("assets.publish") else 0
        unreviewed = await service.unreviewed_devices(s)
    # Each review queue is listed even when it's empty, so people learn where it is.
    review = [
        {"what": {"text": "Proposed changes", "href": "/assets/proposals"}, "count": pending,
         "about": "Records and changes AI proposed, for you to approve or reject."}
    ] if _can("assets.publish") else []  # fmt: skip
    review += [
        {"what": {"text": "Unreviewed devices", "href": "/assets/t/device?view=unreviewed"},
         "count": unreviewed,
         "about": "Found by a sync; set what each one is (its status and kind)."},
        {"what": {"text": "Sources disagree", "href": "/assets/disagreements"},
         "count": len(await service.disagreements()),
         "about": "Where a scan, AD and the status don't line up, with what to do."},
    ]  # fmt: skip
    return {
        "rows": [
            {
                "type": {"text": t.current.definition.name, "href": f"/assets/t/{t.key}"},
                "count": counts.get(t.key, 0),
                "about": t.current.definition.description,
            }
            for t in await service.available_types()
        ],
        "review": review,
        "can_manage_types": _can("assets.types.draft"),
        "has_targets": bool(await service.os_target_list()) or _can("assets.targets"),
        "error": request.query_params.get("error"),
    }


@module.page("/t/{type_key}", template="type_list.html", title="Assets", permission="assets.read")
async def type_list(request: Request) -> dict[str, Any]:
    type_key = request.path_params["type_key"]
    type_name, definition = await _type_or_404(type_key)
    if type_key == dv.TYPE:
        return await _device_list(request, type_name, definition)
    query = request.query_params.get("q", "")
    async with module.session() as s:
        found = await service.records(s, type_key=type_key, query=query, limit=500)
    # A field called "name" (imported types have them) would just repeat the record's name.
    fields = [f for f in definition.fields if f.kind in LIST_KINDS and f.key != "name"]
    columns = fields[:LIST_COLUMNS]
    links = await module.links.resolve(*(record_fields(r) for r in found))
    # Field cells are keyed "f:<key>", so no field can overwrite the record-name link.
    rows = [
        {
            "name": {"text": r.name, "href": f"/assets/r/{r.id}"},
            **{f"f:{f.key}": _cell(f.kind, record_fields(r).get(f.key), links) for f in columns},
        }
        for r in found
    ]
    return {
        "page_title": type_name,
        "access": await module.areas.access("type", type_key),
        "type_key": type_key,
        "type_name": type_name,
        "type_noun": _noun(type_name),
        "description": definition.description,
        "columns": [("name", "Name")] + [(f"f:{f.key}", f.label) for f in columns],
        "rows": rows,
        "query": query,
        "can_write": _can("assets.write"),
    }


async def _device_view(
    record: Record, definition: TypeDef, values: dict[str, Any], sources: list[Any]
) -> dict[str, Any]:
    """The device page: a summary of what's worked out, then the fields in sections, each
    saying which source fills it."""
    today = dt.date.today()
    raw = record_fields(record)
    cls_, basis = cl.classify(raw, await service.class_rules())
    judged = ev.judge(raw, today)
    acquired, age_basis = service.acquired_of(raw, today)
    item = {
        "acquired": acquired,
        "age_basis": age_basis,
        "seen_by": judged.seen_by,
        "seen_on": judged.seen_on or None,
        "seen_via": judged.seen_via,
        "last_seen": raw.get("last_seen"),
        "ad_last_sign_in": raw.get("ad_last_sign_in"),
    }
    by_key = {f.key: f for f in definition.fields}
    placed: set[str] = set()
    sections: list[dict[str, Any]] = []
    for title, keys in [*dv.SECTIONS, ("Other", tuple(by_key))]:
        fields = [by_key[k] for k in keys if k in by_key and k not in placed]
        placed.update(f.key for f in fields)
        keys_here = {f.key for f in fields}
        if not any(f.key in values for f in fields):
            continue
        from_ = [
            f"{src.system} (last seen {_when(src.last_seen)})"
            for src in sources
            if "*" in src.owned or keys_here & set(src.owned)
        ]
        sections.append({"title": title, "definition": {"fields": fields},
                         "sources": ", ".join(from_)})  # fmt: skip
    return {
        "summary_rows": [
            {
                "what": "Class",
                "value": (cls_ or "No class")
                + {"set": " (set by a person)", "model": " (by model)"}.get(basis, ""),
            },
            {"what": "Status", "value": raw.get("status") or "No status"},
            {"what": "In use", "value": dv.seen_text(item)},
            {"what": "Age", "value": dv.age_text(item, today)},
        ],
        "sections": sections,
    }


async def _matching_devices(
    params: Any,
) -> tuple[dv.Filters, list[dict[str, Any]], list[dict[str, Any]]]:
    """The filters, the devices they match (sorted), and every device."""
    f = dv.Filters.from_query(params)
    every = await service.fleet(include_ignored=True, with_sources=True)
    found = [d for d in every if dv.matches(d, f)]
    return f, dv.sort(found, f.sort, dt.date.today()), every


async def _device_list(request: Request, type_name: str, definition: TypeDef) -> dict[str, Any]:
    f, found, every = await _matching_devices(request.query_params)
    today = dt.date.today()
    pages = max(1, -(-len(found) // dv.PAGE_SIZE))
    page = min(f.page, pages)
    shown = found[(page - 1) * dv.PAGE_SIZE : page * dv.PAGE_SIZE]
    kinds: list[str] = []
    for fd in definition.fields:
        if fd.key == "kind":
            kinds = [str(o) for o in fd.options or []]

    def link(**changes: Any) -> str:
        return "/assets/t/device?" + urlencode(f.query(**changes))

    systems = sorted({x for d in every for x in dv.sources_of(d)})

    return {
        "page_title": type_name,
        "device": True,
        "access": await module.areas.access("type", dv.TYPE),
        "type_key": dv.TYPE,
        "type_name": type_name,
        "type_noun": _noun(type_name),
        "description": definition.description,
        "f": f,
        "views": [
            {"label": label, "href": "/assets/t/device?view=" + key}
            for key, (label, _) in dv.VIEWS.items()
        ],
        "options": {
            "status": [
                ("", "Any status"),
                ("not_retired", "Not retired"),
                *[(x, x) for x in dv.STATUSES],
                ("none", "No status"),
            ],
            "cls": [("", "Any class"), *[(x, x) for x in cl.CLASSES], ("none", "No class")],
            "kind": [("", "Any kind"), *[(k, k) for k in kinds]],
            "inuse": [
                ("", "In use or not"),
                ("network", "Seen by a scan"),
                ("ad", "AD only"),
                ("none", "Seen by neither"),
            ],
            "source": dv.source_options(systems),
            "model": [("", "Any model"), ("none", "No model")],
            "disagree": [("", "Agree or not"), ("1", "Sources disagree")],
            "sort": list(dv.SORTS.items()),
        },
        "columns": [
            ("name", "Name"),
            ("class", "Class"),
            ("status", "Status"),
            ("seen", "In use"),
            ("age", "Age"),
            ("model", "Model"),
            ("sources", "Known to"),
            ("flag", ""),
        ],
        "rows": [dv.row(d, today) for d in shown],
        "total": len(found),
        "page": page,
        "pages": pages,
        "prev": link(page=page - 1) if page > 1 else None,
        "next": link(page=page + 1) if page < pages else None,
        "download": "/api/assets/devices/export?" + urlencode(f.query()),
        "bulk_query": f.query(),
        "bulk_statuses": [("", "Leave status"), *[(x, x) for x in dv.STATUSES]],
        "bulk_classes": [
            ("", "Leave class"),
            *[(x, x) for x in cl.CLASSES],
            ("by model", "By model (clear it)"),
        ],
        "can_write": _can("assets.write"),
        "error": request.query_params.get("error"),
        "done": request.query_params.get("done"),
    }


@module.router.get("/devices/export")
async def devices_export(request: Request, _: CanRead, format: str = "xlsx") -> Response:
    _f, found, _every = await _matching_devices(request.query_params)
    today = dt.date.today()
    columns = [
        Column("name", "Device"), Column("kind", "Kind"), Column("class", "Class"),
        Column("class_basis", "Class from"), Column("status", "Status"),
        Column("seen_by", "In use"), Column("last_seen", "Last seen (scan)", "date"),
        Column("sophos_last_seen", "Last seen (Sophos)", "date"),
        Column("sophos_health", "Protection (Sophos)"),
        Column("ad_last_sign_in", "Last signed in (AD)", "date"),
        Column("ad_account", "AD account"), Column("age", "Age (years)", "number"),
        Column("age_basis", "Age from"), Column("acquired", "Acquired", "date"),
        Column("manufacturer", "Manufacturer"), Column("model", "Model"),
        Column("serial", "Serial"), Column("operating_system", "Operating system"),
        Column("sources", "Known to"), Column("disagreements", "Sources disagree"),
    ]  # fmt: skip
    try:
        return export_response([dv.export_row(d, today) for d in found], columns,
                               filename="Devices", format=format)  # fmt: skip
    except ExportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@module.router.post("/devices/bulk")
async def devices_bulk(request: Request, who: CanWrite) -> RedirectResponse:
    form = await request.form()
    keys = ("q", "status", "cls", "kind", "inuse", "source", "model", "disagree", "sort")
    params = {k: str(form.get(k) or "") for k in keys}
    back = "/assets/t/device?" + urlencode({k: v for k, v in params.items() if v})
    _f, found, _every = await _matching_devices(params)
    expected = str(form.get("expected") or "")
    if not expected.isdigit() or int(expected) != len(found):
        return _back(back, f"The list changed (now {len(found)} devices): check it and try again.")
    try:
        async with module.session() as s:
            changed = await service.set_devices(
                s, who, [d["id"] for d in found], status=str(form.get("set_status") or ""),
                computer_class=str(form.get("set_class") or ""),
            )  # fmt: skip
            await s.commit()
    except AssetsError as exc:
        return _back(back, str(exc))
    return RedirectResponse(back + ("&" if "?" in back else "?") + f"done={changed}", 303)


@module.page(
    "/disagreements", template="disagreements.html", title="Sources disagree",
    permission="assets.read",
)  # fmt: skip
async def disagreements_page(request: Request) -> dict[str, Any]:
    show_accepted = request.query_params.get("accepted") == "1"
    devices = await service.fleet()
    accepted = [
        {"name": {"text": d["name"], "href": d["url"]}, "why": a["text"],
         "reason": a["reason"], "by": f"{a['by']}, {_when(a['at'])}"}
        for d in devices for a in d["accepted"]
    ]  # fmt: skip
    return {
        "rows": [
            {
                "name": {"text": d["name"], "href": d["url"]},
                "kind": d["kind"] or "",
                "status": d["status"] or "",
                "why": " ".join(d["disagreements"]),
            }
            for d in devices
            if d["disagreements"]
        ],
        "show_accepted": show_accepted,
        "accepted": accepted if show_accepted else [],
        "accepted_count": len(accepted),
        "days": ev.IN_USE_DAYS,
    }


@module.page(
    "/classes", template="classes.html", title="Computer classes", permission="assets.read"
)
async def classes_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        texts = await service.class_texts(s)
    rules = cl.Rules.from_text(texts)
    kept = module.forms.recall(request)
    in_use = [d for d in await service.fleet() if d["kind"] in cl.FORMS
              and d["status"] not in ("Retired", "Ignored")]  # fmt: skip
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in in_use:
        groups.setdefault((str(d["model"] or ""), cl.form_of(d) or ""), []).append(d)
    rows: list[dict[str, Any]] = []
    for (model, form), found in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0])):
        tier = cl.tier_of(model, rules)
        overridden = sum(1 for d in found if d["class_basis"] == "set")
        rows.append({
            "model": model or "(no model)",
            "form": form,
            "count": len(found),
            "cls": f"{tier} {form}" if tier else "No class: add the model to a list",
            "set": overridden or "",
        })  # fmt: skip
    return {
        "rows": rows,
        "unclassified": sum(1 for d in in_use if not d["computer_class"]),
        "v": {
            "engineering": kept.get("engineering") if kept else texts["Engineering"],
            "standard": kept.get("standard") if kept else texts["Standard"],
        },
        "patterns": [{"tier": t, "pattern": p} for t in cl.TIERS for p in rules.patterns[t]],
        "can_edit": _can("assets.classes"),
        "activity": await module.audit.history(kind="computer_classes", entity_id="models"),
        "error": request.query_params.get("error"),
    }


@module.router.post("/classes")
async def save_classes(
    request: Request, who: CanClass, engineering: Text = "", standard: Text = ""
) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.save_class_rules(
                s, who, {"Engineering": engineering, "Standard": standard}
            )
            await s.commit()
    except AssetsError as exc:
        return await module.forms.back(request, "/assets/classes", str(exc))
    return _back("/assets/classes")


@module.page("/r/{record_id}", template="record.html", title="Asset", permission="assets.read")
async def record_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        record = await _record_or_404(s, request.path_params["record_id"])
        candidates = await service.merge_candidates(s, record) if _can("assets.publish") else []
        proposed = sum(1 for p in await service.pending_proposals(s)
                       if record.id in (p.record_id, p.merge_into))  # fmt: skip
    open_findings, accepted = (
        await service.device_findings(record) if record.type_key == dv.TYPE else ([], [])
    )
    types = await service.all_types()
    view = types.get(record.type_key)
    definition = view.published.definition if view and view.published else None
    values = record_fields(record)
    if definition:
        values = known_fields(definition, values)
    link = f"assets:{record.type_key}:{record.id}"
    linking = await module.links.related(link)
    documents = [r for r in linking if r["module"] == "docs"]
    sources = await module.refs.links(record.type_key, str(record.id))
    by = {f"connector:{src.connector}": f"{src.title} (connector)" for src in sources}
    type_names = {k: v.current.definition.name for k, v in types.items()}
    ready = rd.readiness(await service.os_target_list(), record_fields(record))
    device = (
        await _device_view(record, definition, values, sources)
        if definition and (record.type_key == dv.TYPE)
        else None
    )
    return {
        "device": device,
        "page_title": record.name,
        "readiness": [
            {
                "target": r.target,
                "ready": _ready_badge(r.status),
                "why": "; ".join(r.why) or "Meets every requirement checked.",
            }
            for r in ready
        ],
        "record": record,
        "proposed": proposed,
        "findings": open_findings,
        "findings_options": [
            (f.code, f.text[:90] + ("..." if len(f.text) > 90 else "")) for f in open_findings
        ],
        "accepted": [a | {"at": _when(a["at"])} for a in accepted],
        "computer_class": cl.classify(record_fields(record), await service.class_rules()),
        "type_name": definition.name if definition else record.type_key,
        "type_noun": _noun(definition.name if definition else record.type_key),
        "definition": definition,
        "values": values,
        "links": await module.links.resolve(values),
        "sources": [
            {
                "system": src.system,
                "title": src.title,
                "last_seen": _when(src.last_seen),
                "missing_since": _when(src.missing_since),
            }
            for src in sources
        ],
        "documents": [
            {"name": {"text": r["name"], "href": r["url"]}, "field": r["field"]} for r in documents
        ],
        "contracts": [
            {"name": {"text": r["name"], "href": r["url"]}, "field": r["field"]}
            for r in linking
            if r["module"] == "contracts"
        ],
        "linked_from": [
            {
                "name": {"text": r["name"], "href": r["url"]},
                "type": type_names.get(r["kind"], r["kind"]),
                "field": r["field"],
            }
            for r in await service.related(link)
        ],
        "created": _when(record.created_at),
        "created_by": by.get(record.created_by, record.created_by),
        "updated_by": by.get(record.updated_by, record.updated_by),
        "updated": _when(record.updated_at),
        "activity": await module.audit.history(kind=record.type_key, entity_id=str(record.id)),
        "can_write": _can("assets.write"),
        "can_delete": _can("assets.publish"),
        "merge_choices": [(str(r.id), await _merge_label(r)) for r in candidates],
        "error": request.query_params.get("error"),
    }


@module.page(
    "/t/{type_key}/new", template="record_edit.html", title="New asset", permission="assets.write"
)
async def new_record(request: Request) -> dict[str, Any]:
    type_key = request.path_params["type_key"]
    type_name, definition = await _type_or_404(type_key)
    kept = module.forms.recall(request)
    return {
        "page_title": f"New {_noun(type_name)}",
        "type_noun": _noun(type_name),
        "locked": [],
        "name_locked": False,
        "locked_note": "",
        "record": None,
        "name": kept.get("name") if kept else "",
        "type_key": type_key,
        "type_name": type_name,
        "definition": definition,
        "values": kept.fields() if kept else {},
        "link_options": await module.links.options(definition),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/r/{record_id}/edit",
    template="record_edit.html",
    title="Edit asset",
    permission="assets.write",
)
async def edit_record(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        record = await _record_or_404(s, request.path_params["record_id"])
        filled = await service.filled_fields(s, record.id)
    type_name, definition = await _type_or_404(record.type_key)
    kept = module.forms.recall(request)
    owned = await service.owned(record)
    sources = sorted(
        {src.system for src in await module.refs.links(record.type_key, str(record.id))}
    )
    return {
        "page_title": f"Edit {record.name}",
        "record": record,
        # Owned fields the source left empty can be filled in, and changed again until it
        # sends a value (service._keep).
        "locked": sorted(
            k for k in owned if (k in record_fields(record) and k not in filled) or k == "*"
        ),
        "name_locked": "name" in owned or "*" in owned,
        "locked_note": (
            f"Synced from {', '.join(sources)}: change it there. Fields it left empty can be "
            "filled in here until it sends a value."
            if sources
            else ""
        ),
        "name": kept.get("name") if kept else record.name,
        "type_key": record.type_key,
        "type_name": type_name,
        "definition": definition,
        "values": kept.fields() if kept else known_fields(definition, record_fields(record)),
        # Keep links the picker wouldn't offer (restricted ones) so a save keeps them.
        "link_options": await module.links.options(definition, keep=record_fields(record)),
        "error": request.query_params.get("error"),
    }


def _ready_badge(status: str) -> dict[str, str]:
    return {"badge": rd.STATUS_LABELS[status], "tone": rd.STATUS_TONES[status]}


def _gb(value: Any) -> str:
    return f"{float(value):g} GB" if value not in (None, "") else ""


@module.page(
    "/readiness", template="readiness.html", title="OS readiness", permission="assets.read"
)
async def readiness_page(request: Request) -> dict[str, Any]:
    report = await service.readiness_report()
    async with module.session() as s:
        saved = {t.id: t for t in await service.os_targets(s)}
    # ?target=<id>&show=<status> narrows one target's list to one status.
    show = request.query_params.get("show", "")
    show_in = request.query_params.get("target", "") if show in rd.STATUS_LABELS else ""
    order = {rd.NOT_READY: 0, rd.UNKNOWN: 1, rd.READY: 2}
    can_target = _can("assets.targets")
    targets: list[dict[str, Any]] = []
    for t in report:
        target = saved[t["id"]]
        anchor = f"#t{t['id']}"
        narrowed = show_in == str(t["id"])
        devices = [d for d in t["devices"] if not narrowed or d["ready"] == show]
        devices.sort(key=lambda d: (order[d["ready"]], str(d["name"]).lower()))
        targets.append(
            {
                "id": t["id"],
                "name": t["name"],
                "about": _target_about(service.target_of(target)),
                "source": target.source,
                "notes": target.notes,
                "tiles": [
                    {
                        "label": "Devices checked",
                        "value": len(t["devices"]),
                        "href": "/assets/readiness" + anchor,
                    },
                    *(
                        {
                            "label": label,
                            "value": t["counts"][key],
                            "href": f"/assets/readiness?target={t['id']}&show={key}{anchor}",
                        }
                        for key, label in rd.STATUS_LABELS.items()
                    ),
                ],
                "showing": rd.STATUS_LABELS[show] if narrowed else "",
                "rows": [
                    {
                        "name": {"text": d["name"], "href": d["url"]},
                        "model": d["model"],
                        "os": d["operating_system"],
                        "processor": d["processor"],
                        "memory": _gb(d["memory_gb"]),
                        "tpm": d["tpm"] or "",
                        "ready": _ready_badge(d["ready"]),
                        "why": "; ".join(d["why"]),
                    }
                    for d in devices
                ],
                "edit": f"/assets/readiness/targets/{t['id']}" if can_target else None,
                "download": f"/api/assets/readiness/{t['id']}",
            }
        )
    presets = [n for n in rd.PRESETS if n not in {t.name for t in saved.values()}]
    return {
        "targets": targets,
        "columns": [
            ("name", "Device"),
            ("model", "Model"),
            ("os", "Runs"),
            ("processor", "Processor"),
            ("memory", "Memory"),
            ("tpm", "TPM"),
            ("ready", "Ready?"),
            ("why", "Why"),
        ],
        "can_target": can_target,
        "presets": presets,
        "error": request.query_params.get("error"),
    }


def _target_about(t: rd.Target) -> str:
    needs: list[str] = []
    if t.processors:
        needs.append("a processor on its list")
    if t.min_memory_gb:
        needs.append(f"{t.min_memory_gb:g} GB memory")
    if t.min_storage_gb:
        needs.append(f"{t.min_storage_gb:g} GB storage")
    if t.min_tpm:
        needs.append(f"TPM {t.min_tpm}")
    return (
        f"Checks devices running {', '.join(t.upgrades_from)}. Needs "
        + (", ".join(needs[:-1]) + " and " + needs[-1] if len(needs) > 1 else needs[0])
        + "."
    )


def _target_form(t: OsTarget | None, kept: Any) -> dict[str, str]:
    def value(key: str, current: Any) -> str:
        if kept is not None:
            return str(kept.get(key) or "")
        return "" if current is None else str(current)

    return {
        "name": value("name", t.name if t else ""),
        "upgrades_from": value(
            "upgrades_from", ", ".join(service.target_of(t).upgrades_from) if t else ""
        ),
        "processors": value("processors", t.processors if t else ""),
        "min_memory_gb": value(
            "min_memory_gb", f"{t.min_memory_gb:g}" if t and t.min_memory_gb else ""
        ),
        "min_storage_gb": value(
            "min_storage_gb", f"{t.min_storage_gb:g}" if t and t.min_storage_gb else ""
        ),
        "min_tpm": value("min_tpm", t.min_tpm if t else ""),
        "source": value("source", t.source if t else ""),
        "notes": value("notes", t.notes if t else ""),
    }


@module.page(
    "/readiness/targets/new", template="target_edit.html", title="New OS target",
    permission="assets.targets",
)  # fmt: skip
async def new_target(request: Request) -> dict[str, Any]:
    return {
        "page_title": "New OS target",
        "target": None,
        "v": _target_form(None, module.forms.recall(request)),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/readiness/targets/{target_id}", template="target_edit.html", title="OS target",
    permission="assets.targets",
)  # fmt: skip
async def edit_target(request: Request) -> dict[str, Any]:
    raw = str(request.path_params["target_id"])
    async with module.session() as s:
        t = await s.get(OsTarget, int(raw)) if raw.isdigit() else None
    if t is None:
        raise HTTPException(status_code=404, detail="OS target not found")
    return {
        "page_title": t.name,
        "target": t,
        "v": _target_form(t, module.forms.recall(request)),
        "activity": await module.audit.history(kind="os_target", entity_id=str(t.id)),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/proposals", template="proposals.html", title="Proposed changes", permission="assets.publish"
)
async def proposals(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        pending = await service.pending_proposals(s)
        current = {p.id: await service.get(s, p.record_id) for p in pending if p.record_id}
        kept = {p.id: await service.get(s, p.merge_into) for p in pending if p.merge_into}
    types = await service.all_types()
    items: list[dict[str, Any]] = []
    for p in pending:
        view = types.get(p.type_key)
        definition = view.published.definition if view and view.published else None
        if p.merge_into:
            items.append(await _merge_view(p, current.get(p.id), kept.get(p.id), definition))
        else:
            items.append(await _proposal_view(p, current.get(p.id), definition))
    return {"items": items, "error": request.query_params.get("error")}


def _differs(record: Record | None, before: Any, after: Any) -> bool:
    # A change shows what differs; a new asset shows what it has.
    return before != after if record else after not in (None, "", [])


async def _merge_view(
    p: Proposal, record: Record | None, kept: Record | None, definition: TypeDef | None
) -> dict[str, Any]:
    """A proposed merge: what the kept record would gain, or why it can't happen now."""
    rows: list[dict[str, Any]] = []
    problem = None
    if record is None or kept is None:
        problem = "One of the records no longer exists: reject it."
    else:
        try:
            fill = await service.merge_plan(record, kept)
        except AssetsError as exc:
            problem = str(exc)
        else:
            values = record_fields(record)
            links = await module.links.resolve(values)
            fields = {f.key: (f.label, f.kind) for f in definition.fields} if definition else {}
            rows = [
                {"field": fields.get(k, (k, ""))[0],
                 "after": _cell(fields.get(k, (k, "text"))[1], values.get(k), links)}
                for k in fill
            ]  # fmt: skip
    return {
        "id": p.id,
        "title": f"Merge {p.name} into {kept.name if kept else '(deleted)'}",
        "href": f"/assets/r/{record.id}" if record else None,
        "kept_href": f"/assets/r/{kept.id}" if kept else None,
        "by": p.proposed_by + (" (via AI)" if p.proposed_via_ai else ""),
        "when": _when(p.created_at),
        "note": p.note,
        "is_merge": True,
        "merged_name": p.name,
        "problem": problem,
        "rows": rows,
    }


async def _proposal_view(
    p: Proposal, record: Record | None, definition: TypeDef | None
) -> dict[str, Any]:
    """What a person needs to decide: the values of a new asset, or, for a change, each
    field it changes as it is now -> proposed."""
    proposed = proposal_fields(p)
    before = record_fields(record) if record else {}
    stale = service.whole_copy(p)
    if record and not stale:  # only what it changes, on the record as it is now
        after = {**before, **proposed}
        renamed = service.proposal_renames(p) or record.name
    else:
        after, renamed = proposed, p.name
    links = await module.links.resolve(before, after)
    fields = [(f.key, f.label, f.kind) for f in definition.fields] if definition else []
    known = {k for k, _, _ in fields}
    fields += [(k, k, "text") for k in sorted((set(before) | set(after)) - known)]
    rows = [
        {"field": label, "before": _cell(kind, before.get(key), links),
         "after": _cell(kind, after.get(key), links)}
        for key, label, kind in fields
        if _differs(record, before.get(key), after.get(key))
    ]  # fmt: skip
    if record and record.name != renamed:
        rows.insert(0, {"field": "Name", "before": record.name, "after": renamed})
    type_name = definition.name if definition else p.type_key
    return {
        "id": p.id,
        "title": f"Change to {record.name}" if record else f"New {_noun(type_name)}: {p.name}",
        "href": f"/assets/r/{record.id}" if record else None,
        "by": p.proposed_by + (" (via AI)" if p.proposed_via_ai else ""),
        "when": _when(p.created_at),
        "note": p.note,
        "rows": rows,
        "is_change": record is not None,
        "problem": service.WHOLE_COPY if record and stale else None,
    }


# ---- actions -------------------------------------------------------------------------------


@module.router.post("/records")
async def create_record(
    request: Request,
    who: CanWrite,
    type_key: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    fields = await _form_fields(request)
    try:
        async with module.session() as s:
            record = await service.create(s, who, type_key=type_key, name=name, fields=fields)
            await s.commit()
            rid = record.id
    except AssetsError as exc:
        return await module.forms.back(request, f"/assets/t/{type_key}/new", str(exc))
    return _back(f"/assets/r/{rid}")


@module.router.post("/records/{record_id}")
async def update_record(
    request: Request, record_id: str, who: CanWrite, name: Annotated[str, Form()] = ""
) -> RedirectResponse:
    fields = await _form_fields(request)
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            await service.update(s, who, record, name=name, fields=fields)
            await s.commit()
    except AssetsError as exc:
        return await module.forms.back(request, f"/assets/r/{record_id}/edit", str(exc))
    return _back(f"/assets/r/{record_id}")


async def _merge_label(record: Record) -> str:
    # Enough to tell copies of one machine apart: status and where it comes from.
    systems = sorted({x.system for x in await module.refs.links(record.type_key, str(record.id))})
    status = record_fields(record).get("status") or "no status"
    return f"{record.name}: {status}, " + (
        f"synced from {', '.join(systems)}" if systems else "kept by hand"
    )


@module.router.post("/records/{record_id}/accept")
async def accept_finding(
    request: Request,
    record_id: str,
    who: CanWrite,
    code: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            await service.accept_finding(s, who, record, code, reason)
            await s.commit()
    except AssetsError as exc:
        return await module.forms.back(request, f"/assets/r/{record_id}", str(exc))
    return _back(f"/assets/r/{record_id}")


@module.router.post("/records/{record_id}/unaccept/{code}")
async def unaccept_finding(record_id: str, code: str, who: CanWrite) -> RedirectResponse:
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            await service.unaccept_finding(s, who, record, code)
            await s.commit()
    except AssetsError as exc:
        return _back(f"/assets/r/{record_id}", str(exc))
    return _back(f"/assets/r/{record_id}")


@module.router.post("/records/{record_id}/merge")
async def merge_record(
    record_id: str, who: CanPublish, into: Annotated[str, Form()] = ""
) -> RedirectResponse:
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            kept = await service.get(s, into) if into else None
            if kept is None:
                raise AssetsError("Choose the record to keep.")
            await service.merge(s, who, record, kept)
            await s.commit()
    except AssetsError as exc:
        return _back(f"/assets/r/{record_id}", str(exc))
    return _back(f"/assets/r/{into}")


@module.router.post("/records/{record_id}/delete")
async def delete_record(record_id: str, who: CanPublish) -> RedirectResponse:
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            type_key = record.type_key
            await service.delete_record(s, who, record)
            await s.commit()
    except AssetsError as exc:
        return _back(f"/assets/r/{record_id}", str(exc))
    return _back(f"/assets/t/{type_key}")


async def _decide(proposal_id: int, who: Principal, approve: bool) -> RedirectResponse:
    try:
        async with module.session() as s:
            p = await s.get(Proposal, proposal_id)
            if p is None:
                raise HTTPException(status_code=404, detail="Proposal not found")
            await service.decide(s, who, p, approve=approve)
            await s.commit()
    except AssetsError as exc:
        return _back("/assets/proposals", str(exc))
    return _back("/assets/proposals")


@module.router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: int, who: CanPublish) -> RedirectResponse:
    return await _decide(proposal_id, who, True)


@module.router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: int, who: CanPublish) -> RedirectResponse:
    return await _decide(proposal_id, who, False)


@module.router.post("/readiness/targets")
async def save_new_target(
    request: Request, who: CanTarget, name: Text = "", upgrades_from: Text = "",
    processors: Text = "", min_memory_gb: Text = "", min_storage_gb: Text = "",
    min_tpm: Text = "", source: Text = "", notes: Text = "",
) -> RedirectResponse:  # fmt: skip
    try:
        async with module.session() as s:
            await service.save_target(
                s, who, None, name=name, upgrades_from=upgrades_from, processors=processors,
                min_memory_gb=min_memory_gb, min_storage_gb=min_storage_gb, min_tpm=min_tpm,
                source=source, notes=notes,
            )  # fmt: skip
            await s.commit()
    except AssetsError as exc:
        return await module.forms.back(request, "/assets/readiness/targets/new", str(exc))
    return _back("/assets/readiness")


@module.router.post("/readiness/targets/{target_id}")
async def save_target(
    request: Request, target_id: int, who: CanTarget, name: Text = "", upgrades_from: Text = "",
    processors: Text = "", min_memory_gb: Text = "", min_storage_gb: Text = "",
    min_tpm: Text = "", source: Text = "", notes: Text = "",
) -> RedirectResponse:  # fmt: skip
    try:
        async with module.session() as s:
            t = await s.get(OsTarget, target_id)
            if t is None:
                raise HTTPException(status_code=404, detail="OS target not found")
            await service.save_target(
                s, who, t, name=name, upgrades_from=upgrades_from, processors=processors,
                min_memory_gb=min_memory_gb, min_storage_gb=min_storage_gb, min_tpm=min_tpm,
                source=source, notes=notes,
            )  # fmt: skip
            await s.commit()
    except AssetsError as exc:
        return await module.forms.back(request, f"/assets/readiness/targets/{target_id}", str(exc))
    return _back("/assets/readiness")


@module.router.post("/readiness/targets/{target_id}/delete")
async def delete_target(target_id: int, who: CanTarget) -> RedirectResponse:
    try:
        async with module.session() as s:
            t = await s.get(OsTarget, target_id)
            if t is None:
                raise HTTPException(status_code=404, detail="OS target not found")
            await service.delete_target(s, who, t)
            await s.commit()
    except AssetsError as exc:
        return _back("/assets/readiness", str(exc))
    return _back("/assets/readiness")


@module.router.post("/readiness/presets")
async def add_preset(who: CanTarget, name: Text = "") -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.add_preset(s, who, name)
            await s.commit()
    except AssetsError as exc:
        return _back("/assets/readiness", str(exc))
    return _back("/assets/readiness")


@module.router.get("/readiness/{target_id}")
async def readiness_export(target_id: int, _: CanRead, format: str = "xlsx") -> Response:
    report = {t["id"]: t for t in await service.readiness_report()}
    t = report.get(target_id)
    if t is None:
        raise HTTPException(status_code=404, detail="OS target not found")
    rows = [
        {
            **d,
            "ready": rd.STATUS_LABELS[d["ready"]],
            "why": "; ".join(d["why"]),
            "memory_gb": d["memory_gb"] if d["memory_gb"] not in ("", None) else None,
            "storage_gb": d["storage_gb"] if d["storage_gb"] not in ("", None) else None,
        }
        for d in t["devices"]
    ]
    columns = [
        Column("name", "Device"),
        Column("status", "Status"),
        Column("model", "Model"),
        Column("operating_system", "Runs"),
        Column("processor", "Processor"),
        Column("memory_gb", "Memory (GB)", "number"),
        Column("storage_gb", "Storage (GB)", "number"),
        Column("tpm", "TPM"),
        Column("ready", f"Ready for {t['name']}"),
        Column("why", "Why"),
    ]
    try:
        return export_response(rows, columns, filename=f"{t['name']} readiness", format=format)
    except ExportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
