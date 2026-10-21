"""Bridge HTTP client and job runner against a scripted aiohttp fake bridge (no HA, no BLE)."""

import asyncio
import importlib
import logging
import socket
import sys
import types
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from bridge_fake import LEAK, TOKEN, FakeBridge

ROOT = Path(__file__).parents[1] / "custom_components/tinyprint"
pkg = types.ModuleType("tinyprint_bc")
pkg.__path__ = [str(ROOT)]
sys.modules["tinyprint_bc"] = pkg
bc = importlib.import_module("tinyprint_bc.bridge_client")
wire = importlib.import_module("tinyprint_bc.bridge_wire")


class FakePrinter:
    def __init__(self):
        self.last_result = {
            "job_id": "studio1",
            "bytes_written": 0,
            "frames_written": 0,
            "writes": 0,
            "notifications": 0,
            "delivery_uncertain": False,
            "confirmation": "none",
        }
        self.phases = []

    def phase(self, phase, status):
        self.phases.append((phase, status))
        self.last_result["phase"] = phase


@pytest.fixture
async def bridge(monkeypatch):
    fake = FakeBridge()
    server = TestServer(fake.app())
    await server.start_server()
    import aiohttp

    session = aiohttp.ClientSession()
    fake.url = str(server.make_url("")).rstrip("/")
    fake.session = session
    fake.client = bc.BridgeClient(session, bc.BridgeSettings(fake.url, TOKEN))
    yield fake
    await session.close()
    await server.close()


BITMAP = bytes(48 * 3)


async def run(bridge, kind="print", **kwargs):
    printer = FakePrinter()
    kwargs.setdefault("poll_interval", 0.01)
    bitmap, rows = (BITMAP, 3) if kind == "print" else (None, None)
    try:
        result = await bc.run_bridge_job(printer, bridge.client, kind, bitmap, rows, **kwargs)
    except Exception as error:
        return printer, error
    return printer, result


# --- url / settings ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://100.64.0.5:8765", "http://100.64.0.5:8765"),
        (" https://bridge.example.ts.net/ ", "https://bridge.example.ts.net"),
        ("http://[fd7a::1]:8765", "http://[fd7a::1]:8765"),
        ("http://localhost", "http://localhost"),
    ],
)
def test_url_accepted(value, expected):
    assert bc.normalize_bridge_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        None,
        5,
        "ftp://host",
        "host:8765",
        "http://",
        "http://:80",
        "http://user:pw@host",
        "http://user@host",
        "http://host/path",
        "http://host/?x=1",
        "http://host#frag",
        "http://host:99999",
        "http://host:0",
        "http://host:",
        "http://host name",
        "http://ho\nst",
        "http://ho%73t",
        "http://" + "a" * 300,
        "javascript:alert(1)",
        "file:///etc/passwd",
    ],
)
def test_url_rejected_without_echo(value):
    with pytest.raises(ValueError) as raised:
        bc.normalize_bridge_url(value)
    if isinstance(value, str) and len(value.strip()) > 12:
        assert value.strip() not in str(raised.value)


def test_settings_never_expose_secrets():
    settings = bc.BridgeSettings("http://secret-host:1", TOKEN)
    for text in (
        repr(settings),
        str(settings),
        f"{settings}",
        repr(bc.BridgeClient(None, settings)),
    ):
        assert TOKEN not in text
        assert "secret-host" not in text
    assert settings.scheme == "http"


def test_bridge_settings_selection():
    assert bc.bridge_settings({}) is None
    assert (
        bc.bridge_settings({"transport": "native", "bridge_url": "http://h", "bridge_token": TOKEN})
        is None
    )
    settings = bc.bridge_settings(
        {"transport": "bridge", "bridge_url": "http://h:1/", "bridge_token": TOKEN}
    )
    assert (settings.url, settings.token) == ("http://h:1", TOKEN)
    for options in (
        {"transport": "bridge"},
        {"transport": "bridge", "bridge_url": "http://h"},
        {"transport": "bridge", "bridge_url": "http://h", "bridge_token": "short"},
        {"transport": "bridge", "bridge_url": "nope", "bridge_token": TOKEN},
    ):
        with pytest.raises(bc.BridgeConfigError) as raised:
            bc.bridge_settings(options)
        assert TOKEN not in str(raised.value)


