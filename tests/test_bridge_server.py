"""HTTP surface: auth on every route, strict validation before any radio use, honest job API."""

import asyncio
import base64
import json
import logging
import socket
from types import SimpleNamespace

import att_fake as fake
import pytest
import pytest_asyncio
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from shared_loader import load
from tinyprint_bridge import config as bridge_config
from tinyprint_bridge import jobs, server

wire = load("bridge_wire")
protocol = load("protocol")

TOKEN = "Tk3-" + "x" * 44
PRINTER_MAC = "4C:2A:EE:D3:3E:C2"
ADAPTER_MAC = "00:1A:7D:DA:71:13"
FAST = dict(chunk_delay=0, pause_timeout=0.5)
BITMAP = bytes(range(48)) * 2


def bearer(token: str, scheme: str = "Bearer") -> dict[str, str]:
    return {"Authorization": f"{scheme} {token}"}


AUTH = bearer(TOKEN)


def make_config():
    return bridge_config.BridgeConfig(PRINTER_MAC, ADAPTER_MAC, TOKEN)


def body(job_id="job-000001", rows=2, bitmap=BITMAP):
    return {"job_id": job_id, "rows": rows, "bitmap_b64": base64.b64encode(bitmap).decode()}


@pytest_asyncio.fixture
async def env():
    gatt = fake.FakeGattServer()
    link = fake.FakeLink(gatt)
    manager = jobs.JobManager(
        link,
        cancel_wait=2,
        connect_timeout=0.5,
        discovery_timeout=1,
        request_timeout=0.3,
        send_timeout=0.3,
        pacing=FAST,
    )
    app = server.create_app(make_config(), manager)
    async with TestClient(TestServer(app)) as client:
        yield SimpleNamespace(client=client, link=link, gatt=gatt, manager=manager)


async def wait_terminal(env, job_id):
    for _ in range(400):
        response = await env.client.get(f"/v1/jobs/{job_id}", headers=AUTH)
        view = await response.json()
        if view["terminal"]:
            return view
        await asyncio.sleep(0.005)
    raise AssertionError("job did not finish")


# -- authentication ---------------------------------------------------------------------------

ROUTES = [
    ("GET", "/v1/status"),
    ("POST", "/v1/jobs"),
    ("POST", "/v1/diagnose"),
    ("GET", "/v1/jobs/job-000001"),
    ("POST", "/v1/jobs/job-000001/cancel"),
    ("GET", "/nope"),
    ("GET", "/"),
    ("DELETE", "/v1/status"),
    ("PUT", "/v1/jobs"),
]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        bearer("wrong-" * 8),
        bearer(TOKEN, "Basic"),
        {"Authorization": TOKEN},
        bearer(" " + TOKEN),
        bearer(TOKEN + "x"),
        bearer(TOKEN[:-1]),
    ],
)
async def test_every_route_requires_the_exact_bearer_token(env, method, path, headers):
    response = await env.client.request(method, path, headers=headers, data=b"{}")
    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    payload = await response.json()
    assert payload == {"error": {"code": "unauthorized", "message": "Authentication required"}}
    assert env.link.connects == 0


async def test_authentication_precedes_body_reading(env):
    response = await env.client.post("/v1/jobs", data=b"x" * (300 * 1024))
    assert response.status == 401


async def test_unknown_path_and_wrong_method_are_json_errors_when_authenticated(env):
    response = await env.client.get("/nope", headers=AUTH)
    assert response.status == 404
    assert (await response.json())["error"]["code"] == "not_found"
    response = await env.client.delete("/v1/status", headers=AUTH)
    assert response.status == 405
    assert (await response.json())["error"]["code"] == "method_not_allowed"


async def test_scheme_is_case_insensitive(env):
    response = await env.client.get("/v1/status", headers=bearer(TOKEN, "bearer"))
    assert response.status == 200


