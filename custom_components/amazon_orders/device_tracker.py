"""Courier positions as device_tracker entities for native HA map support.

One entity per parcel whose courier Amazon is showing, created when the live
session starts and removed when it ends, so Home Assistant's native map panel
(/map/0) displays the delivery vehicle in real-time.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.location import distance

from . import AmazonConfigEntry
from .const import live_signal
from .device import ATTRIBUTION, build_device_info

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
        self._attr_device_info = build_device_info(entry)

    def _get_parcel(self) -> dict[str, Any] | None:
        """Find the matching parcel dict from the coordinator."""
        if hasattr(self._entry, "runtime_data") and self._entry.runtime_data:
            coordinator = getattr(self._entry.runtime_data, "coordinator", None)
            if coordinator and getattr(coordinator, "parcels", None):
                for p in coordinator.parcels:
                    if p.get("barcode") == self._barcode:
                        return p
        return None

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
        acc = self._live.get("accuracy")
        if isinstance(acc, (int, float)) and not isinstance(acc, bool):
            return float(acc)
        return 0.0

    @property
    def entity_picture(self) -> str | None:
        """Return courier vehicle icon or item image for map pin."""
        parcel = self._get_parcel()
        if not parcel:
            return None
        raw = parcel.get("raw") or {}
        if raw.get("image_url"):
            return raw.get("image_url")
        order_items = raw.get("order_items")
        if order_items and isinstance(order_items, list) and order_items[0].get("image_url"):
            return order_items[0].get("image_url")
        items = raw.get("items")
        if items and isinstance(items, list) and isinstance(items[0], dict) and items[0].get("image_url"):
            return items[0].get("image_url")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra state attributes for courier delivery status."""
        attrs: dict[str, Any] = {
            "barcode": self._barcode,
            "stops_away": self._live.get("stops_away"),
            "live_state": self._live.get("state"),
            "position_at": self._live.get("position_at"),
        }
        if (
            self.latitude is not None
            and self.longitude is not None
            and self.hass is not None
            and getattr(self.hass.config, "latitude", None) is not None
            and getattr(self.hass.config, "longitude", None) is not None
        ):
            metres = distance(
                self.hass.config.latitude,
                self.hass.config.longitude,
                self.latitude,
                self.longitude,
            )
            if metres is not None:
                attrs["distance_km"] = round(metres / 1000, 2)

        parcel = self._get_parcel()
        if parcel:
            raw = parcel.get("raw") or {}
            items = raw.get("items")
            if items and isinstance(items, list):
                if isinstance(items[0], str):
                    attrs["items"] = items
                    attrs["title"] = items[0]
                elif isinstance(items[0], dict):
                    attrs["items"] = items
                    attrs["title"] = items[0].get("title")
            elif raw.get("order_items"):
                attrs["items"] = raw["order_items"]
                attrs["title"] = raw["order_items"][0].get("title")

        return attrs
