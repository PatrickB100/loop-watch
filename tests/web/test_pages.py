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
