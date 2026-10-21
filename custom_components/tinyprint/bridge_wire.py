"""Wire contract between the HA bridge client and the standalone print bridge.

Pure: stdlib plus `.protocol` only, so the bridge process can load it without Home Assistant
(through a synthetic package, see bridge/tinyprint_bridge/_shared.py). Both sides validate with
the same functions, which keeps the request limits from drifting apart.

Invariants: errors never echo request content (it may carry a print bitmap or a secret);
`physical_print_confirmed` must be exactly False because the X6h has no completed-print ACK;
sizes are bounded before any decoding.
"""

import base64
import binascii
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .protocol import MAX_ROWS, ROW_BYTES

API_PREFIX = "/v1"
MAX_BODY_BYTES = 256 * 1024
MIN_TOKEN_LENGTH = 32
PROTOCOL_VERSION = 1
MAX_BITMAP_BYTES = MAX_ROWS * ROW_BYTES
MAX_BITMAP_B64_LENGTH = 4 * ((MAX_BITMAP_BYTES + 2) // 3)
MAX_DIAGNOSIS_JSON = 32 * 1024
MAX_COUNTER = 10**12

PHASES = ("accepted", "connecting", "discovering", "transmitting", "sent", "failed", "cancelled")
TERMINAL_STATES = frozenset({"sent", "failed", "cancelled"})
KINDS = ("print", "diagnose")
CONFIRMATIONS = ("none", "connection_only", "transport_only")

JOB_ERROR_CODES = (
    "connect_timeout",
    "connect_failed",
    "profile_unsupported",
    "att_protocol_error",
    "link_lost",
    "write_failed",
    "flow_pause_timeout",
    "transfer_timeout",
    "printer_fault",
    "cancelled",
    "internal_error",
)
HTTP_ERROR_CODES = {
    "unauthorized": 401,
    "bad_request": 400,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "busy": 409,
    "duplicate_job": 409,
    "not_found": 404,
    "method_not_allowed": 405,
}

_JOB_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_COUNTERS = ("bytes_written", "frames_written", "writes", "notifications", "flow_pauses")


class WireError(Exception):
    """A request or response broke the contract. Messages are generic by design."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PrintRequest:
    job_id: str
    rows: int
    bitmap: bytes = field(repr=False)


def _bad(message: str) -> WireError:
    return WireError("bad_request", message)


def new_job_id() -> str:
    return uuid.uuid4().hex


def validate_job_id(value: object) -> str:
    if not isinstance(value, str) or _JOB_ID.fullmatch(value) is None:
        raise _bad("job_id must be 8-64 characters of letters, digits, underscore or dash")
    return value


def _check_rows(value: object) -> int:
    # bool is an int subclass; True must not become one row.
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise _bad(f"rows must be an integer from 1 to {MAX_ROWS}")
    return value


def _check_keys(data: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != keys:
        raise _bad("Request body must be an object with exactly the documented fields")
    return data


def parse_print_request(data: object) -> PrintRequest:
    data = _check_keys(data, {"job_id", "rows", "bitmap_b64"})
    job_id = validate_job_id(data["job_id"])
    rows = _check_rows(data["rows"])
    encoded = data["bitmap_b64"]
    if not isinstance(encoded, str) or len(encoded) > MAX_BITMAP_B64_LENGTH:
        raise _bad("bitmap_b64 must be a base64 string within the size limit")
    try:
        bitmap = base64.b64decode(encoded, validate=True)
    except binascii.Error, ValueError:
        raise _bad("bitmap_b64 is not valid base64") from None
    if len(bitmap) != rows * ROW_BYTES:
        raise _bad("bitmap size does not match rows")
    return PrintRequest(job_id, rows, bitmap)


def parse_diagnose_request(data: object) -> str:
    return validate_job_id(_check_keys(data, {"job_id"})["job_id"])


def print_request_body(job_id: str, bitmap: bytes, rows: int) -> dict[str, Any]:
    """Client side: run the server's validation locally, then encode."""
    validate_job_id(job_id)
    _check_rows(rows)
    if not isinstance(bitmap, bytes | bytearray | memoryview) or len(bitmap) != rows * ROW_BYTES:
        raise _bad("bitmap size does not match rows")
    return {
        "job_id": job_id,
        "rows": rows,
        "bitmap_b64": base64.b64encode(bytes(bitmap)).decode("ascii"),
    }


def check_token_strength(token: object) -> str:
    """Return the token unchanged or raise ValueError (the message never contains it)."""
    if not isinstance(token, str):
        raise ValueError("Bridge token must be a string")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"Bridge token must be at least {MIN_TOKEN_LENGTH} characters")
    if not all(0x21 <= ord(char) <= 0x7E for char in token):
        raise ValueError("Bridge token must be printable ASCII without whitespace")
    return token