async def test_token_is_compared_with_compare_digest_on_sha256_digests(env, monkeypatch):
    calls = []
    original = server.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(server.hmac, "compare_digest", spy)
    await env.client.get("/v1/status", headers=AUTH)
    await env.client.get("/v1/status")
    assert len(calls) == 2
    assert all(len(a) == 32 and len(b) == 32 for a, b in calls)


# -- status -----------------------------------------------------------------------------------


async def test_status_is_cheap_and_never_touches_the_radio(env):
    for _ in range(3):
        response = await env.client.get("/v1/status", headers=AUTH)
    assert response.status == 200
    payload = await response.json()
    assert payload["ok"] is True and payload["protocol"] == 1 and payload["busy"] is False
    assert payload["active_job_id"] is None and payload["retained_jobs"] == 0
    assert payload["printer_configured"] is True and payload["version"]
    assert isinstance(payload["uptime_seconds"], int | float)
    assert env.link.connects == 0
    assert PRINTER_MAC not in json.dumps(payload) and ADAPTER_MAC not in json.dumps(payload)


# -- validation happens before any radio activity ------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        body(rows=True),
        body(rows=False),
        body(rows="2"),
        body(rows=0),
        body(rows=2049),
        body(rows=2.0),
        body(rows=None),
        body(job_id="short"),
        body(job_id="has space in it"),
        body(job_id=12345678),
        {**body(), "bitmap_b64": "!!!not-base64!!!"},
        {**body(), "bitmap_b64": base64.b64encode(bytes(95)).decode()},
        {**body(), "bitmap_b64": 5},
        {**body(), "address": PRINTER_MAC},
        {**body(), "path": "/etc/passwd"},
        {"job_id": "job-000001", "rows": 2},
        [],
        "text",
        5,
        None,
    ],
)
async def test_invalid_print_bodies_are_rejected_without_radio_activity(env, payload):
    response = await env.client.post(
        "/v1/jobs",
        data=json.dumps(payload),
        headers=AUTH | {"Content-Type": "application/json"},
    )
    assert response.status == 400, await response.text()
    assert (await response.json())["error"]["code"] == "bad_request"
    assert env.link.connects == 0 and not env.manager.busy


@pytest.mark.parametrize("raw", [b"", b"{", b"not json", b'{"a": NaN', b"[" * 5000])
async def test_broken_json_is_bad_request(env, raw):
    response = await env.client.post(
        "/v1/jobs", data=raw, headers=AUTH | {"Content-Type": "application/json"}
    )
    assert response.status == 400
    assert env.link.connects == 0


async def test_wrong_content_type_is_415(env):
    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        response = await env.client.post(
            "/v1/jobs", data=json.dumps(body()), headers=AUTH | {"Content-Type": content_type}
        )
        assert response.status == 415
        assert (await response.json())["error"]["code"] == "unsupported_media_type"
    response = await env.client.post("/v1/jobs", data=b"{}", headers=AUTH)
    assert response.status == 415
    assert env.link.connects == 0


async def test_oversized_body_is_413(env):
    response = await env.client.post(
        "/v1/jobs",
        data=b'{"x": "' + b"a" * (wire.MAX_BODY_BYTES + 10) + b'"}',
        headers=AUTH | {"Content-Type": "application/json"},
    )
    assert response.status == 413
    assert (await response.json())["error"]["code"] == "payload_too_large"
    assert env.link.connects == 0


async def test_stalled_body_is_cut_off_after_authentication(env, monkeypatch):
    monkeypatch.setattr(server, "BODY_READ_TIMEOUT", 0.2)
    host, port = env.client.server.host, env.client.server.port
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            (
                f"POST /v1/jobs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"
            ).encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
    finally:
        writer.close()
    assert head.startswith(b"HTTP/1.1 408")
    assert env.link.connects == 0


async def test_diagnose_validates_too(env):
    for payload in ({}, {"job_id": "x"}, {"job_id": "diag-000001", "extra": 1}, []):
        response = await env.client.post("/v1/diagnose", json=payload, headers=AUTH)
        assert response.status == 400
    assert env.link.connects == 0


