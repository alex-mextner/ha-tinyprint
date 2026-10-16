"""Bridge transport behaviour for the printer runtime, kept out of studio_runtime.py.

Mixed into StudioPrinter. Invariants: a selected-but-broken bridge fails closed (never native
BLE); status checks are HTTP only and cached (never a radio connect); the URL and token never
leave `self.bridge`.
"""

from datetime import datetime, timezone
from time import monotonic
from typing import Any

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bridge_client import BridgeClient, BridgeError, BridgeJobError, run_bridge_job

BRIDGE_MISCONFIGURED = (
    "Bridge transport is selected but not configured correctly; open the TinyPrint options and "
    "fix the bridge URL and token. Nothing was printed and Bluetooth was not used"
)


class BridgeTransportMixin:
    """Expects TinyPrintPrinter attributes: hass, bridge, transport, bridge_state, ..."""

    _bridge_client: BridgeClient | None = None
    _bridge_status_at: float | None = None
    bridge_status: dict[str, Any] | None = None

    def bridge_client(self) -> BridgeClient:
        if self.bridge is None:
            raise ServiceValidationError(BRIDGE_MISCONFIGURED)
        if self._bridge_client is None:
            self._bridge_client = BridgeClient(async_get_clientsession(self.hass), self.bridge)
        return self._bridge_client

    def _note_bridge_state(self, state: str) -> None:
        self.bridge_state = state
        self.bridge_checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def async_bridge_status(self, max_age: float = 0) -> dict[str, Any] | None:
        """Cheap bridge reachability (HTTP status only, never a radio connect), cached."""
        if self.bridge is None:
            return None
        if self._bridge_status_at is not None and monotonic() - self._bridge_status_at < max_age:
            return self.bridge_status
        try:
            self.bridge_status = await self.bridge_client().status()
            self._note_bridge_state("busy" if self.bridge_status["busy"] else "reachable")
        except BridgeError as error:
            self.bridge_status = None
            self._note_bridge_state(
                "auth_failed" if error.code == "unauthorized" else "unreachable"
            )
        self._bridge_status_at = monotonic()
        return self.bridge_status

    async def async_cancel_bridge_orphan(self) -> dict[str, str]:
        """Cancel a bridge job nobody here owns (e.g. after an HA restart). Bounded, explicit."""
        if self.bridge is None:
            return {"bridge_cancel": "not_applicable"}
        client = self.bridge_client()
        try:
            status = await client.status()
            job_id = status["active_job_id"]
            if not status["busy"] or not job_id:
                return {"bridge_cancel": "none_active"}
            view = await client.cancel(job_id)
        except BridgeError:
            return {"bridge_cancel": "failed"}
        return {"bridge_cancel": "acknowledged" if view["terminal"] else "pending"}

    def _require_bridge(self) -> None:
        # Fail closed: a selected-but-broken bridge must never fall back to local Bluetooth.
        if self.transport == "bridge" and self.bridge is None:
            raise ServiceValidationError(BRIDGE_MISCONFIGURED)

    async def _run_bridge_job(
        self, kind: str, bitmap: bytes | None = None, rows: int | None = None
    ) -> dict[str, Any]:
        try:
            result = await run_bridge_job(self, self.bridge_client(), kind, bitmap, rows)
        except BridgeJobError as error:
            if error.code == "unauthorized":
                self._note_bridge_state("auth_failed")
            elif error.code == "unreachable":
                self._note_bridge_state("unreachable")
            raise
        self._note_bridge_state("reachable")
        return result
