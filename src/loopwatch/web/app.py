"""The web page: current state, reports for a window, device editing. Served by `watch` in the
collector's event loop; store and transport calls run in threads."""

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from jinja2 import ChoiceLoader, Environment, PackageLoader

from loopwatch.collector import utcnow
from loopwatch.inventory import (
    Inventory,
    InventoryError,
    Switch,
    credential_sets_from_env,
    load_inventory,
    parse_inventory,
    save_inventory,
    validate_device,
)
from loopwatch.ports import normalize_port
from loopwatch.profiles import PROFILES, profile_for, version_of
from loopwatch.report.render import build_report, jinja_env
from loopwatch.store import Store
from loopwatch.transport import Transport, TransportError

CTX_KEY: web.AppKey["WebContext"] = web.AppKey("ctx")
JINJA_KEY: web.AppKey[Environment] = web.AppKey("jinja")
HOME_WINDOW = timedelta(hours=2)
RECENT = timedelta(hours=1)
_SINCE = re.compile(r"^(\d+)([mhd])$")
_STATIC = Path(__file__).parent / "static"


@dataclass
class WebContext:
    store: Store
    inventory_path: Path
    env: Mapping[str, str]
    tz: tzinfo
    transport: Transport
    reload: asyncio.Event
    collector_alive: Callable[[], bool]
    inventory_error: Callable[[], str | None]  # the collector's last load error, if any
    clock: Callable[[], datetime] = utcnow


def parse_since(text: str) -> timedelta:
    m = _SINCE.match(text.strip())
    if not m:
        raise ValueError("use a number followed by m, h or d, for example 30m, 2h or 1d")
    value, unit = int(m.group(1)), m.group(2)
    return timedelta(seconds=value * {"m": 60, "h": 3600, "d": 86400}[unit])


def parse_window(query: Mapping[str, str], now: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    """`since=2h`, or `from=`/`to=` as local ISO times in `tz`. Raises ValueError on bad input."""
    if query.get("from"):
        since = _local(query["from"], tz)
        until = _local(query["to"], tz) if query.get("to") else now
    else:
        until = now
        since = now - parse_since(query.get("since", "2h"))
    if since >= until:
        raise ValueError("the window start must be before its end")
    return since, until


def _local(text: str, tz: tzinfo) -> datetime:
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"not a date and time: {text!r}") from None
    return value.replace(tzinfo=tz) if value.tzinfo is None else value


def _load(ctx: WebContext) -> tuple[Inventory | None, str | None]:
    try:
        return load_inventory(ctx.inventory_path, ctx.env), None
    except (InventoryError, OSError, ValueError) as exc:
        return None, str(exc)


def _env(tz: tzinfo) -> Environment:
    loader = ChoiceLoader([PackageLoader("loopwatch.web", "templates"), PackageLoader("loopwatch.report", "templates")])
    env = jinja_env(tz, loader)
    env.filters["local_input"] = lambda t: t.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
    return env


def _render(request: web.Request, template: str, status: int = 200, **values: object) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    html = request.app[JINJA_KEY].get_template(template).render(ctx=ctx, now=ctx.clock(), **values)
    return web.Response(text=html, content_type="text/html", status=status)


async def home(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    now = ctx.clock()

    def gather() -> dict[str, object]:
        inv, load_error = _load(ctx)
        devices = list(inv.switches) if inv else [Switch(name, "") for name in ctx.store.switches()]
        rows = []
        for sw in devices:
            last = ctx.store.last_poll(sw.name)
            events = ctx.store.events(now - RECENT, now, sw.name)
            rows.append({
                "switch": sw, "last": last[0] if last else None, "ok": last[1] if last else None,
                "error": last[2] if last else None, "events": len(events),
                "high": sum(1 for e in events if e.severity == "high"),
            })
        names = {sw.name for sw in devices}
        data = build_report(ctx.store, names, now - HOME_WINDOW, now, inv.ap_ports() if inv else frozenset())
        return {"rows": rows, "data": data, "error": ctx.inventory_error() or load_error}

    values = await asyncio.to_thread(gather)
    return _render(request, "home.html.j2", **values)


async def report(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    try:
        since, until = parse_window(request.query, ctx.clock(), ctx.tz)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))

    def gather():
        inv, _ = _load(ctx)
        names = inv.names if inv else frozenset(ctx.store.switches())
        return build_report(ctx.store, names, since, until, inv.ap_ports() if inv else frozenset())

    data = await asyncio.to_thread(gather)
    return _render(request, "report.html.j2", data=data, since=since, until=until, query=dict(request.query))


async def healthz(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]

    def gather() -> dict[str, object]:
        inv, _ = _load(ctx)
        names = [s.name for s in inv.switches] if inv else ctx.store.switches()
        polls = [p[0] for p in (ctx.store.last_poll(n) for n in names) if p]
        return {"devices": len(names), "last_poll": max(polls).isoformat() if polls else None}

    body = await asyncio.to_thread(gather)
    alive = ctx.collector_alive()
    body["status"] = "ok" if alive else "collector stopped"
    return web.json_response(body, status=200 if alive else 503)


# --- devices ---------------------------------------------------------------

