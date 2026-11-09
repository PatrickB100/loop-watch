import asyncio
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from loopwatch.model import Event
from loopwatch.store import Store
from loopwatch.transport import FakeTransport
from loopwatch.web.app import WebContext, create_app
from tests.conftest import FIXTURES, NOW

ENV = {"LOOPWATCH_SSH_USER": "ro", "LOOPWATCH_SSH_PASSWORD": "hunter2",
       "LOOPWATCH_MIKROTIK_USER": "mt", "LOOPWATCH_MIKROTIK_PASSWORD": "hunter3"}


@pytest.fixture
def seeded_store(snap_a):
    with Store(":memory:") as s:
        s.init_schema()
        s.record_poll("sw-a", "10.0.0.5", NOW - timedelta(minutes=30), NOW - timedelta(minutes=30), snap_a, None)
        s.record_poll("sw-core-1", "10.0.0.1", NOW - timedelta(minutes=31), NOW - timedelta(minutes=31), None, "timed out")
        s.write_events([
            Event(NOW - timedelta(minutes=20), "sw-a", "broadcast_storm", "high", "storm", port="Gi1/0/14"),
            Event(NOW - timedelta(minutes=31), "sw-core-1", "unreachable", "low", "timed out"),
        ])
        yield s


@pytest.fixture
def inventory_file(tmp_path):
    p = tmp_path / "inventory.yaml"
    p.write_text("switches:\n  - {name: sw-a, host: 10.0.0.5}\n  - {name: sw-core-1, host: 10.0.0.1}\n")
    return p


@pytest.fixture
def ctx(seeded_store, inventory_file):
    return WebContext(
        store=seeded_store,
        inventory_path=inventory_file,
        env=ENV,
        tz=timezone.utc,
        transport=FakeTransport(FIXTURES / "ios15"),
        reload=asyncio.Event(),
        collector_alive=lambda: True,
        inventory_error=lambda: None,
        clock=lambda: NOW,
    )


@pytest.fixture
async def client(aiohttp_client, ctx):
    return await aiohttp_client(create_app(ctx))


@pytest.fixture
async def client_empty(aiohttp_client, tmp_path):
    with Store(":memory:") as s:
        s.init_schema()
        ctx = WebContext(s, tmp_path / "none.yaml", ENV, timezone.utc, FakeTransport(FIXTURES / "ios15"),
                         asyncio.Event(), lambda: True, lambda: None, lambda: NOW)
        yield await aiohttp_client(create_app(ctx))


@pytest.fixture
async def client_with_error(aiohttp_client, ctx):
    return await aiohttp_client(create_app(replace(ctx, inventory_error=lambda: "bad yaml")))


@pytest.fixture
async def client_unreachable(aiohttp_client, ctx, tmp_path):
    (tmp_path / "fx" / "sw-a").mkdir(parents=True)
    (tmp_path / "fx" / "sw-a" / "UNREACHABLE").touch()
    return await aiohttp_client(create_app(replace(ctx, transport=FakeTransport(tmp_path / "fx"))))
