"""Wire contract between the HA client and the standalone bridge (pure, no I/O)."""

import base64
import copy
import json

import pytest
from shared_loader import load

w = load("bridge_wire")
protocol = load("protocol")
bitmap_module = load("bitmap")

GOOD_ID = "job-12345678"


def good_view(**changes):
    view = {
        "job_id": GOOD_ID,
        "kind": "print",
        "state": "sent",
        "terminal": True,
        "bytes_written": 10,
        "frames_written": 2,
        "writes": 3,
        "notifications": 1,
        "flow_pauses": 0,
        "delivery_uncertain": False,
        "confirmation": "transport_only",
        "physical_print_confirmed": False,
        "elapsed_seconds": 1.5,
        "error": None,
        "diagnosis": None,
    }
    view.update(changes)
    return view


def body(**changes):
    data = {"job_id": GOOD_ID, "rows": 2, "bitmap_b64": base64.b64encode(bytes(96)).decode()}
    data.update(changes)
    return data


def test_constants():
    assert w.API_PREFIX == "/v1"
    assert w.MAX_BODY_BYTES == 256 * 1024
    assert w.MIN_TOKEN_LENGTH == 32
    assert w.PROTOCOL_VERSION == 1
    assert w.PHASES == (
        "accepted",
        "connecting",
        "discovering",
        "transmitting",
        "sent",
        "failed",
        "cancelled",
    )
    assert w.TERMINAL_STATES == frozenset({"sent", "failed", "cancelled"})
    assert w.KINDS == ("print", "diagnose")
    assert "connect_timeout" in w.JOB_ERROR_CODES and "internal_error" in w.JOB_ERROR_CODES


def test_wire_error_defaults_and_attrs():
    error = w.WireError("bad_request", "nope")
    assert (error.code, error.message, error.status) == ("bad_request", "nope", 400)
    assert w.WireError("busy", "x", 409).status == 409


def test_new_job_id_is_valid_and_unique():
    first, second = w.new_job_id(), w.new_job_id()
    assert first != second and len(first) == 32
    assert w.validate_job_id(first) == first


@pytest.mark.parametrize("value", ["a" * 8, "a" * 64, "Ab_-1234", "x" * 32])
def test_job_id_accepts(value):
    assert w.validate_job_id(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "short", "a" * 65, "has space1", "tab\t12345", "line\n12345", "ünicode12", 12345678, None]
    + ["a" * 8 + "\n", "../../etc", b"abcdefgh", True],
)
def test_job_id_rejects(value):
    with pytest.raises(w.WireError) as caught:
        w.validate_job_id(value)
    assert caught.value.code == "bad_request" and caught.value.status == 400


def test_parse_print_request_roundtrip():
    bitmap = bytes(range(96))
    request = w.parse_print_request(w.print_request_body(GOOD_ID, bitmap, 2))
    assert (request.job_id, request.rows, request.bitmap) == (GOOD_ID, 2, bitmap)
    assert bitmap.hex() not in repr(request)


def test_print_request_body_exact_keys_and_json_safe():
    data = w.print_request_body(GOOD_ID, bytes(48), 1)
    assert set(data) == {"job_id", "rows", "bitmap_b64"}
    json.dumps(data)


@pytest.mark.parametrize("rows", [True, False, "1", 0, -1, 2049, 1.0, None, b"1"])
def test_rows_rejected(rows):
    with pytest.raises(w.WireError) as caught:
        w.parse_print_request(body(rows=rows))
    assert caught.value.code == "bad_request"


def test_max_rows_accepted():
    data = w.print_request_body(GOOD_ID, bytes(48 * 2048), 2048)
    assert w.parse_print_request(data).rows == 2048


@pytest.mark.parametrize(
    "bitmap_b64",
    ["!!!notbase64", "AAA", "AA==AA==", base64.b64encode(bytes(95)).decode(), "", 5, None, b"AA=="],
)
def test_bitmap_rejected(bitmap_b64):
    with pytest.raises(w.WireError) as caught:
        w.parse_print_request(body(bitmap_b64=bitmap_b64))
    assert caught.value.code == "bad_request"


def test_oversized_bitmap_rejected_before_decoding(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("decoded an oversized payload")

    monkeypatch.setattr(w.base64, "b64decode", forbidden)
    with pytest.raises(w.WireError):
        w.parse_print_request(body(bitmap_b64="A" * (bitmap_module.MAX_BITMAP_B64_LENGTH + 4)))


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "x",
        5,
        {},
        {"job_id": GOOD_ID, "rows": 2},
        {"rows": 2, "bitmap_b64": "AAAA"},
    ],
)
def test_non_dict_or_missing_keys_rejected(data):
    with pytest.raises(w.WireError):
        w.parse_print_request(data)


def test_extra_key_rejected():
    with pytest.raises(w.WireError):
        w.parse_print_request(body(address="AA:BB:CC:DD:EE:FF"))


def test_wire_errors_do_not_echo_request_content():
    secret = "SECRETPAYLOAD" * 3
    for data in (body(bitmap_b64=secret), body(job_id=secret + "!"), body(extra=secret)):
        with pytest.raises(w.WireError) as caught:
            w.parse_print_request(data)
        assert secret not in caught.value.message and secret not in str(caught.value)