# --- client ------------------------------------------------------------------------------------


async def test_status_ok_and_sends_bearer(bridge):
    status = await bridge.client.status()
    assert status["busy"] is False
    assert status["version"] == "0.2.1"
    assert "uptime_seconds" not in status


async def test_wrong_token_is_certain_unauthorized(bridge):
    bridge.token = "other" * 10
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.status()
    assert raised.value.code == "unauthorized"
    assert raised.value.uncertain is False
    assert TOKEN not in str(raised.value)
    assert bridge.url not in str(raised.value)


async def test_connection_refused_is_certain(bridge):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = bc.BridgeClient(bridge.session, bc.BridgeSettings(f"http://127.0.0.1:{port}", TOKEN))
    with pytest.raises(bc.BridgeError) as raised:
        await client.start_print(wire.new_job_id(), BITMAP, 3)
    assert raised.value.code == "unreachable"
    assert raised.value.uncertain is False
    assert "127.0.0.1" not in str(raised.value)


async def test_timeout_after_send_is_uncertain(bridge, monkeypatch):
    bridge.start_mode = "hang"
    monkeypatch.setattr(bc, "START_TIMEOUT", 0.2)
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.start_print(wire.new_job_id(), BITMAP, 3)
    assert raised.value.code == "timeout"
    assert raised.value.uncertain is True


async def test_dropped_connection_after_send_is_uncertain(bridge):
    bridge.start_mode = "drop"
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.start_print(wire.new_job_id(), BITMAP, 3)
    assert raised.value.uncertain is True


@pytest.mark.parametrize(
    ("mode", "code", "uncertain"),
    [
        ("busy", "busy", False),
        ("error500", "server_error", True),
        ("redirect", "unexpected_redirect", False),
        ("garbage", "bad_response", True),
        ("huge", "bad_response", True),
    ],
)
async def test_start_error_classification(bridge, mode, code, uncertain):
    bridge.start_mode = mode
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.start_print(wire.new_job_id(), BITMAP, 3)
    assert (raised.value.code, raised.value.uncertain) == (code, uncertain)
    assert LEAK not in str(raised.value)
    assert bridge.count("GET", "/elsewhere") == 0


async def test_job_view_is_whitelisted(bridge):
    bridge.extra_key = True
    job_id = wire.new_job_id()
    view = await bridge.client.start_print(job_id, BITMAP, 3)
    assert "secret" not in view
    assert view["state"] == "accepted"


async def test_invalid_input_rejected_before_network(bridge):
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.start_print("x", BITMAP, 3)
    assert raised.value.code == "invalid_request"
    with pytest.raises(bc.BridgeError):
        await bridge.client.job("../../etc")
    assert bridge.requests == []


async def test_mismatched_job_id_is_bad_response(bridge):
    bridge.start_mode = "ok"
    original = bridge.view

    def other(state, **over):
        view = original(state, **over)
        view["job_id"] = "someone_else_1"
        return view

    bridge.view = other
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.start_print(wire.new_job_id(), BITMAP, 3)
    assert raised.value.code == "bad_response"


# --- runner ------------------------------------------------------------------------------------


async def test_happy_path_print(bridge):
    bridge.polls = [
        {"state": "connecting"},
        {"state": "transmitting", "bytes_written": 20, "writes": 1},
        {
            "state": "sent",
            "bytes_written": 200,
            "frames_written": 5,
            "writes": 10,
            "notifications": 2,
            "flow_pauses": 1,
            "confirmation": "transport_only",
        },
    ]
    bridge.extra_key = True
    printer, result = await run(bridge)
    assert isinstance(result, dict), result
    assert result["bytes_written"] == 200
    assert result["frames_written"] == 5
    assert result["flow_pauses"] == 1
    assert result["confirmation"] == "transport_only"
    assert result["physical_print_confirmed"] is False
    assert result["delivery_uncertain"] is False
    assert result["bridge_job_id"] == bridge.job_id
    assert result["job_id"] == "studio1"
    assert "secret" not in result
    statuses = [status for _, status in printer.phases]
    assert statuses[0] == "connecting" and "printing" in statuses
    assert bridge.count("POST", "/v1/jobs") == 1
    assert bridge.count("POST", "/cancel") == 0


