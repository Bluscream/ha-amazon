"""Courier positions as device_tracker entities for native HA map support.

One entity per parcel whose courier Amazon is showing, created when the live
session starts and removed when it ends, so Home Assistant's native map panel
(/map/0) displays the delivery vehicle in real-time.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AmazonConfigEntry
from .const import live_signal
from .device import ATTRIBUTION

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmazonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Keep one courier tracker entity per live parcel."""
    tracker = entry.runtime_data.live
    couriers: dict[str, AmazonCourierTracker] = {}

    @callback
    def _sync() -> None:
        shown = {
            code: live
            for code, live in tracker.readings.items()
            if live.get("latitude") is not None and live.get("longitude") is not None
        }
        for code in [code for code in couriers if code not in shown]:
            hass.async_create_task(couriers.pop(code).async_remove(force_remove=True))
        new: list[AmazonCourierTracker] = []
        for code, live in shown.items():
            if code in couriers:
                couriers[code].async_update_live(live)
            else:
                couriers[code] = AmazonCourierTracker(entry, code, live)
                new.append(couriers[code])
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, live_signal(entry.entry_id), _sync)
    )
    _sync()


class AmazonCourierTracker(TrackerEntity):
    """Tracker entity representing the courier delivering a parcel."""

    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION
    _attr_icon = "mdi:truck-delivery"
    _attr_source_type = SourceType.GPS
    _unrecorded_attributes = frozenset({"latitude", "longitude", "position_at"})

    def __init__(
        self, entry: AmazonConfigEntry, barcode: str, live: dict[str, Any]
    ) -> None:
        """Initialise for one parcel's live courier session."""
        self._entry = entry
        self._barcode = barcode
        self._live = live
        self._attr_unique_id = f"{entry.entry_id}_courier_{barcode}"
        self._attr_name = f"Amazon courier {barcode}"

    @callback
    def async_update_live(self, live: dict[str, Any]) -> None:
        """Update live courier position."""
        if live == self._live:
            return
        self._live = live
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def latitude(self) -> float | None:
        """Courier latitude."""
        return self._live.get("latitude")

    @property
    def longitude(self) -> float | None:
        """Courier longitude."""
        return self._live.get("longitude")

    @property
    def location_accuracy(self) -> float:
        """Accuracy of GPS position in metres."""
        return 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra state attributes for courier delivery status."""
        return {
            "barcode": self._barcode,
            "stops_away": self._live.get("stops_away"),
            "live_state": self._live.get("state"),
            "position_at": self._live.get("position_at"),
        }
