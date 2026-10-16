"""Job manager: one hardware job at a time, bounded memory, honest outcomes, no replay.

Runs against att_fake.FakeGattServer through a fake link; nothing touches Bluetooth.
"""

import asyncio
import json
import logging

import att_fake as fake
import pytest
from shared_loader import load
from tinyprint_bridge import jobs

wire = load("bridge_wire")
protocol = load("protocol")

FAST = dict(chunk_delay=0, row_delay=0, settle_delay=0, pause_timeout=0.5)
BITMAP = bytes(range(48)) * 2


def manager_for(link, **overrides):
    options = dict(
        max_retained=32,
        max_seen_ids=4096,
        cancel_wait=2,
        connect_timeout=0.5,
        discovery_timeout=1,
        request_timeout=0.3,
        send_timeout=0.3,
        pacing=FAST,
    )
    options.update(overrides)
    return jobs.JobManager(link, **options)


def print_request(job_id="job-000001", bitmap=BITMAP):
    return wire.PrintRequest(job_id, len(bitmap) // 48, bitmap)


async def settle(manager, job_id, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        view = manager.get(job_id)
        if view["terminal"]:
            return view
        await asyncio.sleep(0.005)
    raise AssertionError(f"job did not finish: {manager.get(job_id)['state']}")


def pause_on_first_command(server):
    server.on_command = lambda srv, handle, value: (
        srv.push_notification(srv.notify_handle, fake.PAUSE) if len(srv.commands) == 1 else None
    )


async def test_print_runs_phases_and_reports_transport_only_success():
    server = fake.FakeGattServer()
    link = fake.FakeLink(server)
    manager = manager_for(link)
    started = manager.start_print(print_request())
    assert started["state"] == "accepted" and not started["terminal"]
    assert manager.busy and manager.active_job_id == "job-000001"
    view = await settle(manager, "job-000001")
    expected = b"".join(protocol.print_frames(BITMAP, 2))
    assert view["state"] == "sent" and view["terminal"] and view["error"] is None
    assert view["confirmation"] == "transport_only"
    assert view["physical_print_confirmed"] is False
    assert view["bytes_written"] == len(expected)
    assert b"".join(v for _h, v in server.commands) == expected
    assert view["frames_written"] == 7 and view["writes"] == len(server.commands)
    assert view["delivery_uncertain"] is False
    assert link.connects == 1 and server.closed and not manager.busy
    wire.parse_job_view(view)  # the HA client accepts exactly this shape


async def test_diagnose_reports_profile_without_printing():
    server = fake.FakeGattServer(first_handle=11, gap=1)
    manager = manager_for(fake.FakeLink(server))
    manager.start_diagnose("diag-000001")
    view = await settle(manager, "diag-000001")
    assert view["kind"] == "diagnose" and view["state"] == "sent"
    assert view["confirmation"] == "connection_only" and view["bytes_written"] == 0
    diagnosis = view["diagnosis"]
    assert diagnosis["profile_supported"] is True and diagnosis["att_mtu"] == 23
    assert diagnosis["profile"]["cccd_handle"] == server.cccd_handle
    assert isinstance(diagnosis["link_connect_seconds"], float)
    assert diagnosis["stray_pdus"] == 0
    assert server.commands == [] and server.closed
    assert len(json.dumps(diagnosis)) <= wire.MAX_DIAGNOSIS_JSON
    wire.parse_job_view(view)


async def test_diagnose_on_wrong_device_reports_unsupported_not_crash():
    services = [fake.Service("0000ae40" + fake.BASE_SUFFIX, [fake.Char(fake.WRITE, 0x04)])]
    manager = manager_for(fake.FakeLink(fake.FakeGattServer(services)))
    manager.start_diagnose("diag-000002")
    view = await settle(manager, "diag-000002")
    assert view["state"] == "sent"
    assert view["diagnosis"]["profile_supported"] is False
    assert "AE30" in view["diagnosis"]["profile_problem"]


async def test_second_job_is_rejected_busy_without_touching_the_radio():
    link = fake.FakeLink(fake.FakeGattServer(), connect_delay=0.2)
    manager = manager_for(link)
    manager.start_print(print_request("job-000001"))
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-000002"))
    assert (caught.value.code, caught.value.status) == ("busy", 409)
    with pytest.raises(wire.WireError) as caught:
        manager.start_diagnose("diag-000003")
    assert caught.value.code == "busy"
    await settle(manager, "job-000001")
    assert link.connects == 1
    with pytest.raises(wire.WireError) as caught:
        manager.get("job-000002")
    assert caught.value.code == "not_found"


async def test_duplicate_ids_rejected_active_finished_and_after_result_eviction():
    link = fake.FakeLink(fake.FakeGattServer(), connect_delay=0.05)
    manager = manager_for(link, max_retained=1, max_seen_ids=3)
    manager.start_print(print_request("job-000001"))
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-000001"))
    assert (caught.value.code, caught.value.status) == ("duplicate_job", 409)
    await settle(manager, "job-000001")
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-000001"))
    assert caught.value.code == "duplicate_job"
    for name in ("job-000002", "job-000003"):
        link.server = fake.FakeGattServer()
        manager.start_print(print_request(name))
        await settle(manager, name)
    # Result of the first job is evicted (max_retained=1) yet the id is still remembered.
    with pytest.raises(wire.WireError) as caught:
        manager.get("job-000001")
    assert caught.value.code == "not_found"
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-000001"))
    assert caught.value.code == "duplicate_job"
    # Seen-id memory is bounded too: once pushed out an id is accepted again.
    link.server = fake.FakeGattServer()
    manager.start_print(print_request("job-000004"))
    await settle(manager, "job-000004")
    link.server = fake.FakeGattServer()
    manager.start_print(print_request("job-000001"))
    await settle(manager, "job-000001")


async def test_retained_results_are_bounded():
    link = fake.FakeLink(fake.FakeGattServer())
    manager = manager_for(link, max_retained=2)
    for index in range(5):
        link.server = fake.FakeGattServer()
        name = f"job-00000{index}"
        manager.start_print(print_request(name))
        await settle(manager, name)
    assert manager.status()["retained_jobs"] == 2
    manager.get("job-000004")
    manager.get("job-000003")
    with pytest.raises(wire.WireError):
        manager.get("job-000000")


async def test_cancel_mid_transmit_waits_for_socket_cleanup_before_returning():
    server = fake.FakeGattServer()
    pause_on_first_command(server)
    manager = manager_for(
        fake.FakeLink(server), pacing=FAST | dict(pause_timeout=30, chunk_delay=0.002)
    )
    manager.start_print(print_request())
    for _ in range(200):
        if manager.get("job-000001")["state"] == "transmitting" and server.commands:
            break
        await asyncio.sleep(0.005)
    assert manager.busy
    view = await manager.cancel("job-000001")
    assert server.closed  # the socket was released before cancel answered
    assert not manager.busy  # and so was the hardware slot
    assert view["state"] == "cancelled" and view["terminal"]
    assert view["error"]["code"] == "cancelled"
    assert view["bytes_written"] > 0 and view["delivery_uncertain"] is True
    assert view["confirmation"] == "none" and view["physical_print_confirmed"] is False
    sent = len(server.commands)
    await asyncio.sleep(0.05)
    assert len(server.commands) == sent  # nothing written after cancel
    again = await manager.cancel("job-000001")
    assert again == view


async def test_cancel_unknown_job_is_not_found():
    manager = manager_for(fake.FakeLink(fake.FakeGattServer()))
    with pytest.raises(wire.WireError) as caught:
        await manager.cancel("job-unknown1")
    assert (caught.value.code, caught.value.status) == ("not_found", 404)


async def test_a_start_that_arrives_after_its_cancel_is_refused_not_run():
    # A start request delayed on the network can land after the client already cancelled it.
    link = fake.FakeLink(fake.FakeGattServer())
    manager = manager_for(link)
    with pytest.raises(wire.WireError) as cancelled:
        await manager.cancel("job-late0001")
    assert (cancelled.value.code, cancelled.value.status) == ("not_found", 404)
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-late0001"))
    assert caught.value.code == "duplicate_job"
    assert link.connects == 0 and not manager.busy


async def test_blind_cancels_never_evict_the_memory_of_real_jobs():
    link = fake.FakeLink(fake.FakeGattServer())
    manager = manager_for(link, max_seen_ids=2)
    manager.start_print(print_request("job-real0001"))
    await settle(manager, "job-real0001")
    for number in range(50):
        with pytest.raises(wire.WireError):
            await manager.cancel(f"job-blind{number:04d}")
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-real0001"))
    assert caught.value.code == "duplicate_job"


async def test_cancel_finished_job_returns_final_view_unchanged():
    manager = manager_for(fake.FakeLink(fake.FakeGattServer()))
    manager.start_print(print_request())
    final = await settle(manager, "job-000001")
    assert await manager.cancel("job-000001") == final


async def test_cancel_during_connect_releases_slot_and_closes_nothing_twice():
    link = fake.FakeLink(fake.FakeGattServer(), connect_delay=5)
    manager = manager_for(link, connect_timeout=10)
    manager.start_print(print_request())
    await asyncio.sleep(0.02)
    assert manager.get("job-000001")["state"] == "connecting"
    view = await manager.cancel("job-000001")
    assert view["state"] == "cancelled" and view["bytes_written"] == 0
    assert view["delivery_uncertain"] is False
    assert not manager.busy


async def test_cancel_before_the_worker_starts_never_touches_the_link():
    link = fake.FakeLink(fake.FakeGattServer())
    manager = manager_for(link)
    manager.start_print(print_request())
    view = await manager.cancel("job-000001")
    assert view["state"] == "cancelled" and link.connects == 0 and not manager.busy


async def test_cancel_that_cannot_finish_in_time_reports_non_terminal_and_keeps_lock():
    class Stubborn(fake.FakeLink):
        async def connect(self):
            self.connects += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)  # cleanup that outlasts the cancel bound
                raise

    manager = manager_for(Stubborn(fake.FakeGattServer()), cancel_wait=0.05, connect_timeout=10)
    manager.start_print(print_request())
    await asyncio.sleep(0.02)
    view = await manager.cancel("job-000001")
    assert view["terminal"] is False
    assert manager.busy  # hardware slot stays held until the worker really ends
    with pytest.raises(wire.WireError) as caught:
        manager.start_print(print_request("job-000002"))
    assert caught.value.code == "busy"
    final = await settle(manager, "job-000001")
    assert final["state"] == "cancelled" and not manager.busy


async def test_partial_write_failure_keeps_counters_marks_uncertain_and_never_retries():
    server = fake.FakeGattServer()
    server.fail_send_after_commands = 3
    link = fake.FakeLink(server)
    manager = manager_for(link)
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["state"] == "failed" and view["error"]["code"] == "write_failed"
    assert view["writes"] == 3 and view["bytes_written"] == sum(len(v) for _h, v in server.commands)
    assert view["delivery_uncertain"] is True and view["confirmation"] == "none"
    await asyncio.sleep(0.05)
    assert link.connects == 1 and len(server.commands) == 3 and server.closed


async def test_link_loss_mid_job_is_link_lost_and_uncertain():
    server = fake.FakeGattServer()
    server.on_command = lambda srv, h, v: srv.drop_link() if len(srv.commands) == 2 else None
    manager = manager_for(fake.FakeLink(server), pacing=FAST | dict(chunk_delay=0.002))
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["state"] == "failed" and view["error"]["code"] == "link_lost"
    assert view["delivery_uncertain"] is True and view["bytes_written"] > 0


async def test_connect_timeout_and_failure_are_mapped_without_leaking_details():
    slow = fake.FakeLink(fake.FakeGattServer(), connect_delay=5)
    manager = manager_for(slow, connect_timeout=0.05)
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] == "connect_timeout" and view["bytes_written"] == 0
    assert view["delivery_uncertain"] is False

    broken = fake.FakeLink(
        fake.FakeGattServer(), connect_error=OSError(113, "no route to AA:BB:CC:DD:EE:FF")
    )
    manager = manager_for(broken)
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] == "connect_failed"
    assert "AA:BB" not in json.dumps(view) and "route" not in json.dumps(view)
    assert broken.connects == 1