def _form_switch(form: Mapping[str, str], skip: list[str]) -> tuple[Switch, list[str]]:
    """A Switch from posted fields plus the problems the field types themselves raise."""
    problems: list[str] = []
    try:
        interval = int(form.get("poll_interval", "").strip() or 0)
    except ValueError:
        interval = 0
        problems.append("Poll interval must be a whole number of seconds")
    ap_ports = frozenset(normalize_port(p) for p in re.split(r"[,\s]+", form.get("ap_ports", "")) if p)
    switch = Switch(
        name=form.get("name", "").strip(),
        host=form.get("host", "").strip(),
        device_type=form.get("device_type", "").strip(),
        poll_interval=interval,
        skip=frozenset(skip),
        credentials=form.get("credentials", "").strip(),
        ap_ports=ap_ports,
    )
    return switch, problems


def _form_values(ctx: WebContext, switch: Switch | None, **extra: object) -> dict[str, object]:
    return {
        "switch": switch,
        "credential_sets": sorted(credential_sets_from_env(ctx.env)),
        "device_types": sorted(t for p in PROFILES for t in p.device_types),
        "families": [(p.family, sorted(p.device_types), p.commands) for p in PROFILES],
        "errors": [],
        "test_result": None,
        **extra,
    }


def _find(switches: list[Switch], name: str) -> Switch:
    for s in switches:
        if s.name == name:
            return s
    raise web.HTTPNotFound(text=f"no device named {name!r}")


async def devices(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]

    def gather():
        switches, _ = parse_inventory(ctx.inventory_path)
        sets = credential_sets_from_env(ctx.env).keys()
        return [(s, validate_device(s, sets)) for s in switches]

    try:
        rows = await asyncio.to_thread(gather)
        error = None
    except InventoryError as exc:
        rows, error = [], str(exc)
    return _render(request, "devices.html.j2", rows=rows, error=error)


async def device_form(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    name = request.match_info.get("name")
    switches, _ = await asyncio.to_thread(parse_inventory, ctx.inventory_path)
    switch = _find(switches, name) if name else None
    return _render(request, "device_form.html.j2", **_form_values(ctx, switch, is_new=name is None))


async def device_save(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    name = request.match_info.get("name")
    form = await request.post()
    switch, problems = _form_switch({k: str(v) for k, v in form.items()}, [str(v) for v in form.getall("skip", [])])
    switches, defaults = await asyncio.to_thread(parse_inventory, ctx.inventory_path)
    if name is not None:
        _find(switches, name)
    problems += validate_device(switch, credential_sets_from_env(ctx.env).keys())
    if any(s.name == switch.name and s.name != name for s in switches):
        problems.append(f"A device named {switch.name!r} already exists")
    if problems:
        values = _form_values(ctx, switch, is_new=name is None, errors=problems)
        return _render(request, "device_form.html.j2", status=400, **values)
    updated = [switch if s.name == name else s for s in switches] if name else [*switches, switch]
    await asyncio.to_thread(save_inventory, ctx.inventory_path, updated, defaults)
    ctx.reload.set()
    raise web.HTTPSeeOther("/devices")


async def device_delete(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    name = request.match_info["name"]
    switches, defaults = await asyncio.to_thread(parse_inventory, ctx.inventory_path)
    _find(switches, name)
    await asyncio.to_thread(save_inventory, ctx.inventory_path, [s for s in switches if s.name != name], defaults)
    ctx.reload.set()
    raise web.HTTPSeeOther("/devices")


async def device_test(request: web.Request) -> web.Response:
    ctx: WebContext = request.app[CTX_KEY]
    name = request.match_info["name"]
    switches, _ = await asyncio.to_thread(parse_inventory, ctx.inventory_path)
    switch = _find(switches, name)
    sets = credential_sets_from_env(ctx.env)

    def run() -> tuple[bool, str]:
        if switch.credentials not in sets:
            return False, f"credential set {switch.credentials!r} is not defined in the environment"
        try:
            profile = profile_for(switch.device_type)
            out = ctx.transport.run(switch, sets[switch.credentials], [profile.version_command])
        except (InventoryError, TransportError) as exc:
            return False, getattr(exc, "message", str(exc))
        return True, f"connected; version {version_of(profile, out[profile.version_command]) or 'unknown'}"

    ok, message = await asyncio.to_thread(run)
    values = _form_values(ctx, switch, is_new=False, test_result={"ok": ok, "message": message})
    return _render(request, "device_form.html.j2", **values)


@web.middleware
async def same_origin(request: web.Request, handler):
    """Refuse POSTs whose Origin names another host: the only guard, by decision, against a page
    elsewhere on the LAN driving a browser that can reach this one."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="cross-origin request refused")
    return await handler(request)


def create_app(ctx: WebContext) -> web.Application:
    app = web.Application(middlewares=[same_origin])
    app[CTX_KEY] = ctx
    app[JINJA_KEY] = _env(ctx.tz)
    app.router.add_get("/", home)
    app.router.add_get("/report", report)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/devices", devices)
    app.router.add_get("/devices/new", device_form)
    app.router.add_post("/devices/new", device_save)
    app.router.add_get("/devices/{name}", device_form)
    app.router.add_post("/devices/{name}", device_save)
    app.router.add_post("/devices/{name}/delete", device_delete)
    app.router.add_post("/devices/{name}/test", device_test)
    app.router.add_static("/static/", path=str(_STATIC), name="static")
    return app


async def serve(app: web.Application, port: int) -> web.AppRunner:
    """Bind on every interface; an OSError (port in use) propagates to the caller."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
