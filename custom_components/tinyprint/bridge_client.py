"""Home Assistant side of the TinyPrint LE bridge: HTTP client and the shared job runner.

Pure enough to test without Home Assistant: aiohttp, `.bridge_wire` and `.const` only. The
printer object handed to `run_bridge_job` is duck-typed (`phase()`, `last_result`).

Invariants:
* A start request is sent at most once and never retried. A lost answer after the request may
  have left the machine is reported as uncertain, never replayed.
* HTTP 202 means "accepted", not "printed"; only a terminal `sent` view ends a job as sent, and
  physical confirmation stays False because the X6h cannot report one.
* The bridge URL and token never appear in reprs, exception messages, logs or results. Network
  exceptions are re-raised as `BridgeError` with the original dropped (aiohttp messages carry
  the host).
* Every wait is bounded; cancellation of the caller asks the bridge to cancel before returning.
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from . import bridge_wire as wire
from .const import CONF_BRIDGE_TOKEN, CONF_BRIDGE_URL, CONF_TRANSPORT, TRANSPORT_BRIDGE
from .transport import transfer_timeout

_LOGGER = logging.getLogger(__name__)

MAX_URL_LENGTH = 200
MAX_RESPONSE_BYTES = 128 * 1024
STATUS_TIMEOUT = 5.0
START_TIMEOUT = 30.0
JOB_TIMEOUT = 10.0
CANCEL_TIMEOUT = 20.0
CONNECT_TIMEOUT = 5.0
POLL_INTERVAL = 0.5
MAX_POLL_FAILURES = 3
DIAGNOSE_DEADLINE = 120.0
DEADLINE_MARGIN = 90.0
_HOST = re.compile(r"[A-Za-z0-9.\-_\[\]:]+")
_MESSAGES = {
    "unauthorized": "The bridge rejected the access token",
    "busy": "The bridge is already running a job",
    "duplicate_job": "The bridge already knows this job id",
    "bad_request": "The bridge rejected the request",
    "payload_too_large": "The bridge rejected the request as too large",
    "unsupported_media_type": "The bridge rejected the request format",
    "not_found": "The bridge does not know this job",
    "method_not_allowed": "The bridge rejected the request method",
    "unreachable": "The bridge could not be reached",
    "timeout": "The bridge did not answer in time",
    "connection_lost": "The connection to the bridge was lost",
    "server_error": "The bridge reported an internal error",
    "bad_response": "The bridge sent an unexpected answer",
    "unexpected_redirect": "The bridge address answered with a redirect",
    "invalid_request": "The request could not be prepared",
}


class BridgeConfigError(ValueError):
    """Bridge transport is selected but the stored options are unusable."""


class BridgeError(Exception):
    """Bridge call failed. `uncertain` means the bridge may still have acted on the request."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(_MESSAGES.get(code, "The bridge call failed"))
        self.code = code
        self.uncertain = uncertain


class BridgeJobError(Exception):
    """A bridge job did not end as `sent`; the text is user-facing and secret-free."""

    def __init__(self, message: str, code: str, *, uncertain: bool) -> None:
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