async def test_unexpected_connect_exception_is_internal_error():
    link = fake.FakeLink(fake.FakeGattServer(), connect_error=RuntimeError("secret detail"))
    manager = manager_for(link)
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] in {"connect_failed", "internal_error"}
    assert "secret" not in json.dumps(view)


async def test_profile_mismatch_fails_before_any_write():
    services = [fake.Service(fake.SERVICE, [fake.Char(fake.WRITE, 0x04)])]
    server = fake.FakeGattServer(services)
    manager = manager_for(fake.FakeLink(server))
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] == "profile_unsupported"
    assert server.commands == [] and view["bytes_written"] == 0 and server.closed
    assert view["delivery_uncertain"] is False


async def test_malformed_discovery_is_att_protocol_error():
    server = fake.FakeGattServer()
    server.mutators[0x10] = lambda responses: [b"\x11\x00" + bytes(6)]
    manager = manager_for(fake.FakeLink(server))
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] == "att_protocol_error" and server.closed


async def test_unanswered_discovery_times_out_and_closes():
    server = fake.FakeGattServer()
    server.silent.add(0x10)
    manager = manager_for(fake.FakeLink(server), request_timeout=0.05)
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["state"] == "failed" and server.closed and server.commands == []


async def test_printer_that_never_resumes_is_flow_pause_timeout():
    server = fake.FakeGattServer()
    pause_on_first_command(server)
    manager = manager_for(
        fake.FakeLink(server), pacing=FAST | dict(pause_timeout=0.05, chunk_delay=0.002)
    )
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] == "flow_pause_timeout"
    assert view["flow_pauses"] == 1 and view["delivery_uncertain"] is True