def _counter(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise _bad("Bridge returned an invalid counter")
    return value


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise _bad("Bridge returned an invalid flag")
    return value


def _error(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        raise _bad("Bridge returned an invalid error")
    code, message = value["code"], value["message"]
    if not isinstance(code, str) or not isinstance(message, str):
        raise _bad("Bridge returned an invalid error")
    # The code is shown in user-facing text, so it must look like a code, not prose.
    if _ERROR_CODE.fullmatch(code) is None or len(message) > 300:
        raise _bad("Bridge returned an invalid error")
    return {"code": code, "message": message}


def _diagnosis(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad("Bridge returned an invalid diagnosis")
    try:
        encoded = json.dumps(value, allow_nan=False)
    except TypeError, ValueError, RecursionError:
        raise _bad("Bridge returned an invalid diagnosis") from None
    if len(encoded) > MAX_DIAGNOSIS_JSON:
        raise _bad("Bridge returned an oversized diagnosis")
    return json.loads(encoded)


def parse_job_view(data: object) -> dict[str, Any]:
    """Client side: strictly parse a bridge job view and return only the whitelisted keys."""
    if not isinstance(data, dict):
        raise _bad("Bridge returned an invalid job")
    required = {
        "job_id",
        "kind",
        "state",
        "terminal",
        "delivery_uncertain",
        "confirmation",
        "physical_print_confirmed",
        "elapsed_seconds",
        "error",
        *_COUNTERS,
    }
    if not required <= set(data):
        raise _bad("Bridge returned an incomplete job")
    state = data["state"]
    if (
        state not in PHASES
        or data["kind"] not in KINDS
        or data["confirmation"] not in CONFIRMATIONS
    ):
        raise _bad("Bridge returned an unknown job state")
    terminal = _flag(data["terminal"])
    if terminal != (state in TERMINAL_STATES):
        raise _bad("Bridge returned an inconsistent job state")
    error = _error(data["error"])
    if (error is not None) != (state in ("failed", "cancelled")):
        raise _bad("Bridge returned an inconsistent job error")
    if state != "sent":
        expected_confirmation = "none"
    else:
        expected_confirmation = "transport_only" if data["kind"] == "print" else "connection_only"
    if data["confirmation"] != expected_confirmation:
        raise _bad("Bridge returned an inconsistent confirmation")
    if _flag(data["physical_print_confirmed"]):
        raise _bad("Bridge claimed physical confirmation, which the printer cannot give")
    elapsed = data["elapsed_seconds"]
    if (
        type(elapsed) not in (int, float)
        or not math.isfinite(elapsed)
        or not 0 <= elapsed <= MAX_COUNTER
    ):
        raise _bad("Bridge returned an invalid duration")
    view: dict[str, Any] = {
        "job_id": validate_job_id(data["job_id"]),
        "kind": data["kind"],
        "state": state,
        "terminal": terminal,
        "delivery_uncertain": _flag(data["delivery_uncertain"]),
        "confirmation": data["confirmation"],
        "physical_print_confirmed": False,
        "elapsed_seconds": elapsed,
        "error": error,
        "diagnosis": _diagnosis(data.get("diagnosis")),
    }
    for name in _COUNTERS:
        view[name] = _counter(data[name])
    return {key: view[key] for key in _VIEW_ORDER}


_VIEW_ORDER = (
    "job_id",
    "kind",
    "state",
    "terminal",
    "bytes_written",
    "frames_written",
    "writes",
    "notifications",
    "flow_pauses",
    "delivery_uncertain",
    "confirmation",
    "physical_print_confirmed",
    "elapsed_seconds",
    "error",
    "diagnosis",
)