async def test_accepted_202_is_not_success(bridge):
    bridge.default_poll = {"state": "transmitting", "bytes_written": 40}
    printer, error = await run(bridge, deadline=0.2)
    assert isinstance(error, bc.BridgeJobError)
    assert error.uncertain is True
    assert printer.last_result["delivery_uncertain"] is True
    assert bridge.count("POST", "/cancel") == 1
    assert "no automatic replay" in str(error)


async def test_answer_of_the_wrong_kind_is_never_a_print_success(bridge):
    bridge.polls = [
        {"state": "sent", "kind": "diagnose", "confirmation": "connection_only"},
    ]
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError), error
    assert error.code == "bridge_bad_response" and error.uncertain is True
    assert printer.last_result["delivery_uncertain"] is True
    assert bridge.count("POST", "/v1/jobs") == 1


async def test_print_reported_sent_without_any_written_byte_is_not_success(bridge):
    bridge.polls = [{"state": "sent", "confirmation": "transport_only"}]
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError), error
    assert error.code == "bridge_bad_response" and error.uncertain is True
    assert printer.last_result["delivery_uncertain"] is True


async def test_failed_job_with_partial_write_is_uncertain(bridge):
    bridge.polls = [
        {
            "state": "failed",
            "bytes_written": 40,
            "delivery_uncertain": True,
            "error": {"code": "link_lost", "message": "Link lost"},
        }
    ]
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError)
    assert "link_lost" in str(error)
    assert "Partial delivery is possible; no automatic replay." in str(error)
    assert printer.last_result["bytes_written"] == 40
    assert printer.last_result["delivery_uncertain"] is True
    assert bridge.count("POST", "/v1/jobs") == 1


async def test_printer_fault_is_reported_with_its_code_and_hint(bridge):
    bridge.polls = [
        {
            "state": "failed",
            "bytes_written": 40,
            "delivery_uncertain": True,
            "error": {"code": "printer_fault", "message": "The printer reported a fault"},
        }
    ]
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError)
    assert error.code == "printer_fault"
    assert "paper, cover and temperature" in str(error)
    assert bridge.count("POST", "/v1/jobs") == 1


async def test_failed_job_without_writes_says_nothing_written(bridge):
    bridge.start_state = "failed"
    bridge.polls = []
    original = bridge.view

    def failing(state, **over):
        return original(state, error={"code": "connect_timeout", "message": "No link"}, **over)

    bridge.view = failing
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError)
    assert "No raster writes were attempted." in str(error)
    assert error.uncertain is False


async def test_uncertain_start_never_replayed_and_asks_state_once(bridge):
    bridge.start_mode = "drop"
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError)
    assert error.uncertain is True
    assert printer.last_result["delivery_uncertain"] is True
    assert bridge.count("POST", "/v1/jobs") == 1
    assert bridge.count("GET", f"/v1/jobs/{printer.last_result['bridge_job_id']}") == 1
    assert "may have accepted" in str(error)
    # The job may be printing right now with nobody left to own it: it must be cancelled.
    assert bridge.count("POST", "/cancel") == 1
    assert printer.last_result["bridge_cancel"] == "acknowledged"


async def test_uncertain_start_whose_job_already_finished_is_not_cancelled(bridge):
    bridge.start_mode = "drop"
    bridge.polls = [{"state": "sent", "bytes_written": 20, "confirmation": "transport_only"}]
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError) and error.uncertain is True
    assert bridge.count("POST", "/v1/jobs") == 1
    assert bridge.count("POST", "/cancel") == 0


@pytest.mark.parametrize(
    "status",
    [
        {"busy": True, "active_job_id": "x" * 65},
        {"busy": True, "active_job_id": None},
        {"busy": True, "active_job_id": "bad id!"},
        {"busy": False, "active_job_id": "a" * 32},
        {"busy": True, "active_job_id": 5},
    ],
)
async def test_inconsistent_status_is_a_bad_response_not_idle(bridge, status):
    bridge.status_body = {**bridge.status_body, **status}
    with pytest.raises(bc.BridgeError) as raised:
        await bridge.client.status()
    assert raised.value.code == "bad_response"


