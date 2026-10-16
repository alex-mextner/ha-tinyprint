"""ATT client: strict PDU codec, dynamic discovery, reader task and the bleak-like facade.

Everything runs against tests/att_fake.py; no socket and no Bluetooth is ever opened.
"""

import asyncio
import struct

import att_fake as fake
import pytest
from shared_loader import load

att = load("att")
protocol = load("protocol")
transport = load("transport")

KW = dict(chunk_delay=0, row_delay=0, settle_delay=0, pause_timeout=0.5, total_timeout=5)


async def connected(server, **kwargs):
    kwargs.setdefault("request_timeout", 0.3)
    kwargs.setdefault("send_timeout", 0.3)
    client = att.AttClient(server, **kwargs)
    await client.start()
    return client


async def gatt_pair(server=None, **kwargs):
    server = server or fake.FakeGattServer()
    client = await connected(server, **kwargs)
    profile = await att.discover_profile(client)
    return server, client, att.AttGattClient(client, profile)


# -- UUID helpers and PDU codec ------------------------------------------------------------


def test_uuid_roundtrip_16_and_128_bit():
    assert att.uuid_from_bytes(b"\x30\xae") == protocol.SERVICE_UUID
    assert att.uuid_to_bytes(protocol.SERVICE_UUID) == b"\x30\xae"
    custom = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    wire = att.uuid_to_bytes(custom)
    assert len(wire) == 16 and att.uuid_from_bytes(wire) == custom
    wide_base = bytes.fromhex("fb349b5f800000800010000030ae0000")
    assert att.uuid_from_bytes(wide_base) == protocol.SERVICE_UUID
    for bad in (b"", b"\x01", b"\x01\x02\x03"):
        with pytest.raises(att.AttProtocolError):
            att.uuid_from_bytes(bad)


def test_request_builders_are_exact():
    assert att.read_by_group_type_request(1, 0xFFFF, 0x2800) == bytes.fromhex("10 0100 ffff 0028")
    assert att.read_by_type_request(3, 9, 0x2803) == bytes.fromhex("08 0300 0900 0328")
    assert att.find_information_request(5, 7) == bytes.fromhex("04 0500 0700")
    assert att.write_request(0x0A, b"\x01\x00") == bytes.fromhex("12 0a00 0100")
    assert att.write_command(0x0A, b"ab") == bytes.fromhex("52 0a00 6162")
    assert att.confirmation() == b"\x1e"
    assert att.exchange_mtu_response(23) == bytes.fromhex("03 1700")
    assert att.error_response(0x08, 0, 0x06) == bytes.fromhex("01 08 0000 06")


@pytest.mark.parametrize("start,end", [(0, 5), (5, 4), (1, 0x10000), (-1, 3)])
def test_request_builders_reject_bad_ranges(start, end):
    with pytest.raises(ValueError):
        att.find_information_request(start, end)


def test_group_response_parser_accepts_valid():
    pdu = bytes.fromhex("11 06 0100 0300 0018  0400 0900 30ae")
    services = att.parse_read_by_group_response(pdu, 1, 0xFFFF)
    assert [(s.start, s.end, s.uuid) for s in services] == [
        (1, 3, "00001800" + fake.BASE_SUFFIX),
        (4, 9, protocol.SERVICE_UUID),
    ]


@pytest.mark.parametrize(
    "pdu",
    [
        b"",
        b"\x11",
        b"\x11\x06",  # no entries
        b"\x11\x00" + bytes(6),  # zero entry length
        b"\x11\x05" + bytes(10),  # impossible entry length
        b"\x11\x06" + bytes(7),  # not a multiple of the entry size
        b"\x11\x14" + bytes(20 + 5),  # 128-bit entries, truncated
        bytes.fromhex("11 06 0500 0300 0018"),  # end < start
        bytes.fromhex("11 06 0000 0300 0018"),  # handle zero
        bytes.fromhex("11 06 0400 0600 0018 0300 0900 30ae"),  # not increasing
        bytes.fromhex("11 06 0100 0900 0018 0500 0a00 30ae"),  # overlaps previous group
        bytes.fromhex("13 06 0100 0300 0018"),  # wrong opcode
    ],
)
def test_group_response_parser_rejects(pdu):
    with pytest.raises(att.AttProtocolError):
        att.parse_read_by_group_response(pdu, 1, 0xFFFF)


