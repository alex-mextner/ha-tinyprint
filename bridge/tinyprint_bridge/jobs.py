"""Job manager: one hardware job at a time, bounded memory, honest outcomes, never a replay.

Reached from server.py handlers. The active-job slot is a plain attribute taken synchronously in
`_begin` (before any await) and released only in `_finalize`, which runs after the worker task
ended, i.e. after the socket and reader cleanup. So busy/duplicate checks cannot race and cancel
cannot hand the printer to a new job while the old socket is still open.

Invariants: an accepted request is not a print; `sent` is set only after the transport finished
writing and settled; `physical_print_confirmed` is always False; any job that wrote bytes and then
failed or was cancelled is `delivery_uncertain`; errors expose a fixed message per code, never
exception text, payloads, tokens or addresses; logs carry job id, phase, counters, error code.
"""

import asyncio
import functools
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ._shared import att, bridge_wire, transport
from .link import LinkFactory

_LOGGER = logging.getLogger(__name__)

ERROR_MESSAGES = {
    "connect_timeout": "The printer did not accept an LE connection in time",
    "connect_failed": "The LE connection to the printer failed",
    "profile_unsupported": "The device does not expose the expected printer profile",
    "att_protocol_error": "The printer broke the ATT protocol or stopped answering",
    "link_lost": "The LE link was lost during the job",
    "write_failed": "Writing to the printer failed",
    "flow_pause_timeout": "The printer stayed in flow-control pause for too long",
    "transfer_timeout": "The transfer exceeded its time budget",
    "cancelled": "The job was cancelled",
    "internal_error": "Internal bridge error",
}
_ZERO_COUNTERS = {
    "bytes_written": 0,
    "frames_written": 0,
    "writes": 0,
    "notifications": 0,
    "flow_pauses": 0,
    "delivery_uncertain": False,
}


@dataclass
class _Job:
    job_id: str
    kind: str
    started: float
    state: str = "accepted"
    error_code: str | None = None
    counters: dict[str, Any] = field(default_factory=lambda: dict(_ZERO_COUNTERS))
    live: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    finished_at: float | None = None
    task: asyncio.Task[None] | None = None
    client: Any = None
    gatt: Any = None
    link_connect_seconds: float = 0.0
    finalized: bool = False
    cancel_requested: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def terminal(self) -> bool:
        return self.state in bridge_wire.TERMINAL_STATES

    def view(self, now: float) -> dict[str, Any]:
        counters = self.live if self.live is not None else self.counters
        written = counters["bytes_written"]
        uncertain = bool(counters["delivery_uncertain"]) or (
            self.state in ("failed", "cancelled") and written > 0
        )
        if self.state == "sent":
            confirmation = "transport_only" if self.kind == "print" else "connection_only"
        else:
            confirmation = "none"
        error = None
        if self.error_code is not None:
            error = {"code": self.error_code, "message": ERROR_MESSAGES[self.error_code]}
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state,
            "terminal": self.terminal,
            "bytes_written": written,
            "frames_written": counters["frames_written"],
            "writes": counters["writes"],
            "notifications": counters["notifications"],
            "flow_pauses": counters["flow_pauses"],
            "delivery_uncertain": uncertain,
            "confirmation": confirmation,
            "physical_print_confirmed": False,
            "elapsed_seconds": round((self.finished_at or now) - self.started, 3),
            "error": error,
            "diagnosis": self.diagnosis,
        }