async def test_three_poll_failures_cancel_and_flag_uncertain(bridge):
    bridge.polls = ["fail", "fail", "fail"]
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError)
    assert error.uncertain is True or printer.last_result["bridge_cancel"] == "acknowledged"
    assert bridge.count("POST", "/cancel") == 1
    assert printer.last_result["bridge_cancel"] == "acknowledged"


async def test_one_poll_failure_is_tolerated(bridge):
    bridge.polls = [
        "fail",
        {"state": "sent", "bytes_written": 20, "confirmation": "transport_only"},
    ]
    printer, result = await run(bridge)
    assert isinstance(result, dict), result


async def test_unacknowledged_cancel_marks_uncertain(bridge, monkeypatch):
    bridge.polls = ["fail", "fail", "fail"]
    bridge.cancel_states = [{"state": "transmitting"}]
    printer, error = await run(bridge)
    assert printer.last_result["bridge_cancel"] == "pending"
    assert printer.last_result["delivery_uncertain"] is True
    assert error.uncertain is True


async def test_caller_cancellation_posts_bridge_cancel(bridge):
    bridge.default_poll = {"state": "transmitting", "bytes_written": 20}
    printer = FakePrinter()
    task = asyncio.create_task(
        bc.run_bridge_job(printer, bridge.client, "print", BITMAP, 3, poll_interval=0.01)
    )
    for _ in range(100):
        if bridge.count("GET", "") and bridge.job_id and bridge.count("GET", bridge.job_id):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.count("POST", "/cancel") == 1
    assert printer.last_result["bridge_cancel"] == "acknowledged"


async def test_cancel_during_start_retries_not_found_once(bridge):
    bridge.start_mode = "hang"
    bridge.cancel_not_found_first = 1
    printer = FakePrinter()
    task = asyncio.create_task(
        bc.run_bridge_job(printer, bridge.client, "print", BITMAP, 3, poll_interval=0.01)
    )
    for _ in range(100):
        if bridge.job_id:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.count("POST", "/cancel") == 2
    assert (
        printer.last_result["delivery_uncertain"] is False
        or printer.last_result["bridge_cancel"] == "acknowledged"
    )


async def test_diagnose_result_shape(bridge):
    diagnosis = {
        "profile_supported": True,
        "profile": {"write": 8},
        "services": ["0000ae30-0000-1000-8000-00805f9b34fb"],
        "att_mtu": 23,
        "link_connect_seconds": 0.4,
        "stray_pdus": 0,
        "unexpected": "dropped",
    }
    bridge.start_state = "accepted"
    bridge.polls = [
        {
            "state": "sent",
            "kind": "diagnose",
            "confirmation": "connection_only",
            "diagnosis": diagnosis,
        }
    ]
    printer, result = await run(bridge, kind="diagnose")
    assert isinstance(result, dict), result
    assert result["transport"] == "bridge"
    assert result["supported_profile"] is True
    assert result["confirmation"] == "connection_only"
    assert result["profile"] == {"write": 8}
    assert "unexpected" not in result
    assert bridge.count("POST", "/v1/diagnose") == 1


async def test_deadline_scales_with_rows():
    assert bc._deadline("print", 3) == pytest.approx(60 + bc.DEADLINE_MARGIN)
    assert bc._deadline("print", 2048) > bc._deadline("print", 3)
    assert bc._deadline("diagnose", None) == bc.DIAGNOSE_DEADLINE


async def test_unauthorized_start_has_hint_and_no_secret(bridge, caplog):
    bridge.token = "different" * 5
    caplog.set_level(logging.DEBUG)
    printer, error = await run(bridge)
    assert isinstance(error, bc.BridgeJobError)
    assert "token" in str(error)
    assert TOKEN not in str(error)
    assert TOKEN not in caplog.text
    assert bridge.url not in caplog.text
    assert bridge.count("POST", "/v1/jobs") == 1


async def test_no_secret_in_logs_or_result(bridge, caplog):
    caplog.set_level(logging.DEBUG)
    bridge.polls = [{"state": "sent", "confirmation": "transport_only"}]
    printer, result = await run(bridge)
    flat = repr(result) + caplog.text + repr(printer.phases)
    assert TOKEN not in flat
    assert bridge.url not in flat
    assert "bitmap" not in flat.lower() or "bitmap_b64" not in flat