async def test_job_id_in_path_is_validated(env):
    response = await env.client.get("/v1/jobs/..%2f..%2fetc", headers=AUTH)
    assert response.status in (400, 404)
    response = await env.client.get("/v1/jobs/short", headers=AUTH)
    assert response.status == 400
    response = await env.client.get("/v1/jobs/job-unknown1", headers=AUTH)
    assert response.status == 404


# -- job API -----------------------------------------------------------------------------------


async def test_print_job_is_accepted_then_polled_to_sent(env):
    response = await env.client.post("/v1/jobs", json=body(), headers=AUTH)
    assert response.status == 202
    started = wire.parse_job_view(await response.json())
    assert started["state"] == "accepted" and not started["terminal"]
    final = wire.parse_job_view(await wait_terminal(env, "job-000001"))
    assert final["state"] == "sent" and final["confirmation"] == "transport_only"
    assert final["physical_print_confirmed"] is False
    sent = protocol.get_state_frame() + b"".join(protocol.print_frames(BITMAP, 2))
    assert b"".join(v for _h, v in env.gatt.commands) == sent
    assert env.link.connects == 1


async def test_diagnose_job_over_http(env):
    response = await env.client.post("/v1/diagnose", json={"job_id": "diag-000001"}, headers=AUTH)
    assert response.status == 202
    final = wire.parse_job_view(await wait_terminal(env, "diag-000001"))
    assert final["kind"] == "diagnose" and final["diagnosis"]["profile_supported"] is True
    assert env.gatt.commands == []


async def test_busy_and_duplicate_are_409(env):
    env.link.connect_delay = 0.2
    first = await env.client.post("/v1/jobs", json=body("job-000001"), headers=AUTH)
    assert first.status == 202
    busy = await env.client.post("/v1/jobs", json=body("job-000002"), headers=AUTH)
    assert busy.status == 409 and (await busy.json())["error"]["code"] == "busy"
    dup = await env.client.post("/v1/jobs", json=body("job-000001"), headers=AUTH)
    assert dup.status == 409 and (await dup.json())["error"]["code"] == "duplicate_job"
    await wait_terminal(env, "job-000001")
    again = await env.client.post("/v1/jobs", json=body("job-000001"), headers=AUTH)
    assert again.status == 409 and (await again.json())["error"]["code"] == "duplicate_job"
    assert env.link.connects == 1


async def test_cancel_over_http_answers_only_after_cleanup(env):
    env.gatt.on_command = lambda srv, h, v: (
        srv.push_notification(srv.notify_handle, fake.PAUSE) if len(srv.commands) == 1 else None
    )
    env.manager._pacing = FAST | dict(pause_timeout=30, chunk_delay=0.002)
    await env.client.post("/v1/jobs", json=body(), headers=AUTH)
    for _ in range(200):
        if env.gatt.commands:
            break
        await asyncio.sleep(0.005)
    response = await env.client.post("/v1/jobs/job-000001/cancel", headers=AUTH)
    assert response.status == 200
    view = wire.parse_job_view(await response.json())
    assert view["state"] == "cancelled" and view["delivery_uncertain"] is True
    assert env.gatt.closed and not env.manager.busy
    status = await (await env.client.get("/v1/status", headers=AUTH)).json()
    assert status["busy"] is False
    again = await env.client.post("/v1/jobs/job-000001/cancel", headers=AUTH)
    assert wire.parse_job_view(await again.json()) == view


async def test_cancel_unknown_job_is_404(env):
    response = await env.client.post("/v1/jobs/job-unknown1/cancel", headers=AUTH)
    assert response.status == 404
    assert (await response.json())["error"]["code"] == "not_found"


