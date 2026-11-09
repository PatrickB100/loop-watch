from loopwatch.inventory import load_inventory

NEW = {"name": "ccr", "host": "10.0.0.254", "device_type": "mikrotik_routeros", "credentials": "mikrotik",
       "poll_interval": "30", "ap_ports": ""}


async def test_devices_page_lists_inventory(client):
    body = await (await client.get("/devices")).text()
    assert "sw-a" in body and "sw-core-1" in body and "Add device" in body


async def test_add_edit_delete_round_trip(client, ctx):
    r = await client.post("/devices/new", data=NEW, allow_redirects=False)
    assert r.status == 303 and ctx.reload.is_set()
    inv = load_inventory(ctx.inventory_path, ctx.env)
    assert inv.by_name("ccr").poll_interval == 30 and inv.by_name("ccr").credentials == "mikrotik"
    edited = dict(NEW, host="10.0.0.253", poll_interval="60", ap_ports="ether2, ether3")
    r = await client.post("/devices/ccr", data=edited, allow_redirects=False)
    assert r.status == 303
    assert load_inventory(ctx.inventory_path, ctx.env).by_name("ccr").ap_ports == frozenset({"ether2", "ether3"})
    r = await client.post("/devices/ccr/delete", allow_redirects=False)
    assert r.status == 303 and "ccr" not in load_inventory(ctx.inventory_path, ctx.env).names


async def test_edit_form_is_prefilled_and_skip_is_saved(client, ctx):
    body = await (await client.get("/devices/sw-a")).text()
    assert 'value="10.0.0.5"' in body and "show mac address-table dynamic" in body
    data = {"name": "sw-a", "host": "10.0.0.5", "device_type": "cisco_ios", "credentials": "ssh",
            "poll_interval": "60", "ap_ports": "Gi1/0/20", "skip": "show mac address-table dynamic"}
    r = await client.post("/devices/sw-a", data=data, allow_redirects=False)
    assert r.status == 303
    sw = load_inventory(ctx.inventory_path, ctx.env).by_name("sw-a")
    assert sw.skip == frozenset({"show mac address-table dynamic"}) and sw.ap_ports == frozenset({"Gi1/0/20"})


async def test_rename_replaces_the_entry(client, ctx):
    data = {"name": "sw-b", "host": "10.0.0.5", "device_type": "cisco_ios", "credentials": "ssh",
            "poll_interval": "60", "ap_ports": ""}
    assert (await client.post("/devices/sw-a", data=data, allow_redirects=False)).status == 303
    names = load_inventory(ctx.inventory_path, ctx.env).names
    assert "sw-b" in names and "sw-a" not in names


async def test_validation_errors_are_shown(client):
    bad = {"name": "bad name", "host": "h", "device_type": "cisco_ios", "credentials": "ssh",
           "poll_interval": "5", "ap_ports": ""}
    r = await client.post("/devices/new", data=bad)
    body = await r.text()
    assert r.status == 400 and "between 10 and 3600" in body and "Name must" in body


async def test_duplicate_name_is_rejected(client):
    dup = dict(NEW, name="sw-a")
    r = await client.post("/devices/new", data=dup)
    assert r.status == 400 and "already exists" in await r.text()


async def test_unknown_device_is_404(client):
    assert (await client.get("/devices/nope")).status == 404
    assert (await client.post("/devices/nope/delete")).status == 404


async def test_form_offers_credential_set_names_only(client):
    body = await (await client.get("/devices/new")).text()
    assert "ssh" in body and "mikrotik" in body and "hunter2" not in body and "hunter3" not in body


async def test_test_connection_success_and_failure(client, client_unreachable):
    body = await (await client.post("/devices/sw-a/test")).text()
    assert "15.2" in body
    body = await (await client_unreachable.post("/devices/sw-a/test")).text()
    assert "timed out" in body and "hunter2" not in body


async def test_cross_origin_post_is_refused(client, ctx):
    r = await client.post("/devices/sw-a/delete", headers={"Origin": "http://evil.example"}, allow_redirects=False)
    assert r.status == 403 and "sw-a" in load_inventory(ctx.inventory_path, ctx.env).names
    r = await client.post("/devices/sw-a/delete", headers={"Origin": f"http://{client.host}:{client.port}"},
                          allow_redirects=False)
    assert r.status == 303
