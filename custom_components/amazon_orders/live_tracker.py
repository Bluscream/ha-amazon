"""Follow Amazon's courier map for parcels out for delivery (parcel-live/1).

The coordinator reads the whole account every 15 minutes at best; a courier
moves faster than that. While a parcel Amazon's own fleet carries is out for
delivery, this tracker asks the location endpoint the tracking page itself
uses, once a minute while the courier is shown and every five minutes until
then. Each read updates the parcel's ``live`` dict in place, so it costs no
full poll; a session ending in a delivery asks the coordinator for one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .account.client import AmazonClient
from .account.errors import AmazonApiError, AmazonAuthError
from .account.live import LiveState, strip_trail
from .const import (
    CONF_LIVE_TRACKING,
    DEFAULT_LIVE_TRACKING,
    EVENT_PARCEL_LIVE_UPDATED,
    LIVE_BACKOFF_CAP_SECONDS,
    LIVE_INTERVAL_SECONDS,
    LIVE_PROBE_SECONDS,
    ParcelStatus,
)

if TYPE_CHECKING:
    from .account.coordinator import AmazonCoordinator

_LOGGER = logging.getLogger(__name__)

_ON_THE_ROAD = frozenset({LiveState.EN_ROUTE, LiveState.NEXT_STOP})
# Only Amazon's own fleet has the courier map; a parcel a third-party carrier
# delivers is that carrier's integration's to follow.
_OWN_FLEET = "Amazon"


def _wants_live(parcel: dict[str, Any]) -> bool:
    """Whether Amazon may be showing a courier for this parcel."""
    if parcel.get("carrier") != _OWN_FLEET or parcel.get("delivered"):
        return False
    state = (parcel.get("raw") or {}).get("page_state") or {}
    return parcel.get("status") is ParcelStatus.OUT_FOR_DELIVERY or bool(
        state.get("mapTracking")
    )


class LiveTracker:
    """Reads the courier map of one config entry's parcels on its own timer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AmazonClient,
        coordinator: AmazonCoordinator,
    ) -> None:
        """Initialise for one entry's client and coordinator."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._coordinator = coordinator
        # barcode -> the parcel's ``live`` dict, kept across full polls.
        self.readings: dict[str, dict[str, Any]] = {}
        self._parcels: list[dict[str, Any]] = []
        self._unsub: CALLBACK_TYPE | None = None
        self._failures = 0

    @property
    def enabled(self) -> bool:
        """Whether the user wants the courier followed."""
        return bool(self._entry.options.get(CONF_LIVE_TRACKING, DEFAULT_LIVE_TRACKING))

    @callback
    def async_apply(self, parcels: list[dict[str, Any]]) -> None:
        """Attach the readings to a fresh poll's active parcels and re-arm."""
        self._parcels = parcels
        active = {parcel.get("barcode") for parcel in parcels}
        self.readings = {
            code: live for code, live in self.readings.items() if code in active
        }
        for parcel in parcels:
            parcel["live"] = self.readings.get(parcel.get("barcode") or "")
        self._reschedule()

    @callback
    def async_stop(self) -> None:
        """Cancel the timer (entry unload)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _candidates(self) -> list[str]:
        codes: list[str] = []
        for parcel in self._parcels:
            code = parcel.get("barcode")
            if not code or not self._client.can_read_live(code):
                continue
            live = self.readings.get(code)
            if live is not None and live["state"] not in _ON_THE_ROAD:
                continue  # the session ended; the next poll takes it from here
            if live is not None or _wants_live(parcel):
                codes.append(code)
        return codes

    @callback
    def _reschedule(self) -> None:
        self.async_stop()
        if not self.enabled or not self._candidates():
            return
        on_the_road = any(
            live["state"] in _ON_THE_ROAD for live in self.readings.values()
        )
        delay = LIVE_INTERVAL_SECONDS if on_the_road else LIVE_PROBE_SECONDS
        delay = min(delay * 2**self._failures, LIVE_BACKOFF_CAP_SECONDS)
        self._unsub = async_call_later(self._hass, delay, self._on_timer)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._unsub = None
        self._entry.async_create_background_task(
            self._hass, self.async_read(), f"{self._entry.entry_id} live read"
        )

    async def async_read(self) -> None:
        """Read every candidate once, publish the result and re-arm."""
        read_at = dt_util.utcnow()
        delivered = False
        try:
            for code in self._candidates():
                delivered |= await self._read_one(code, read_at)
        except AmazonAuthError:
            # The full poll owns sign-in trouble; wait for it, more slowly.
            self._failures += 1
        except (AmazonApiError, aiohttp.ClientError, TimeoutError) as err:
            self._failures += 1
            _LOGGER.debug("Reading the courier map failed: %s", err)
        else:
            self._failures = 0
        for parcel in self._parcels:
            parcel["live"] = self.readings.get(parcel.get("barcode") or "")
        self._coordinator.async_update_listeners()
        if delivered:
            await self._coordinator.async_request_refresh()
        self._reschedule()

    async def _read_one(self, code: str, read_at: datetime) -> bool:
        """Read one parcel; return whether its session just ended."""
        reading = await self._client.async_read_live(code)
        old = self.readings.get(code)
        if reading is None:
            if old is not None:
                self.readings.pop(code)
                self._announce(code, old, None)
            return False
        live = reading.as_live(read_at)
        self.readings[code] = live
        self._announce(code, old, live)
        return live["state"] not in _ON_THE_ROAD and (
            old is None or old["state"] in _ON_THE_ROAD
        )

    def _announce(
        self, code: str, old: dict[str, Any] | None, new: dict[str, Any] | None
    ) -> None:
        """Fire the live event when a session starts, changes state or stops."""
        old_state = old["state"] if old else None
        new_state = new["state"] if new else None
        old_stops = old["stops_away"] if old else None
        new_stops = new["stops_away"] if new else None
        if old_state == new_state and old_stops == new_stops:
            return
        self._coordinator.async_fire_parcel_event(
            code,
            {
                "live": strip_trail(new),
                "old_live_state": old_state,
                "new_live_state": new_state,
                "old_stops_away": old_stops,
                "new_stops_away": new_stops,
            },
            EVENT_PARCEL_LIVE_UPDATED,
        )