async def test_start_delayed_past_its_blind_cancel_is_refused_over_http(env):
    cancelled = await env.client.post("/v1/jobs/job-late0001/cancel", headers=AUTH)
    assert cancelled.status == 404
    late = await env.client.post("/v1/jobs", json=body("job-late0001"), headers=AUTH)
    assert late.status == 409
    assert (await late.json())["error"]["code"] == "duplicate_job"
    assert env.link.connects == 0


async def test_failed_job_reports_error_and_never_retries(env):
    env.gatt.fail_send_after_commands = 2
    await env.client.post("/v1/jobs", json=body(), headers=AUTH)
    view = wire.parse_job_view(await wait_terminal(env, "job-000001"))
    assert view["state"] == "failed" and view["error"]["code"] == "write_failed"
    assert view["delivery_uncertain"] is True and view["writes"] == 2
    await asyncio.sleep(0.05)
    assert env.link.connects == 1 and len(env.gatt.commands) == 2


async def test_no_secret_payload_or_address_appears_in_bodies_or_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = []
    bitmap = bytes([0xA5]) * 96
    marker = base64.b64encode(bitmap).decode()
    for method, path, kwargs in [
        ("POST", "/v1/jobs", dict(json=body(bitmap=bitmap))),
        ("POST", "/v1/jobs", dict(json=body("job-000002", bitmap=bitmap))),
        ("GET", "/v1/status", {}),
        ("GET", "/v1/jobs/job-000001", {}),
        ("POST", "/v1/jobs", dict(json={**body("job-000003"), "bitmap_b64": marker + "!"})),
        ("GET", "/nope", {}),
    ]:
        response = await env.client.request(method, path, headers=AUTH, **kwargs)
        bodies.append(await response.text())
    await wait_terminal(env, "job-000001")
    await env.client.get("/v1/status", headers=bearer("nope"))
    haystack = "\n".join(bodies + [r.getMessage() for r in caplog.records])
    for secret in (TOKEN, marker, PRINTER_MAC, ADAPTER_MAC, "Bearer"):
        assert secret not in haystack
    assert "job-000001" in haystack


# -- serve() lifecycle ---------------------------------------------------------------------------


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_serve_runs_and_shuts_down_cancelling_the_active_job():
    gatt = fake.FakeGattServer()
    gatt.on_command = lambda srv, h, v: (
        srv.push_notification(srv.notify_handle, fake.PAUSE) if len(srv.commands) == 1 else None
    )
    link = fake.FakeLink(gatt)
    port = free_port()
    config = bridge_config.BridgeConfig(PRINTER_MAC, ADAPTER_MAC, TOKEN, "127.0.0.1", port)
    stop = asyncio.Event()
    manager = jobs.JobManager(
        link, cancel_wait=2, pacing=FAST | dict(pause_timeout=30, chunk_delay=0.002)
    )
    task = asyncio.create_task(server.serve(config, manager=manager, stop=stop))
    async with ClientSession() as session:
        url = f"http://127.0.0.1:{port}"
        for _ in range(200):
            try:
                async with session.get(f"{url}/v1/status", headers=AUTH) as response:
                    assert response.status == 200
                    break
            except OSError:
                await asyncio.sleep(0.01)
        async with session.post(f"{url}/v1/jobs", json=body(), headers=AUTH) as response:
            assert response.status == 202
        for _ in range(200):
            if gatt.commands:
                break
            await asyncio.sleep(0.005)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert gatt.closed and not manager.busy


async def test_wildcard_bind_is_logged_as_warning(caplog):
    caplog.set_level(logging.WARNING)
    config = bridge_config.BridgeConfig(PRINTER_MAC, ADAPTER_MAC, TOKEN, "0.0.0.0", free_port())
    stop = asyncio.Event()
    manager = jobs.JobManager(fake.FakeLink(fake.FakeGattServer()))
    task = asyncio.create_task(server.serve(config, manager=manager, stop=stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert any("all interfaces" in r.getMessage() for r in caplog.records)
    assert TOKEN not in caplog.text
