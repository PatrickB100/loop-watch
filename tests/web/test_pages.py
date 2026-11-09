from loopwatch.web.app import create_app

async def test_home_lists_devices_and_suspects(client):
    r = await client.get("/")
    body = await r.text()
    assert r.status == 200 and "sw-a" in body and "Gi1/0/14" in body and "hunter2" not in body
    assert "unreachable" in body  # sw-core-1's last poll failed


async def test_home_with_empty_inventory_says_so(client_empty):
    body = await (await client_empty.get("/")).text()
    assert "No devices yet" in body and "Add device" in body


async def test_home_shows_inventory_error(client_with_error):
    assert "bad yaml" in await (await client_with_error.get("/")).text()


async def test_report_since_and_window(client):
    assert (await client.get("/report?since=2h")).status == 200
    r = await client.get("/report?from=2026-11-09T09:00&to=2026-11-09T11:00")
    body = await r.text()
    assert r.status == 200 and "<h2>Timeline</h2>" in body and "broadcast_storm" in body
    assert (await client.get("/report?since=bogus")).status == 400
    assert (await client.get("/report?from=nope")).status == 400


async def test_static_css_is_served(client):
    r = await client.get("/static/style.css")
    assert r.status == 200 and "text/css" in r.headers["Content-Type"]


async def test_healthz(client, ctx):
    r = await client.get("/healthz")
    assert r.status == 200 and (await r.json())["devices"] == 2
    ctx.collector_alive = lambda: False
    assert (await client.get("/healthz")).status == 503


async def test_report_window_in_local_zone_finds_the_event(aiohttp_client, ctx):
    from dataclasses import replace
    from zoneinfo import ZoneInfo

    client = await aiohttp_client(create_app(replace(ctx, tz=ZoneInfo("Europe/Amsterdam"))))
    # the storm is at 09:40 UTC = 10:40 Amsterdam
    body = await (await client.get("/report?from=2026-11-09T10:30&to=2026-11-09T10:50")).text()
    assert "broadcast_storm" in body
    body = await (await client.get("/report?from=2026-11-09T09:30&to=2026-11-09T09:50")).text()
    assert "broadcast_storm" not in body


async def test_malformed_inventory_file_is_shown_not_a_500(client, ctx):
    ctx.inventory_path.write_text("switches: 5\n")
    for path in ("/", "/devices", "/devices/new", "/devices/sw-a"):
        r = await client.get(path)
        body = await r.text()
        assert r.status in (200, 500) and "switches" in body and "Traceback" not in body, path
    assert (await client.get("/healthz")).status == 200
    r = await client.post("/devices/sw-a/delete", allow_redirects=False)
    assert r.status == 500 and "Traceback" not in await r.text()