def test_group_response_start_outside_requested_range_is_rejected():
    pdu = bytes.fromhex("11 06 0100 0300 0018")
    with pytest.raises(att.AttProtocolError):
        att.parse_read_by_group_response(pdu, 2, 0xFFFF)
    with pytest.raises(att.AttProtocolError):
        att.parse_read_by_group_response(pdu, 0, 0)


@pytest.mark.parametrize(
    "pdu",
    [
        b"\x09\x00" + bytes(7),
        b"\x09\x06" + bytes(6),
        b"\x09\x07" + bytes(8),
        b"\x09\x07",
        bytes.fromhex("09 07 0500 04 0500 01ae"),  # value handle not after declaration
        bytes.fromhex("09 07 0500 04 0900 01ae"),  # value handle beyond the service end (8)
        bytes.fromhex("09 07 0900 04 0a00 01ae"),  # declaration beyond requested end
        bytes.fromhex("09 07 0400 04 0500 01ae 0400 04 0600 02ae"),  # not increasing
    ],
)
def test_characteristic_parser_rejects(pdu):
    with pytest.raises(att.AttProtocolError):
        att.parse_characteristic_declarations(pdu, 4, 8)


def test_characteristic_parser_accepts_16_and_128_bit():
    pdu = bytes.fromhex("09 07 0500 04 0600 01ae 0700 10 0800 02ae")
    chars = att.parse_characteristic_declarations(pdu, 4, 8)
    assert [(c.declaration_handle, c.value_handle, c.properties) for c in chars] == [
        (5, 6, 4),
        (7, 8, 0x10),
    ]
    long_pdu = bytes.fromhex("09 15 0500 04 0600") + bytes(range(16))
    assert len(att.parse_characteristic_declarations(long_pdu, 4, 8)) == 1


@pytest.mark.parametrize(
    "pdu",
    [
        b"\x05",
        b"\x05\x01",
        b"\x05\x00" + bytes(4),
        b"\x05\x03" + bytes(4),
        b"\x05\x01" + bytes(5),  # not a multiple of 4
        b"\x05\x02" + bytes(17),
        bytes.fromhex("05 01 0000 0229"),  # handle zero
        bytes.fromhex("05 01 0300 0229"),  # below requested start
        bytes.fromhex("05 01 0a00 0229"),  # above requested end
        bytes.fromhex("05 01 0600 0229 0600 0329"),  # not increasing
    ],
)
def test_find_information_parser_rejects(pdu):
    with pytest.raises(att.AttProtocolError):
        att.parse_find_information_response(pdu, 5, 8)


def test_error_response_parser():
    assert att.parse_error_response(bytes.fromhex("01 08 0500 0a")) == (8, 5, 0x0A)
    for bad in (b"\x01", b"\x01\x08\x05\x00", bytes(6), b"\x02" + bytes(4)):
        with pytest.raises(att.AttProtocolError):
            att.parse_error_response(bad)


# -- reader task and request matching -------------------------------------------------------


async def test_unsolicited_peer_requests_are_answered_not_crashing():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.push(bytes.fromhex("02 1700"))  # peer Exchange MTU
    server.push(bytes.fromhex("0a 0300"))  # peer Read request
    server.push(bytes.fromhex("08 0100 ffff 0328"))  # peer Read By Type
    await asyncio.sleep(0.02)
    assert server.received == [
        bytes.fromhex("03 1700"),
        bytes.fromhex("01 0a 0000 06"),
        bytes.fromhex("01 08 0000 06"),
    ]
    assert not client.is_closed
    await client.aclose()


async def test_peer_mtu_request_never_raises_our_mtu_above_23():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.push(bytes.fromhex("02 0002"))  # peer offers 512
    await asyncio.sleep(0.02)
    assert server.received == [bytes.fromhex("03 1700")]
    await client.aclose()


