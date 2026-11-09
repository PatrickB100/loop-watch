import asyncio
import concurrent.futures
import logging
import os
import re
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from loopwatch.collector import MAX_SESSIONS, poll_once, run_collector, utcnow
from loopwatch.inventory import Inventory, InventoryError, load_inventory
from loopwatch.profiles import profile_for, version_of
from loopwatch.report.render import build_report, render_html, render_text
from loopwatch.store import Store
from loopwatch.transport import FakeTransport, NetmikoTransport, RecordingTransport, Transport, TransportError

SNAPSHOT_WINDOW = timedelta(minutes=15)
_SINCE = re.compile(r"^(\d+)([mhd])$")


def parse_since(text: str) -> timedelta:
    m = _SINCE.match(text.strip())
    if not m:
        raise click.BadParameter("use a number followed by m, h or d, for example 30m, 2h or 1d")
    value, unit = int(m.group(1)), m.group(2)
    return timedelta(seconds=value * {"m": 60, "h": 3600, "d": 86400}[unit])


def _inventory(path: Path) -> Inventory:
    try:
        return load_inventory(path, os.environ)
    except FileNotFoundError:
        raise click.ClickException(f"inventory file not found: {path}")
    except InventoryError as exc:
        raise click.ClickException(str(exc))


def _transport(fixtures: Path | None) -> Transport:
    return FakeTransport(fixtures) if fixtures else NetmikoTransport()


def _open_store(path: Path) -> Store:
    store = Store(path)
    store.init_schema()
    return store


@click.group()
@click.option("--inventory", "inventory_path", type=click.Path(path_type=Path), default="inventory.yaml",
              envvar="LOOPWATCH_INVENTORY", show_default=True, help="Switch list (YAML).")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default="loopwatch.sqlite",
              envvar="LOOPWATCH_DB", show_default=True, help="SQLite database file.")
@click.pass_context
def main(ctx: click.Context, inventory_path: Path, db_path: Path) -> None:
    """Watch Cisco switches for the signs of a layer-2 loop and report where it entered."""
    ctx.ensure_object(dict)
    ctx.obj["inventory_path"] = inventory_path
    ctx.obj["db_path"] = db_path


@main.command()
@click.pass_context
def watch(ctx: click.Context) -> None:
    """Poll every switch continuously until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    inventory_path: Path = ctx.obj["inventory_path"]
    stop = asyncio.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        with _open_store(ctx.obj["db_path"]) as store:
            await run_collector(lambda: load_inventory(inventory_path, os.environ), NetmikoTransport(), store,
                                stop=stop, inventory_path=inventory_path)

    asyncio.run(run())


@main.command()
@click.option("--since", "since_text", default=None, help="Window ending now, for example 30m, 2h, 1d.")
@click.option("--from", "from_text", default=None, help="Window start, ISO-8601.")
@click.option("--to", "to_text", default=None, help="Window end, ISO-8601 (default: now).")
@click.option("--html", "html_path", type=click.Path(path_type=Path), default=None, help="Write an HTML report here.")
@click.pass_context
def report(ctx: click.Context, since_text: str | None, from_text: str | None, to_text: str | None,
           html_path: Path | None) -> None:
    """Timeline and ranked suspect ports for a time window (default: the last 2 hours)."""
    until = datetime.fromisoformat(to_text) if to_text else utcnow()
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if from_text:
        since = datetime.fromisoformat(from_text)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        since = until - parse_since(since_text or "2h")
    with _open_store(ctx.obj["db_path"]) as store:
        managed = _managed_names(ctx.obj["inventory_path"], store)
        data = build_report(store, managed, since, until)
    if html_path:
        html_path.write_text(render_html(data))
        click.echo(f"wrote {html_path}")
    else:
        click.echo(render_text(data), nl=False)


def _managed_names(inventory_path: Path, store: Store) -> frozenset[str]:
    """Managed switch names from the inventory when it is readable, else whatever the database knows."""
    try:
        return load_inventory(inventory_path, os.environ).names
    except (FileNotFoundError, InventoryError):
        return frozenset(store.switches())


@main.command()
@click.argument("switches", nargs=-1)
@click.option("--record", "record_dir", type=click.Path(path_type=Path), default=None,
              help="Also save raw command output here, in the fixture layout.")
@click.option("--transport-fixtures", type=click.Path(path_type=Path), default=None, hidden=True)
@click.pass_context
def snapshot(ctx: click.Context, switches: tuple[str, ...], record_dir: Path | None, transport_fixtures: Path | None) -> None:
    """Poll the given switches (default: all) once, now, and print the suspects of the last 15 minutes."""
    inv = _inventory(ctx.obj["inventory_path"])
    targets = list(inv.switches)
    if switches:
        unknown = [s for s in switches if s not in inv.names]
        if unknown:
            raise click.ClickException(f"not in the inventory: {', '.join(unknown)}")
        targets = [inv.by_name(s) for s in switches]
    transport = _transport(transport_fixtures)
    if record_dir:
        transport = RecordingTransport(transport, record_dir)
    now = utcnow()
    with _open_store(ctx.obj["db_path"]) as store:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SESSIONS) as pool:
            futures = {pool.submit(poll_once, sw, inv, transport, store, None, now): sw for sw in targets}
            for future, sw in futures.items():
                snap, events = future.result()
                click.echo(f"{sw.name}: {'ok' if snap else 'unreachable'}, {len(events)} event(s)", err=True)
        data = build_report(store, inv.names, now - SNAPSHOT_WINDOW, now)
    click.echo(render_text(data), nl=False)


@main.command("check-inventory")
@click.option("--transport-fixtures", type=click.Path(path_type=Path), default=None, hidden=True)
@click.pass_context
def check_inventory(ctx: click.Context, transport_fixtures: Path | None) -> None:
    """Connect to every device once and read its software version."""
    inv = _inventory(ctx.obj["inventory_path"])
    transport = _transport(transport_fixtures)
    failed = 0
    for sw in inv.switches:
        profile = profile_for(sw.device_type)
        try:
            out = transport.run(sw, inv.credentials_for(sw), [profile.version_command])
        except TransportError as exc:
            failed += 1
            click.echo(f"{sw.name:<24} FAIL {exc.message}")
            continue
        version = version_of(profile, out[profile.version_command])
        click.echo(f"{sw.name:<24} ok   {version or 'version unknown'}")
    if failed:
        sys.exit(1)