def normalize_bridge_url(value: object) -> str:
    """Return `scheme://host[:port]`; reject anything that could carry credentials or paths."""
    if not isinstance(value, str):
        raise ValueError("Bridge URL must be text")
    text = value.strip()
    if (
        not text
        or len(text) > MAX_URL_LENGTH
        or any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in text)
    ):
        raise ValueError("Bridge URL is empty, too long or contains invalid characters")
    if any(char in text for char in "?#@%\\") or text.endswith(":"):
        raise ValueError("Bridge URL must not contain credentials, a query or a fragment")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("Bridge URL is malformed") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Bridge URL must be http:// or https:// with a host")
    if parts.path not in ("", "/") or parts.username or parts.password:
        raise ValueError("Bridge URL must contain only scheme, host and optional port")
    if (
        parts.netloc.endswith(":")
        or _HOST.fullmatch(parts.netloc) is None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Bridge URL host or port is invalid")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class BridgeSettings:
    url: str = field(repr=False)
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return "BridgeSettings(<redacted>)"

    __str__ = __repr__

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme


def bridge_settings(options: Mapping[str, Any]) -> BridgeSettings | None:
    """Settings for the selected transport; None means native BLE (the default)."""
    if options.get(CONF_TRANSPORT) != TRANSPORT_BRIDGE:
        return None
    try:
        url = normalize_bridge_url(options.get(CONF_BRIDGE_URL))
        token = wire.check_token_strength(options.get(CONF_BRIDGE_TOKEN))
    except ValueError:
        raise BridgeConfigError(
            "Bridge transport is selected but URL or token is invalid"
        ) from None
    return BridgeSettings(url, token)


class BridgeClient:
    """Thin authenticated HTTP client. Holds no state besides the session and settings."""

    def __init__(self, session: aiohttp.ClientSession, settings: BridgeSettings) -> None:
        self._session = session
        self._settings = settings

    def __repr__(self) -> str:
        return "BridgeClient(<redacted>)"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        timeout: float,
        mutating: bool,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self._settings.token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.request(
                method,
                self._settings.url + path,
                json=body,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=CONNECT_TIMEOUT),
            ) as response:
                status = response.status
                raw = await _read_capped(response)
        except aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError:
            # Never connected: the bridge cannot have seen the request.
            raise BridgeError("unreachable") from None
        except aiohttp.InvalidURL:
            raise BridgeError("unreachable") from None
        except TimeoutError:
            raise BridgeError("timeout", uncertain=mutating) from None
        except aiohttp.ClientError:
            raise BridgeError("connection_lost", uncertain=mutating) from None
        except _TooLarge:
            raise BridgeError("bad_response", uncertain=mutating) from None
        if 200 <= status < 300:
            try:
                return json.loads(raw)
            except ValueError:
                raise BridgeError("bad_response", uncertain=mutating) from None
        raise _http_error(status, raw, mutating)

    async def status(self) -> dict[str, Any]:
        data = await self._request(
            "GET", f"{wire.API_PREFIX}/status", timeout=STATUS_TIMEOUT, mutating=False
        )
        if (
            not isinstance(data, dict)
            or type(data.get("busy")) is not bool
            or data.get("ok") is not True
        ):
            raise BridgeError("bad_response")
        active = data.get("active_job_id")
        try:
            # A busy bridge must name its job and an idle one must not: anything else would let
            # the orphan-cancel action report "nothing active" while a print is running.
            if data["busy"] != (active is not None):
                raise wire.WireError("bad_request", "inconsistent status")
            if active is not None:
                wire.validate_job_id(active)
        except wire.WireError:
            raise BridgeError("bad_response") from None
        version = data.get("version")
        return {
            "busy": data["busy"],
            "active_job_id": active,
            "version": version if isinstance(version, str) and len(version) <= 32 else None,
        }

    async def start_print(self, job_id: str, bitmap: bytes, rows: int) -> dict[str, Any]:
        try:
            body = wire.print_request_body(job_id, bitmap, rows)
        except wire.WireError:
            raise BridgeError("invalid_request") from None
        return await self._job_call(
            "POST", f"{wire.API_PREFIX}/jobs", job_id, body, START_TIMEOUT, mutating=True
        )

    async def start_diagnose(self, job_id: str) -> dict[str, Any]:
        return await self._job_call(
            "POST",
            f"{wire.API_PREFIX}/diagnose",
            job_id,
            {"job_id": job_id},
            START_TIMEOUT,
            mutating=True,
        )

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self._job_call(
            "GET", f"{wire.API_PREFIX}/jobs/{_checked_id(job_id)}", job_id, None, JOB_TIMEOUT
        )

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return await self._job_call(
            "POST",
            f"{wire.API_PREFIX}/jobs/{_checked_id(job_id)}/cancel",
            job_id,
            None,
            CANCEL_TIMEOUT,
            mutating=True,
        )

    async def _job_call(
        self,
        method: str,
        path: str,
        job_id: str,
        body: Any,
        timeout: float,
        *,
        mutating: bool = False,
    ) -> dict[str, Any]:
        data = await self._request(method, path, body=body, timeout=timeout, mutating=mutating)
        try:
            view = wire.parse_job_view(data)
        except wire.WireError:
            raise BridgeError("bad_response", uncertain=mutating) from None
        if view["job_id"] != job_id:
            raise BridgeError("bad_response", uncertain=mutating)
        return view


class _TooLarge(Exception):
    pass