async def test_peer_mtu_request_during_outstanding_request_does_not_break_it():
    server = fake.FakeGattServer()
    client = await connected(server)

    def with_mtu(responses):
        server.push(bytes.fromhex("02 1700"))
        return responses

    server.mutators[0x10] = with_mtu
    services = await att.read_primary_services(client)
    assert len(services) == 2
    assert bytes.fromhex("03 1700") in server.received
    await client.aclose()


async def test_peer_commands_and_confirmations_are_ignored():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.push(bytes.fromhex("52 0300 aa"))
    server.push(b"\x1e")
    await asyncio.sleep(0.02)
    assert server.received == [] and not client.is_closed
    await client.aclose()


async def test_garbage_pdus_never_crash_the_reader():
    server = fake.FakeGattServer()
    client = await connected(server)
    for garbage in (b"", b"\xff", b"\x1b", b"\x1b\x01", b"\x01", b"\x02", b"\x11" * 600):
        server.push(garbage)
    await asyncio.sleep(0.02)
    assert not client.is_closed
    assert len(await att.read_primary_services(client)) == 2
    assert client.stats["malformed_pdus"] >= 5
    await client.aclose()


async def test_stray_response_is_counted_and_never_delivered():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.push(b"\x13")  # response with nothing pending
    await asyncio.sleep(0.02)
    assert client.stats["stray_pdus"] == 1
    assert len(await att.read_primary_services(client)) == 2
    await client.aclose()


async def test_error_response_for_other_opcode_is_stray_and_request_times_out():
    server = fake.FakeGattServer()
    client = await connected(server, request_timeout=0.05)
    server.mutators[0x10] = lambda responses: [bytes.fromhex("01 08 0100 0a")]
    with pytest.raises(att.AttTimeout):
        await att.read_primary_services(client)
    assert client.stats["stray_pdus"] == 1
    assert client.is_closed
    await client.aclose()


async def test_timeout_closes_client_and_sends_nothing_afterwards():
    server = fake.FakeGattServer()
    client = await connected(server, request_timeout=0.05)
    server.silent.add(0x10)
    with pytest.raises(att.AttTimeout):
        await att.read_primary_services(client)
    assert client.is_closed and client.close_reason == "att_timeout"
    assert server.closed  # channel released
    sent = len(server.received)
    with pytest.raises(ConnectionError):
        await att.read_primary_services(client)
    assert len(server.received) == sent
    await client.aclose()


async def test_eof_during_request_fails_with_connection_error():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.mutators[0x10] = lambda responses: (server.drop_link(), [])[1]
    with pytest.raises(ConnectionError):
        await att.read_primary_services(client)
    assert client.is_closed and client.closed.is_set()
    assert client.close_reason == "link_lost"
    await client.aclose()


async def test_indication_is_confirmed_before_handler_runs():
    server = fake.FakeGattServer()
    client = await connected(server)
    seen = []

    def handler(value):
        seen.append((value, [e for e in server.events if e == ("confirmation",)]))

    client.set_handler(7, handler)
    server.push_indication(7, b"\x01\x02")
    await asyncio.sleep(0.02)
    assert seen == [(b"\x01\x02", [("confirmation",)])]
    assert server.received == [b"\x1e"]
    await client.aclose()


async def test_notification_goes_only_to_registered_handle_and_handler_errors_are_contained():
    server = fake.FakeGattServer()
    client = await connected(server)
    seen = []

    def boom(value):
        seen.append(value)
        raise RuntimeError("handler bug")

    client.set_handler(7, boom)
    server.push_notification(8, b"x")
    server.push_notification(7, b"y")
    server.push_notification(7, b"z")
    await asyncio.sleep(0.02)
    assert seen == [b"y", b"z"] and not client.is_closed
    client.remove_handler(7)
    server.push_notification(7, b"q")
    await asyncio.sleep(0.02)
    assert seen == [b"y", b"z"]
    await client.aclose()


async def test_stalled_send_times_out_and_closes():
    server = fake.FakeGattServer()
    client = await connected(server, send_timeout=0.05)
    server.stall_commands = True
    with pytest.raises(att.AttTimeout):
        await client.write_command(3, b"x")
    assert client.is_closed and server.closed
    await client.aclose()


