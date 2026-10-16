"""Local LE regression tests: real ATT codec, in-memory peer, never actual Bluetooth."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from att_fake import FakeGattServer, FakeLink
from shared_loader import load

ADAPTER = "00:11:22:33:44:55"
PRINTER = "AA:BB:CC:DD:EE:FF"


def printer(adapter=ADAPTER):
    p = SimpleNamespace(address=PRINTER, adapter_mac=adapter, last_result={}, phases=[])

    def phase(name, status):
        p.phases.append(name)
        p.last_result["phase"] = name
        p.status = status

    p.phase = phase
    return p


def test_local_mode_is_not_a_misconfigured_remote_bridge():
    assert (
        load("bridge_client").bridge_settings({"transport": "local_le", "adapter_mac": ADAPTER})
        is None
    )


@pytest.mark.asyncio
async def test_print_uses_real_att_and_closes_without_http():
    local = load("local_le")
    peer = FakeGattServer(first_handle=20, gap=1)
    link = FakeLink(peer)
    p = printer()
    result = await local.run_local_job(
        p,
        "print",
        bytes([0xA5]) * 96,
        2,
        link=link,
        pacing={"chunk_delay": 0, "row_delay": 0, "settle_delay": 0},
    )
    assert result["confirmation"] == "transport_only"
    assert result["physical_print_confirmed"] is False
    assert result["bytes_written"] > 0 and result["frames_written"] > 0
    assert peer.closed and link.connects == 1
    assert "connecting_le" in p.phases and "transmitting" in p.phases
    assert all(handle == peer.write_handle for handle, _ in peer.commands)


@pytest.mark.asyncio
async def test_diagnose_discovers_real_services_without_raster():
    local = load("local_le")
    peer = FakeGattServer(first_handle=17)
    result = await local.run_local_job(printer(), "diagnose", link=FakeLink(peer))
    assert result["supported_profile"] is True
    assert result["confirmation"] == "connection_only"
    assert result["bytes_written"] == 0 and not peer.commands
    assert len(result["services"]) == 2 and peer.closed


@pytest.mark.asyncio
async def test_discovery_timeout_closes_channel_and_never_prints():
    local = load("local_le")
    peer = FakeGattServer()
    peer.silent.add(0x10)
    p = printer()
    with pytest.raises(TimeoutError):
        await local.run_local_job(
            p, "print", bytes(48), 1, link=FakeLink(peer), request_timeout=0.02
        )
    assert peer.closed and not peer.commands
    assert p.last_result["bytes_written"] == 0
    assert p.last_result["phase"] == "discovering_gatt"


@pytest.mark.asyncio
async def test_partial_failure_keeps_counters_and_does_not_replay():
    local = load("local_le")
    peer = FakeGattServer()
    peer.fail_send_after_commands = 3
    link = FakeLink(peer)
    p = printer()
    with pytest.raises(ConnectionError):
        await local.run_local_job(
            p,
            "print",
            bytes(96),
            2,
            link=link,
            pacing={"chunk_delay": 0, "row_delay": 0, "settle_delay": 0},
        )
    assert p.last_result["bytes_written"] > 0
    assert p.last_result["delivery_uncertain"] is True
    assert link.connects == 1 and peer.closed


@pytest.mark.asyncio
async def test_cancel_discovery_closes_socket_and_reader():
    local = load("local_le")
    peer = FakeGattServer()
    peer.silent.add(0x10)
    p = printer()
    task = asyncio.create_task(local.run_local_job(p, "diagnose", link=FakeLink(peer)))
    for _ in range(100):
        if peer.requests:
            break
        await asyncio.sleep(0.001)
    assert peer.requests
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert peer.closed and not peer.commands
    assert p.last_result["delivery_uncertain"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("address", [None, "", "bad", "00:00:00:00:00:00", "gg:11:22:33:44:55"])
async def test_invalid_adapter_fails_before_connect(address):
    local = load("local_le")
    peer = FakeGattServer()
    link = FakeLink(peer)
    with pytest.raises(ValueError):
        await local.run_local_job(printer(address), "diagnose", link=link)
    assert link.connects == 0 and not peer.received


@pytest.mark.asyncio
async def test_invalid_geometry_fails_before_connect():
    local = load("local_le")
    link = FakeLink(FakeGattServer())
    with pytest.raises(ValueError):
        await local.run_local_job(printer(), "print", bytes(47), 1, link=link)
    assert link.connects == 0


def test_bridge_and_integration_reuse_the_same_link_module():
    local_link = load("le_link")
    bridge_link = importlib.import_module("tinyprint_bridge.link")
    assert bridge_link.L2capLeLink is local_link.L2capLeLink


def test_local_kernel_transport_is_packaged_inside_ha_integration():
    from shared_loader import SHARED_DIR

    assert (SHARED_DIR / "local_le.py").is_file(), (
        "HA has no local LE runtime; only remote bridge exists"
    )
    assert (SHARED_DIR / "le_link.py").is_file(), "Kernel link must ship inside the integration"


@pytest.mark.asyncio
async def test_repeated_cancel_during_cleanup_keeps_wait_bounded_and_socket_closed():
    local = load("local_le")
    entered, release = asyncio.Event(), asyncio.Event()
    peer = FakeGattServer()

    class SlowClient:
        async def aclose(self):
            entered.set()
            await release.wait()

    task = asyncio.create_task(local._close(SlowClient(), peer))
    await asyncio.wait_for(entered.wait(), 1)
    assert peer.closed
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_stuck_reader_cleanup_has_deadline():
    local = load("local_le")
    peer = FakeGattServer()

    class StuckClient:
        async def aclose(self):
            await asyncio.Event().wait()

    with patch.object(local, "CLOSE_TIMEOUT", 0.02):
        await asyncio.wait_for(local._close(StuckClient(), peer), 0.5)
    assert peer.closed


@pytest.mark.asyncio
async def test_cancel_during_final_cleanup_retains_delivery_uncertainty():
    local = load("local_le")
    peer = FakeGattServer()
    p = printer()

    async def cancelled_close(client, channel):
        channel.close()
        await client.aclose()
        raise asyncio.CancelledError

    with patch.object(local, "_close", cancelled_close):
        with pytest.raises(asyncio.CancelledError):
            await local.run_local_job(
                p,
                "print",
                bytes(48),
                1,
                link=FakeLink(peer),
                pacing={"chunk_delay": 0, "row_delay": 0, "settle_delay": 0},
            )
    assert p.last_result["bytes_written"] > 0
    assert p.last_result["delivery_uncertain"] is True
    assert p.last_result["confirmation"] == "none"