def _checked_id(job_id: str) -> str:
    try:
        return wire.validate_job_id(job_id)
    except wire.WireError:
        raise BridgeError("invalid_request") from None


async def _read_capped(response: aiohttp.ClientResponse) -> bytes:
    length = response.content_length
    if length is not None and length > MAX_RESPONSE_BYTES:
        raise _TooLarge
    chunks = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        chunks.extend(chunk)
        if len(chunks) > MAX_RESPONSE_BYTES:
            raise _TooLarge
    return bytes(chunks)


def _http_error(status: int, raw: bytes, mutating: bool) -> BridgeError:
    if 300 <= status < 400:
        return BridgeError("unexpected_redirect")
    code = None
    try:
        error = json.loads(raw).get("error")
        code = error.get("code") if isinstance(error, dict) else None
    except ValueError, AttributeError:
        pass
    if isinstance(code, str) and code in wire.HTTP_ERROR_CODES:
        return BridgeError(code)
    if status >= 500:
        return BridgeError("server_error", uncertain=mutating)
    return BridgeError("bad_response")


# --- shared job runner -------------------------------------------------------------------------

_COUNTER_KEYS = ("bytes_written", "frames_written", "writes", "notifications", "flow_pauses")
_STATE_STATUS = {"accepted": "connecting", "connecting": "connecting", "discovering": "connecting"}


def _merge(result: dict[str, Any], view: dict[str, Any]) -> None:
    """Copy only whitelisted fields; counters never go down and uncertainty is sticky."""
    for key in _COUNTER_KEYS:
        result[key] = max(result.get(key, 0), view[key])
    result["delivery_uncertain"] = (
        bool(result.get("delivery_uncertain")) or view["delivery_uncertain"]
    )
    result["confirmation"] = view["confirmation"]
    result["physical_print_confirmed"] = False
    result["bridge_state"] = view["state"]


def _merge_diagnosis(result: dict[str, Any], view: dict[str, Any]) -> None:
    diagnosis = view.get("diagnosis")
    if not isinstance(diagnosis, dict):
        return
    result["transport"] = "bridge"
    result["supported_profile"] = diagnosis.get("profile_supported") is True
    for key in ("profile", "services", "att_mtu", "link_connect_seconds", "stray_pdus"):
        if key in diagnosis:
            result[key] = diagnosis[key]


async def _bounded_cancel(
    client: BridgeClient, job_id: str, result: dict[str, Any], *, in_flight_start: bool = False
) -> None:
    """Ask the bridge to cancel; errors are recorded, never raised (we may be unwinding)."""
    outcome = "failed"
    try:
        async with asyncio.timeout(CANCEL_TIMEOUT):
            try:
                view = await client.cancel(job_id)
            except BridgeError as error:
                if error.code != "not_found" or not in_flight_start:
                    raise
                # The start request may still be on its way; look once more.
                await asyncio.sleep(0.5)
                view = await client.cancel(job_id)
            _merge(result, view)
            outcome = "acknowledged" if view["terminal"] else "pending"
    except BridgeError as error:
        outcome = "not_found" if error.code == "not_found" else "failed"
    except TimeoutError:
        outcome = "failed"
    result["bridge_cancel"] = outcome


def _deadline(kind: str, rows: int | None) -> float:
    if kind == "diagnose" or rows is None:
        return DIAGNOSE_DEADLINE
    return transfer_timeout(rows) + DEADLINE_MARGIN


async def _cancel_and_flag(
    client: BridgeClient, job_id: str, result: dict[str, Any], *, in_flight_start: bool = False
) -> None:
    """Cancel on the bridge; without an acknowledged terminal view we cannot rule out writes."""
    await _bounded_cancel(client, job_id, result, in_flight_start=in_flight_start)
    if result["bridge_cancel"] != "acknowledged" or result.get("bytes_written"):
        result["delivery_uncertain"] = True


_HINTS = {
    "unauthorized": " Check the bridge token in the TinyPrint options.",
    "unreachable": " Check that the bridge is running and the address in the options is right.",
    "busy": " Wait for the running job or cancel it.",
}