async def test_aclose_is_idempotent_and_releases_channel():
    server = fake.FakeGattServer()
    client = await connected(server)
    await client.aclose()
    await client.aclose()
    assert server.closed and client.is_closed and client.closed.is_set()


async def test_oversized_pdu_is_ignored():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.push(b"\x1b\x07\x00" + bytes(600))
    seen = []
    client.set_handler(7, seen.append)
    await asyncio.sleep(0.02)
    assert seen == [] and not client.is_closed
    await client.aclose()


# -- dynamic discovery -----------------------------------------------------------------------


async def test_profile_discovered_with_dynamic_handles():
    server = fake.FakeGattServer(first_handle=11, gap=2)
    client = await connected(server)
    profile = await att.discover_profile(client)
    assert profile.write.value_handle == server.write_handle
    assert profile.notify.value_handle == server.notify_handle
    assert profile.cccd_handle == server.cccd_handle
    assert (profile.write.value_handle, profile.notify.value_handle) != (6, 8)
    assert profile.service.uuid == protocol.SERVICE_UUID
    await client.aclose()


async def test_profile_with_128_bit_uuids_and_many_pages():
    services = [
        fake.Service(f"1000{i:04x}-0000-1000-8000-00805f9b34fb", [fake.Char(fake.WRITE, 2)])
        for i in range(7)
    ] + [
        fake.Service(
            fake.SERVICE,
            [
                fake.Char(fake.WRITE, 0x0C, wide=True),
                fake.Char(fake.NOTIFY, 0x10, cccd=True, wide=True),
            ],
            wide=True,
        )
    ]
    server = fake.FakeGattServer(services)
    client = await connected(server)
    profile = await att.discover_profile(client)
    assert profile.cccd_handle == server.cccd_handle
    assert server.request_opcodes(0x10) > 2  # pagination was exercised
    await client.aclose()


async def test_cccd_taken_from_ae02_not_from_neighbour():
    services = [
        fake.Service(
            fake.SERVICE,
            [
                fake.Char(fake.WRITE, 0x04),
                fake.Char(fake.NOTIFY, 0x10, cccd=True, extra_descriptors=(0x2901,)),
                fake.Char("0000ae03" + fake.BASE_SUFFIX, 0x10, cccd=True),
            ],
        )
    ]
    server = fake.FakeGattServer(services)
    client = await connected(server)
    profile = await att.discover_profile(client)
    assert profile.cccd_handle == server.cccd_handles[fake.NOTIFY]
    assert profile.cccd_handle != server.cccd_handles["0000ae03" + fake.BASE_SUFFIX]
    await client.aclose()


async def test_missing_cccd_is_not_borrowed_from_neighbour():
    services = [
        fake.Service(
            fake.SERVICE,
            [
                fake.Char(fake.WRITE, 0x04),
                fake.Char(fake.NOTIFY, 0x10, cccd=False),
                fake.Char("0000ae03" + fake.BASE_SUFFIX, 0x10, cccd=True),
            ],
        ),
        fake.Service(
            "0000ae40" + fake.BASE_SUFFIX,
            [fake.Char("0000ae41" + fake.BASE_SUFFIX, 0x10, cccd=True)],
        ),
    ]
    client = await connected(fake.FakeGattServer(services))
    with pytest.raises(att.ProfileError, match="CCCD"):
        await att.discover_profile(client)
    await client.aclose()


async def test_missing_cccd_on_last_characteristic_does_not_scan_next_service():
    services = [
        fake.Service(fake.SERVICE, [fake.Char(fake.WRITE, 0x04), fake.Char(fake.NOTIFY, 0x10)]),
        fake.Service(
            "0000ae40" + fake.BASE_SUFFIX,
            [fake.Char("0000ae41" + fake.BASE_SUFFIX, 0x10, cccd=True)],
        ),
    ]
    client = await connected(fake.FakeGattServer(services))
    with pytest.raises(att.ProfileError, match="CCCD"):
        await att.discover_profile(client)
    await client.aclose()