async def test_total_budget_exhaustion_is_transfer_timeout():
    server = fake.FakeGattServer()
    manager = manager_for(
        fake.FakeLink(server), pacing=FAST | dict(chunk_delay=0.02, total_timeout=0.05)
    )
    manager.start_print(print_request())
    view = await settle(manager, "job-000001")
    assert view["error"]["code"] == "transfer_timeout" and view["bytes_written"] > 0


async def test_sent_is_not_reported_until_the_settle_delay_after_all_writes():
    server = fake.FakeGattServer()
    manager = manager_for(fake.FakeLink(server), pacing=FAST | dict(settle_delay=0.3))
    manager.start_print(print_request())
    expected = len(b"".join(protocol.print_frames(BITMAP, 2)))
    for _ in range(200):
        if sum(len(v) for _h, v in server.commands) == expected:
            break
        await asyncio.sleep(0.005)
    view = manager.get("job-000001")
    assert view["bytes_written"] == expected
    assert view["state"] == "transmitting" and not view["terminal"]
    final = await settle(manager, "job-000001")
    assert final["state"] == "sent"


async def test_status_never_touches_the_link():
    link = fake.FakeLink(fake.FakeGattServer())
    manager = manager_for(link)
    for _ in range(5):
        status = manager.status()
    assert status == {"busy": False, "active_job_id": None, "retained_jobs": 0}
    assert link.connects == 0


