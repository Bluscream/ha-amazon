"""Courier positions as geo_location entities (parcel-live/1).

One entity per parcel whose courier Amazon is showing, created when the live
session starts and removed when it ends, so the built-in map card can show
them with ``geo_location_sources: [amazon_orders]``. The coordinates are left
out of the recorder: a courier's path ends at the user's door.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.location import distance

from . import AmazonConfigEntry
from .const import DOMAIN, live_signal
from .device import ATTRIBUTION

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmazonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Keep one courier entity per live parcel."""
    tracker = entry.runtime_data.live
    couriers: dict[str, AmazonCourierLocation] = {}

    @callback
    def _sync() -> None:
        shown = {
            code: live
            for code, live in tracker.readings.items()
            if live.get("latitude") is not None and live.get("longitude") is not None
        }
        for code in [code for code in couriers if code not in shown]:
            hass.async_create_task(couriers.pop(code).async_remove(force_remove=True))
        new: list[AmazonCourierLocation] = []
        for code, live in shown.items():
            if code in couriers:
                couriers[code].async_update_live(live)
            else:
                couriers[code] = AmazonCourierLocation(code, live)
                new.append(couriers[code])
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, live_signal(entry.entry_id), _sync)
    )
    _sync()


class AmazonCourierLocation(GeolocationEvent):
    """Where the courier carrying one parcel is."""

    _attr_should_poll = False
    _attr_source = DOMAIN
    _attr_attribution = ATTRIBUTION
    _attr_icon = "mdi:truck-delivery"
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _unrecorded_attributes = frozenset({"latitude", "longitude", "position_at"})

    def __init__(self, barcode: str, live: dict[str, Any]) -> None:
        """Initialise for one parcel's live session."""
        self._barcode = barcode
        self._live = live
        self._attr_name = f"Amazon courier {barcode}"

    @callback
    def async_update_live(self, live: dict[str, Any]) -> None:
        """Take a new reading and write the state once it changed."""
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
    def distance(self) -> float | None:
        """Kilometres from home, rounded to ten metres."""
        if self.latitude is None or self.longitude is None or self.hass is None:
            return None
        metres = distance(
            self.hass.config.latitude,
            self.hass.config.longitude,
            self.latitude,
            self.longitude,
        )
        return None if metres is None else round(metres / 1000, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The parcel and how far along its delivery is."""
        return {
            "barcode": self._barcode,
            "stops_away": self._live.get("stops_away"),
            "live_state": self._live.get("state"),
            "position_at": self._live.get("position_at"),
        }