@pytest.mark.parametrize(
    "services,message",
    [
        ([fake.Service("0000ae40" + fake.BASE_SUFFIX, [])], "AE30"),
        (
            [fake.Service(fake.SERVICE, [fake.Char(fake.NOTIFY, 0x10, cccd=True)])],
            "AE01",
        ),
        (
            [fake.Service(fake.SERVICE, [fake.Char(fake.WRITE, 0x04)])],
            "AE02",
        ),
        (
            [
                fake.Service(
                    fake.SERVICE,
                    [fake.Char(fake.WRITE, 0x08), fake.Char(fake.NOTIFY, 0x10, cccd=True)],
                )
            ],
            "write-without-response",
        ),
        (
            [
                fake.Service(
                    fake.SERVICE,
                    [fake.Char(fake.WRITE, 0x04), fake.Char(fake.NOTIFY, 0x20, cccd=True)],
                )
            ],
            "notify",
        ),
    ],
)
async def test_profile_errors(services, message):
    client = await connected(fake.FakeGattServer(services))
    with pytest.raises(att.ProfileError, match=message):
        await att.discover_profile(client)
    await client.aclose()


async def test_discovery_stops_paging_once_ae30_is_found():
    client = await connected(fake.FakeGattServer())
    services = await att.read_primary_services(client, stop_uuid=protocol.SERVICE_UUID)
    assert services[-1].uuid == protocol.SERVICE_UUID
    await client.aclose()


FAULTS = {
    "zero_entry_length": lambda r: [b"\x11\x00" + bytes(6)],
    "wrong_entry_length": lambda r: [b"\x11\x05" + bytes(10)],
    "truncated": lambda r: [b"\x11"],
    "empty": lambda r: [b"\x11\x06"],
    "handles_outside_range": lambda r: [bytes.fromhex("11 06 0000 0300 0018")],
    "non_increasing": lambda r: [bytes.fromhex("11 06 0400 0600 0018 0300 0900 30ae")],
}


@pytest.mark.parametrize("name", sorted(FAULTS))
async def test_malformed_group_responses_are_protocol_errors(name):
    server = fake.FakeGattServer()
    client = await connected(server)
    server.mutators[0x10] = FAULTS[name]
    with pytest.raises(att.AttProtocolError):
        await att.read_primary_services(client)
    await client.aclose()


async def test_non_progress_pagination_is_rejected():
    server = fake.FakeGattServer()
    client = await connected(server)
    # Always the same first entry, so the next request start never advances past it.
    server.mutators[0x10] = lambda r: [bytes.fromhex("11 06 0100 0300 0018")]
    with pytest.raises(att.AttProtocolError):
        await att.read_primary_services(client)
    assert server.request_opcodes(0x10) <= 3
    await client.aclose()


async def test_endless_progressing_pagination_is_capped():
    server = fake.FakeGattServer()
    client = await connected(server)
    counter = {"next": 1}

    def endless(responses):
        handle = counter["next"]
        counter["next"] += 1
        return [bytes((0x11, 6)) + struct.pack("<HH", handle, handle) + b"\x00\x18"]

    server.mutators[0x10] = endless
    with pytest.raises(att.AttProtocolError, match="too many"):
        await att.read_primary_services(client)
    assert server.request_opcodes(0x10) <= att.MAX_DISCOVERY_REQUESTS
    await client.aclose()


async def test_unexpected_error_code_during_discovery_is_an_att_error():
    server = fake.FakeGattServer()
    client = await connected(server)
    server.mutators[0x10] = lambda r: [bytes.fromhex("01 10 0100 05")]  # authentication
    with pytest.raises(att.AttErrorResponse) as caught:
        await att.read_primary_services(client)
    assert caught.value.error_code == 5
    assert not client.is_closed  # a valid error response is a completed transaction
    await client.aclose()


async def test_find_descriptor_handles_empty_and_scoped_range():
    server = fake.FakeGattServer()
    client = await connected(server)
    assert await att.find_descriptor(client, 9, 8) is None
    assert server.request_opcodes(0x04) == 0
    profile = await att.discover_profile(client)
    cccd = await att.find_descriptor(client, profile.notify.value_handle, profile.service.end)
    assert cccd == server.cccd_handle
    # A range holding only the value attribute has no CCCD.
    only_value = profile.notify.value_handle
    assert await att.find_descriptor(client, only_value, only_value) is None
    await client.aclose()