async def test_shutdown_cancels_active_job_bounded_and_refuses_new_ones():
    server = fake.FakeGattServer()
    pause_on_first_command(server)
    manager = manager_for(
        fake.FakeLink(server), pacing=FAST | dict(pause_timeout=30, chunk_delay=0.002)
    )
    manager.start_print(print_request())
    await asyncio.sleep(0.1)
    await manager.shutdown()
    assert server.closed and not manager.busy
    assert manager.get("job-000001")["state"] == "cancelled"
    with pytest.raises(wire.WireError):
        manager.start_print(print_request("job-000002"))


def test_every_wire_error_code_has_a_message_and_nothing_else_does():
    # A code without a message would turn a finished job's view into a 500.
    assert set(jobs.ERROR_MESSAGES) == set(wire.JOB_ERROR_CODES)


async def test_logs_contain_no_payload_token_or_addresses(caplog):
    caplog.set_level(logging.DEBUG)
    server = fake.FakeGattServer()
    manager = manager_for(
        fake.FakeLink(server, connect_error=OSError(5, "AA:BB:CC:DD:EE:FF secret-token"))
    )
    manager.start_print(print_request(bitmap=bytes([0xA5]) * 96))
    await settle(manager, "job-000001")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "job-000001" in text
    assert "AA:BB" not in text and "secret-token" not in text and "a5a5a5" not in text.lower()
    assert "165, 165" not in text