class JobManager:
    def __init__(
        self,
        link: LinkFactory,
        *,
        max_retained: int = 32,
        max_seen_ids: int = 4096,
        cancel_wait: float = 15,
        connect_timeout: float = 35,
        discovery_timeout: float = 30,
        request_timeout: float = 10,
        send_timeout: float = 5,
        pacing: dict[str, float] | None = None,
    ) -> None:
        self._link = link
        self._max_retained = max_retained
        self._max_seen = max_seen_ids
        self._cancel_wait = cancel_wait
        self._connect_timeout = connect_timeout
        self._discovery_timeout = discovery_timeout
        self._request_timeout = request_timeout
        self._send_timeout = send_timeout
        self._pacing = dict(pacing or {})
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()
        # Ids cancelled before they were ever started live apart from `_seen`, so that blind
        # cancels cannot push the memory of real jobs (the replay guard) out of its ring.
        self._cancelled_early: OrderedDict[str, None] = OrderedDict()
        self._active: _Job | None = None
        self._closing = False

    # -- queries ---------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def active_job_id(self) -> str | None:
        return self._active.job_id if self._active else None

    def status(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "active_job_id": self.active_job_id,
            "retained_jobs": len(self._jobs),
        }

    def get(self, job_id: str) -> dict[str, Any]:
        return self._find(job_id).view(time.monotonic())

    def _find(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise bridge_wire.WireError("not_found", "No such job", 404)
        return job

    # -- starting --------------------------------------------------------------------------
    def start_print(self, request: Any) -> dict[str, Any]:
        job = self._begin(request.job_id, "print")
        self._spawn(job, functools.partial(self._print, job, request))
        return job.view(time.monotonic())

    def start_diagnose(self, job_id: str) -> dict[str, Any]:
        job = self._begin(job_id, "diagnose")
        self._spawn(job, functools.partial(self._diagnose, job))
        return job.view(time.monotonic())

    def _spawn(self, job: _Job, work: Callable[[], Awaitable[None]]) -> None:
        job.task = asyncio.create_task(self._run(job, work))
        job.task.add_done_callback(functools.partial(self._on_task_done, job))

    def _remember(self, job_id: str) -> None:
        self._seen[job_id] = None
        while len(self._seen) > self._max_seen:
            self._seen.popitem(last=False)

    def _burn(self, job_id: str) -> None:
        self._cancelled_early[job_id] = None
        while len(self._cancelled_early) > self._max_seen:
            self._cancelled_early.popitem(last=False)

    def _begin(self, job_id: str, kind: str) -> _Job:
        if self._closing:
            raise bridge_wire.WireError("busy", "The bridge is shutting down", 409)
        if job_id in self._seen or job_id in self._cancelled_early:
            raise bridge_wire.WireError(
                "duplicate_job", "This job id was already used or cancelled", 409
            )
        if self._active is not None:
            raise bridge_wire.WireError("busy", "Another job is using the printer", 409)
        self._remember(job_id)
        job = _Job(job_id, kind, time.monotonic())
        self._jobs[job_id] = job
        self._active = job
        _LOGGER.info("job=%s kind=%s state=accepted", job_id, kind)
        return job

    # -- running ---------------------------------------------------------------------------
    def _set_state(self, job: _Job, state: str) -> None:
        job.state = state
        _LOGGER.info("job=%s state=%s", job.job_id, state)

    def _terminal(self, job: _Job, state: str, error_code: str | None = None) -> None:
        if job.terminal:
            return
        job.state = state
        job.error_code = error_code
        job.finished_at = time.monotonic()
        if job.live is not None:
            job.counters = dict(job.live)
            job.live = None
        _LOGGER.info(
            "job=%s state=%s error=%s bytes=%s frames=%s writes=%s",
            job.job_id,
            state,
            error_code,
            job.counters["bytes_written"],
            job.counters["frames_written"],
            job.counters["writes"],
        )

    async def _run(self, job: _Job, work: Callable[[], Awaitable[None]]) -> None:
        try:
            await work()
        except asyncio.CancelledError:
            self._terminal(job, "cancelled", "cancelled")
        except Exception as error:
            _LOGGER.info("job=%s failed type=%s", job.job_id, type(error).__name__)
            self._terminal(job, "failed", self._classify(job, error))
        else:
            self._terminal(job, "sent")
        finally:
            self._finalize(job)

    def _finalize(self, job: _Job) -> None:
        """Release the hardware slot. Runs only once the worker (and its cleanup) ended."""
        if job.finalized:
            return
        job.finalized = True
        if not job.terminal:
            self._terminal(job, "cancelled", "cancelled")
        job.client = None
        job.gatt = None
        if self._active is job:
            self._active = None
        job.finished.set()
        terminal = [key for key, value in self._jobs.items() if value.terminal]
        for key in terminal[: max(0, len(terminal) - self._max_retained)]:
            del self._jobs[key]

    def _on_task_done(self, job: _Job, _task: asyncio.Task[None]) -> None:
        # Covers a task cancelled before its coroutine ever started (no try/finally ran).
        self._finalize(job)

    def _classify(self, job: _Job, error: Exception) -> str:
        reason = job.client.close_reason if job.client is not None else None
        if job.state == "connecting":
            if isinstance(error, TimeoutError):
                return "connect_timeout"
            return "connect_failed" if isinstance(error, OSError) else "internal_error"
        if isinstance(error, att.ProfileError | ValueError):
            return "profile_unsupported"
        if isinstance(error, att.AttTimeout):
            return "write_failed" if reason == "send_timeout" else "att_protocol_error"
        if isinstance(error, TimeoutError):
            if job.state == "transmitting":
                paused = job.gatt is not None and job.gatt.flow_paused
                return "flow_pause_timeout" if paused else "transfer_timeout"
            return "att_protocol_error"
        if isinstance(error, att.AttError):
            return "att_protocol_error"
        if isinstance(error, ConnectionError | EOFError | OSError):
            if reason in ("send_failed", "send_timeout"):
                return "write_failed"
            return "link_lost"
        return "internal_error"

    async def _open(self, job: _Job) -> tuple[Any, Any]:
        self._set_state(job, "connecting")
        began = time.monotonic()
        async with asyncio.timeout(self._connect_timeout):
            channel = await self._link.connect()
        job.link_connect_seconds = round(time.monotonic() - began, 3)
        client = att.AttClient(
            channel, request_timeout=self._request_timeout, send_timeout=self._send_timeout
        )
        job.client = client
        await client.start()
        self._set_state(job, "discovering")
        return channel, client

    async def _release(self, channel: Any, client: Any) -> None:
        """Close reader and socket, each bounded; never raises."""
        try:
            async with asyncio.timeout(5):
                if client is not None:
                    await client.aclose()
        except Exception as error:
            _LOGGER.debug("ATT cleanup failed: %s", type(error).__name__)
        if channel is not None:
            try:
                channel.close()
            except Exception as error:
                _LOGGER.debug("Channel close failed: %s", type(error).__name__)

    async def _print(self, job: _Job, request: Any) -> None:
        channel = client = None
        try:
            channel, client = await self._open(job)
            async with asyncio.timeout(self._discovery_timeout):
                profile = await att.discover_profile(client)
            gatt = att.AttGattClient(client, profile)
            job.gatt = gatt
            job.live = gatt.counters
            self._set_state(job, "transmitting")
            options = {"total_timeout": transport.transfer_timeout(request.rows)} | self._pacing
            await transport.send_bitmap(gatt, request.bitmap, request.rows, **options)
        finally:
            await self._release(channel, client)

    async def _diagnose(self, job: _Job) -> None:
        channel = client = None
        try:
            channel, client = await self._open(job)
            async with asyncio.timeout(self._discovery_timeout):
                survey = await att.discover_all(client)
            job.diagnosis = self._fit_diagnosis(
                {
                    "profile_supported": survey["profile_supported"],
                    "profile": survey["profile"],
                    "profile_problem": survey["profile_problem"],
                    "services": survey["services"],
                    "truncated": survey["truncated"],
                    "att_mtu": att.ATT_DEFAULT_MTU,
                    "link_connect_seconds": float(job.link_connect_seconds),
                    "stray_pdus": client.stats["stray_pdus"],
                }
            )
        finally:
            await self._release(channel, client)

    @staticmethod
    def _fit_diagnosis(diagnosis: dict[str, Any]) -> dict[str, Any]:
        """Keep the view inside the shared wire limit; shrink characteristics first."""
        if len(json.dumps(diagnosis)) <= bridge_wire.MAX_DIAGNOSIS_JSON:
            return diagnosis
        diagnosis["truncated"] = True
        diagnosis["services"] = [
            {"uuid": s["uuid"], "start": s["start"], "end": s["end"]} for s in diagnosis["services"]
        ]
        return diagnosis

    # -- cancelling ------------------------------------------------------------------------
    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel and wait (bounded) for the worker and its socket cleanup to finish."""
        job = self._jobs.get(job_id)
        if job is None:
            # The start request may still be on its way (a client that lost the answer cancels
            # blind): burn the id so that a late arrival is refused instead of printing.
            self._burn(job_id)
            raise bridge_wire.WireError("not_found", "No such job", 404)
        if job.terminal and job.finalized:
            return job.view(time.monotonic())
        await self._cancel_job(job)
        return job.view(time.monotonic())

    async def _cancel_job(self, job: _Job) -> None:
        task = job.task
        if task is not None and not job.cancel_requested and not task.done():
            # Once only: a second cancel would interrupt the cleanup the first one started.
            job.cancel_requested = True
            task.cancel()
        if task is not None:
            await asyncio.wait({task}, timeout=self._cancel_wait)
            if task.done():
                self._finalize(job)

    async def shutdown(self) -> None:
        self._closing = True
        job = self._active
        if job is not None:
            await self._cancel_job(job)