async def test_discover_all_is_json_safe_and_bounded():
    import json

    server = fake.FakeGattServer()
    client = await connected(server)
    result = await att.discover_all(client)
    json.dumps(result)
    uuids = [s["uuid"] for s in result["services"]]
    assert protocol.SERVICE_UUID in uuids
    ae30 = next(s for s in result["services"] if s["uuid"] == protocol.SERVICE_UUID)
    names = {c["uuid"]: c for c in ae30["characteristics"]}
    assert names[protocol.WRITE_UUID]["properties"] == ["write-without-response", "write"]
    assert names[protocol.NOTIFY_UUID]["properties"] == ["notify"]
    assert result["ae02_cccd_handle"] == server.cccd_handle
    await client.aclose()


async def test_discover_all_truncates_instead_of_growing():
    services = [
        fake.Service(f"1000{i:04x}-0000-1000-8000-00805f9b34fb", [fake.Char(fake.WRITE, 2)])
        for i in range(6)
    ]
    client = await connected(fake.FakeGattServer(services))
    result = await att.discover_all(client, max_services=3, max_chars=1)
    assert len(result["services"]) == 3 and result["truncated"] is True
    await client.aclose()


# -- AttGattClient with transport.send_bitmap -------------------------------------------------


async def test_send_bitmap_over_att_end_to_end():
    server, client, gatt = await gatt_pair()
    bitmap = bytes(range(48)) * 3
    stats = await transport.send_bitmap(gatt, bitmap, 3, **KW)
    expected = b"".join(protocol.print_frames(bitmap, 3))
    assert b"".join(value for _handle, value in server.commands) == expected
    assert all(len(value) <= 20 for _handle, value in server.commands)
    assert {handle for handle, _ in server.commands} == {server.write_handle}
    assert all(pdu[0] == 0x52 and len(pdu) <= 23 for pdu in server.received if pdu[0] == 0x52)
    assert stats["bytes_written"] == len(expected)
    assert gatt.counters["bytes_written"] == len(expected)
    assert gatt.counters["writes"] == len(server.commands)
    assert gatt.counters["frames_written"] == stats["frames_written"] == 3 + 3 + 2
    assert [e for e in server.events if e[0] == "cccd"] == [
        ("cccd", b"\x01\x00"),
        ("cccd", b"\x00\x00"),
    ]
    assert server.closed and not gatt.is_connected
    assert not gatt.counters["delivery_uncertain"]


async def test_notification_pause_and_resume_gate_the_writes():
    server, client, gatt = await gatt_pair()
    loop = asyncio.get_running_loop()

    def on_command(srv, handle, value):
        if len(srv.commands) == 1:
            srv.push_notification(srv.notify_handle, fake.PAUSE)
            srv.events.append(("pause",))
            loop.call_later(
                0.05,
                lambda: (
                    srv.push_notification(srv.notify_handle, fake.RESUME),
                    srv.events.append(("resume",)),
                ),
            )

    server.on_command = on_command
    stats = await transport.send_bitmap(gatt, bytes(48), 1, **KW | dict(chunk_delay=0.002))
    kinds = [e[0] for e in server.events]
    assert kinds.index("pause") < kinds.index("resume")
    between = kinds[kinds.index("pause") : kinds.index("resume")]
    assert "cmd" not in between
    assert stats["flow_pauses"] == 1 and gatt.counters["flow_pauses"] == 1
    assert gatt.counters["notifications"] == 2 and stats["notifications"] == 2
    assert stats["bytes_written"] == gatt.counters["bytes_written"]


async def test_notifications_on_other_handles_are_ignored():
    server, client, gatt = await gatt_pair()
    seen = []
    await gatt.start_notify(protocol.NOTIFY_UUID, lambda _c, data: seen.append(bytes(data)))
    server.push_notification(server.write_handle, b"x")
    server.push_notification(server.notify_handle, b"ok")
    await asyncio.sleep(0.02)
    assert seen == [b"ok"] and gatt.counters["notifications"] == 1
    await gatt.disconnect()