def _failure(kind: str, phase: str, code: str, result: dict[str, Any]) -> BridgeJobError:
    partial = bool(result.get("bytes_written") or result.get("delivery_uncertain"))
    hint = _HINTS.get(code, "")
    if kind == "diagnose":
        text = f"GATT diagnostic failed at {phase} ({code}); no print was sent.{hint}"
    elif partial:
        text = (
            f"Printer failed at {phase} ({code}). "
            f"Partial delivery is possible; no automatic replay.{hint}"
        )
    else:
        text = f"Printer failed at {phase} ({code}). No raster writes were attempted.{hint}"
    return BridgeJobError(text, code, uncertain=partial)


async def run_bridge_job(
    printer: Any,
    client: BridgeClient,
    kind: str,
    bitmap: bytes | None = None,
    rows: int | None = None,
    *,
    poll_interval: float | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Submit one job, follow it to a terminal state and return the merged result.

    Raises BridgeJobError for anything but a terminal `sent`; re-raises CancelledError after a
    bounded bridge cancel. Never submits twice.
    """
    result: dict[str, Any] = printer.last_result
    job_id = wire.new_job_id()
    result["bridge_job_id"] = job_id
    limit = _deadline(kind, rows) if deadline is None else deadline
    interval = POLL_INTERVAL if poll_interval is None else poll_interval
    started = monotonic()
    phase = "bridge_submit"
    printer.phase(phase, "connecting")
    try:
        try:
            if kind == "print":
                view = await client.start_print(job_id, bitmap, rows)
            else:
                view = await client.start_diagnose(job_id)
        except BridgeError as error:
            if not error.uncertain:
                raise _failure(kind, phase, error.code, result) from None
            result["delivery_uncertain"] = True
            learned = await _learn_state(client, job_id, result)
            if learned is None or not learned["terminal"]:
                # It may be printing right now with nobody left to own it.
                await _cancel_and_flag(client, job_id, result, in_flight_start=True)
            raise BridgeJobError(
                "The bridge may have accepted the job but its answer was lost; "
                "no automatic replay. Check the printer and run diagnose before printing again",
                error.code,
                uncertain=True,
            ) from None
        failures = 0
        while True:
            if view["kind"] != kind:
                # Not the job we started (stale or incompatible bridge): never trust its verdict.
                result["delivery_uncertain"] = True
                await _cancel_and_flag(client, job_id, result)
                raise _failure(kind, phase, "bridge_bad_response", result)
            _merge(result, view)
            state = view["state"]
            if view["terminal"]:
                break
            if f"bridge_{state}" != phase:
                phase = f"bridge_{state}"
                printer.phase(phase, _STATE_STATUS.get(state, "printing"))
            if monotonic() - started > limit:
                await _cancel_and_flag(client, job_id, result)
                raise _failure(kind, phase, "bridge_deadline", result)
            await asyncio.sleep(interval)
            try:
                view = await client.job(job_id)
                failures = 0
            except BridgeError as error:
                failures += 1
                if error.code in ("unauthorized", "not_found") or failures >= MAX_POLL_FAILURES:
                    await _cancel_and_flag(client, job_id, result)
                    raise _failure(kind, phase, f"bridge_{error.code}", result) from None
        if view["state"] == "sent":
            if kind == "print" and not view["bytes_written"]:
                # A print that "completed" without a single written byte cannot have printed.
                result["delivery_uncertain"] = True
                raise _failure(kind, phase, "bridge_bad_response", result)
            if kind == "diagnose":
                _merge_diagnosis(result, view)
            return dict(result)
        raise _failure(kind, phase, (view["error"] or {}).get("code", view["state"]), result)
    except asyncio.CancelledError:
        # The printer lock must outlive the bridge cancel even if more cancels arrive meanwhile.
        cleanup = asyncio.ensure_future(
            _cancel_and_flag(client, job_id, result, in_flight_start=phase == "bridge_submit")
        )
        while not cleanup.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({cleanup})
        if not cleanup.cancelled():
            cleanup.exception()
        raise


async def _learn_state(
    client: BridgeClient, job_id: str, result: dict[str, Any]
) -> dict[str, Any] | None:
    """One bounded GET after an uncertain start. Never a second POST."""
    try:
        async with asyncio.timeout(JOB_TIMEOUT + 2):
            view = await client.job(job_id)
    except BridgeError, TimeoutError:
        return None
    _merge(result, view)
    return view