def test_print_request_body_validates():
    with pytest.raises(w.WireError):
        w.print_request_body("bad", bytes(48), 1)
    with pytest.raises(w.WireError):
        w.print_request_body(GOOD_ID, bytes(47), 1)
    with pytest.raises(w.WireError):
        w.print_request_body(GOOD_ID, bytes(48), True)
    with pytest.raises(w.WireError):
        w.print_request_body(GOOD_ID, "x" * 48, 1)


def test_parse_diagnose_request():
    assert w.parse_diagnose_request({"job_id": GOOD_ID}) == GOOD_ID
    for data in (None, {}, {"job_id": GOOD_ID, "x": 1}, {"job_id": "bad"}, [GOOD_ID]):
        with pytest.raises(w.WireError):
            w.parse_diagnose_request(data)


def test_token_strength():
    token = "A" * 32
    assert w.check_token_strength(token) == token
    padded = "A" * 31 + " "
    for bad in ("short", "A" * 31, padded, "A" * 31 + "\n", "A" * 31 + "é", None, 5, b"A" * 32):
        with pytest.raises(ValueError) as caught:
            w.check_token_strength(bad)
        assert "AAAA" not in str(caught.value)


def test_parse_job_view_roundtrip_and_whitelist():
    view = good_view(extra="drop me")
    parsed = w.parse_job_view(view)
    assert parsed == good_view()
    assert "extra" not in parsed


def test_parse_job_view_diagnosis_and_error():
    diagnosis = {"profile_supported": True, "services": [{"uuid": "x"}], "att_mtu": 23}
    error = {"code": "link_lost", "message": "Link lost"}
    parsed = w.parse_job_view(
        good_view(
            kind="diagnose", state="failed", error=error, diagnosis=diagnosis, confirmation="none"
        )
    )
    assert parsed["error"] == error and parsed["diagnosis"] == diagnosis
    assert parsed["diagnosis"] is not diagnosis


def test_parse_job_view_accepts_every_consistent_combination():
    sent_diagnose = good_view(kind="diagnose", confirmation="connection_only")
    assert w.parse_job_view(sent_diagnose)["confirmation"] == "connection_only"
    error = {"code": "cancelled", "message": "The job was cancelled"}
    for state in ("failed", "cancelled"):
        view = good_view(state=state, confirmation="none", error=error)
        assert w.parse_job_view(view)["error"] == error


def test_parse_job_view_non_terminal_states():
    for state in ("accepted", "connecting", "discovering", "transmitting"):
        parsed = w.parse_job_view(good_view(state=state, terminal=False, confirmation="none"))
        assert parsed["state"] == state and not parsed["terminal"]


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": "short"},
        {"kind": "other"},
        {"state": "weird"},
        {"state": "transmitting", "terminal": True},
        {"state": "sent", "terminal": False},
        {"bytes_written": True},
        {"bytes_written": -1},
        {"bytes_written": 1.5},
        {"frames_written": "1"},
        {"writes": -3},
        {"notifications": None},
        {"flow_pauses": False},
        {"delivery_uncertain": 1},
        {"delivery_uncertain": "yes"},
        {"confirmation": "physical"},
        {"physical_print_confirmed": True},
        {"physical_print_confirmed": 0},
        {"physical_print_confirmed": None},
        {"elapsed_seconds": -0.1},
        {"elapsed_seconds": True},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": "1"},
        {"error": "boom"},
        {"error": {"code": "x" * 65, "message": "m"}},
        {"error": {"code": "Contains spaces; and text", "message": "m"}},
        {"error": {"code": "UPPER", "message": "m"}},
        # state, kind, confirmation and error must tell one consistent story
        {"kind": "diagnose"},
        {"confirmation": "connection_only"},
        {"confirmation": "none"},
        {"error": {"code": "link_lost", "message": "m"}},
        {"state": "failed", "confirmation": "none", "error": None},
        {"state": "cancelled", "confirmation": "none", "error": None},
        {"state": "transmitting", "terminal": False},
        {"kind": "diagnose", "state": "sent", "confirmation": "transport_only"},
        {"error": {"code": "ok", "message": "m" * 301}},
        {"error": {"code": "ok"}},
        {"error": {"code": 5, "message": "m"}},
        {"error": {"code": "ok", "message": "m", "extra": 1}},
        {"diagnosis": "text"},
        {"diagnosis": ["x"]},
        {"diagnosis": {"blob": "x" * 70000}},
    ],
)
def test_parse_job_view_rejects(changes):
    with pytest.raises(w.WireError):
        w.parse_job_view(good_view(**changes))


def test_parse_job_view_rejects_missing_keys_and_non_dict():
    for key in good_view():
        if key == "diagnosis":
            continue
        view = good_view()
        del view[key]
        with pytest.raises(w.WireError):
            w.parse_job_view(view)
    for data in (None, [], "x", 5):
        with pytest.raises(w.WireError):
            w.parse_job_view(data)


def test_parse_job_view_does_not_mutate_input():
    view = good_view(diagnosis={"a": [1, 2]})
    before = copy.deepcopy(view)
    w.parse_job_view(view)
    assert view == before


def test_bitmap_limit_matches_protocol():
    assert bitmap_module.MAX_BITMAP_BYTES == protocol.MAX_ROWS * protocol.ROW_BYTES