async def test_partial_write_failure_keeps_counters_and_marks_uncertain():
    server, client, gatt = await gatt_pair()
    server.fail_send_after_commands = 3
    with pytest.raises(ConnectionError):
        await transport.send_bitmap(gatt, bytes(48), 1, **KW)
    assert len(server.commands) == 3
    assert gatt.counters["writes"] == 3
    assert gatt.counters["bytes_written"] == sum(len(v) for _h, v in server.commands)
    assert gatt.counters["delivery_uncertain"] is True
    assert server.closed
    # never replayed: no further command after the failure
    assert len(server.commands) == 3


async def test_cancel_mid_transfer_releases_channel_and_stops_writing():
    server, client, gatt = await gatt_pair()
    server.on_command = lambda srv, h, v: (
        srv.push_notification(srv.notify_handle, fake.PAUSE) if len(srv.commands) == 1 else None
    )
    task = asyncio.create_task(
        transport.send_bitmap(gatt, bytes(48), 1, **KW | dict(chunk_delay=0.002))
    )
    await asyncio.sleep(0.05)
    sent = len(server.commands)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.closed and client.is_closed
    assert len(server.commands) == sent
    assert [e for e in server.events if e[0] == "cccd"][-1] == ("cccd", b"\x00\x00")


async def test_cancel_during_a_stalled_write_is_uncertain_and_cleans_up():
    server, client, gatt = await gatt_pair()
    server.stall_commands = True
    task = asyncio.create_task(transport.send_bitmap(gatt, bytes(48), 1, **KW))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gatt.counters["delivery_uncertain"] is True
    assert server.closed
    assert gatt.counters["bytes_written"] == 0


async def test_write_gatt_char_rejects_everything_but_small_commands_to_ae01():
    server, client, gatt = await gatt_pair()
    before = len(server.received)
    with pytest.raises(ValueError):
        await gatt.write_gatt_char(protocol.NOTIFY_UUID, b"x", response=False)
    with pytest.raises(ValueError):
        await gatt.write_gatt_char(protocol.WRITE_UUID, b"x", response=True)
    with pytest.raises(ValueError):
        await gatt.write_gatt_char(protocol.WRITE_UUID, bytes(21), response=False)
    with pytest.raises(ValueError):
        await gatt.write_gatt_char(protocol.WRITE_UUID, b"", response=False)
    assert len(server.received) == before and gatt.counters["writes"] == 0
    await gatt.disconnect()


async def test_services_facade_reports_discovered_properties():
    server, client, gatt = await gatt_pair()
    write = gatt.services.get_characteristic(protocol.WRITE_UUID)
    notify = gatt.services.get_characteristic(protocol.NOTIFY_UUID)
    assert "write-without-response" in write.properties and "notify" in notify.properties
    assert gatt.services.get_characteristic("0000ae10" + fake.BASE_SUFFIX) is None
    assert gatt.is_connected
    await gatt.disconnect()
    await gatt.disconnect()
    assert not gatt.is_connected and server.closed


async def test_start_notify_failure_surfaces_att_error():
    server, client, gatt = await gatt_pair()
    server.reject_cccd_write = 0x03
    with pytest.raises(att.AttErrorResponse):
        await gatt.start_notify(protocol.NOTIFY_UUID, lambda *_: None)
    await gatt.disconnect()


async def test_stop_notify_after_link_loss_is_a_quiet_noop():
    server, client, gatt = await gatt_pair()
    await gatt.start_notify(protocol.NOTIFY_UUID, lambda *_: None)
    server.drop_link()
    await asyncio.sleep(0.02)
    await gatt.stop_notify(protocol.NOTIFY_UUID)
    await gatt.disconnect()


async def test_link_loss_mid_job_is_reported_not_replayed():
    server, client, gatt = await gatt_pair()
    server.on_command = lambda srv, h, v: srv.drop_link() if len(srv.commands) == 2 else None
    with pytest.raises(ConnectionError):
        await transport.send_bitmap(gatt, bytes(48), 1, **KW | dict(chunk_delay=0.002))
    assert len(server.commands) < 12
    assert server.closed
