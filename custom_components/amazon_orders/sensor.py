"""Sensor platform for the Amazon parcel tracker integration."""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AmazonConfigEntry
from .account.coordinator import AmazonCoordinator
from .account.parcels import parse_iso
from .const import DOMAIN, ParcelStatus
from .device import ATTRIBUTION, build_device_info

_LOGGER = logging.getLogger(__name__)

# The DataUpdateCoordinator handles fan-out to all entities; HA's per-entity
# update throttling adds nothing here.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmazonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Amazon sensor entities from a config entry."""
    # The coordinator is already refreshed by __init__.py before platforms are
    # forwarded, so ConfigEntryNotReady is raised from the entry setup rather
    # than (too late) from this forwarded platform.
    coordinator = entry.runtime_data.coordinator

    current_barcodes: set[str] = {p.get("barcode", "") for p in coordinator.data or []}
    entry_id = entry.entry_id

    # Remove per-parcel sensors from the registry whose barcode is no longer
    # active (e.g. the code was removed, or the parcel was delivered between
    # restarts). Scoped to the sensor domain so it never touches the refresh
    # button or the diagnostic last-update sensor.
    registry = er.async_get(hass)
    non_parcel_unique_ids = {
        f"{entry_id}_incoming_parcels",
        f"{entry_id}_next_delivery",
        f"{entry_id}_en_route_to_pickup_point",
        f"{entry_id}_awaiting_pickup",
        f"{entry_id}_delivered_parcels",
        f"{entry_id}_last_update",
        f"{entry_id}_courier_stops_away",
    }
    for entity_entry in er.async_entries_for_config_entry(registry, entry_id):
        if (
            entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{entry_id}_")
            and entity_entry.unique_id not in non_parcel_unique_ids
        ):
            barcode = entity_entry.unique_id[len(f"{entry_id}_") :]
            if barcode not in current_barcodes:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        AmazonIncomingParcelsSensor(
            coordinator, entry, async_add_entities, current_barcodes
        ),
    ]
    for parcel in coordinator.data or []:
        entities.append(
            AmazonParcelSensor(coordinator, entry, parcel.get("barcode", ""))
        )
    entities.append(AmazonNextDeliverySensor(coordinator, entry))
    entities.append(AmazonEnRouteToPickupPointSensor(coordinator, entry))
    entities.append(AmazonAwaitingPickupSensor(coordinator, entry))
    entities.append(AmazonDeliveredParcelsSensor(coordinator, entry))
    entities.append(AmazonCourierStopsAwaySensor(coordinator, entry))
    entities.append(AmazonLastUpdateSensor(coordinator, entry))

    async_add_entities(entities)


class AmazonIncomingParcelsSensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Summary sensor: count of active (not-yet-delivered) tracked parcels.

    Spawns a per-parcel sensor for each new barcode and removes stale ones
    from the registry (via the registry, not self-removal, to avoid the ghost
    entity race).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: AmazonCoordinator,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
        known_barcodes: set[str] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._async_add_entities = async_add_entities
        self._attr_unique_id = f"{entry.entry_id}_incoming_parcels"
        self._attr_device_info = build_device_info(entry)
        self._known_barcodes: set[str] = known_barcodes or set()

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self.coordinator.data or []}

    def _handle_coordinator_update(self) -> None:
        current_barcodes: set[str] = {
            p.get("barcode", "") for p in (self.coordinator.data or [])
        }

        new_barcodes = current_barcodes - self._known_barcodes
        if new_barcodes:
            self._async_add_entities(
                AmazonParcelSensor(self.coordinator, self._entry, barcode)
                for barcode in new_barcodes
            )

        removed_barcodes = self._known_barcodes - current_barcodes
        if removed_barcodes:
            registry = er.async_get(self.hass)
            for barcode in removed_barcodes:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{self._entry.entry_id}_{barcode}"
                )
                if entity_id:
                    registry.async_remove(entity_id)

        self._known_barcodes = current_barcodes
        super()._handle_coordinator_update()


def build_tiled_image_svg(image_urls: list[str]) -> str | None:
    """Generate an SVG data URI collage for multiple item images.

    - 1 image: return the URL directly.
    - 2 images: diagonal split from bottom-left (0, 256) to top-right (256, 0).
    - 3 images: top half split vertically, bottom half full width.
    - 4+ images: 2x2 grid.
    """
    valid = [u for u in image_urls if u and isinstance(u, str)]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]

    u1 = valid[0]
    u2 = valid[1]

    if len(valid) == 2:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="256" height="256" viewBox="0 0 256 256">'
            '<defs>'
            '<clipPath id="topleft"><polygon points="0,0 256,0 0,256"/></clipPath>'
            '<clipPath id="bottomright"><polygon points="256,0 256,256 0,256"/></clipPath>'
            '</defs>'
            f'<image href="{u1}" width="256" height="256" preserveAspectRatio="xMidYMid slice" clip-path="url(#topleft)"/>'
            f'<image href="{u2}" width="256" height="256" preserveAspectRatio="xMidYMid slice" clip-path="url(#bottomright)"/>'
            '<line x1="0" y1="256" x2="256" y2="0" stroke="#ffffff" stroke-width="3"/>'
            '</svg>'
        )
    elif len(valid) == 3:
        u3 = valid[2]
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="256" height="256" viewBox="0 0 256 256">'
            '<defs>'
            '<clipPath id="c1"><rect x="0" y="0" width="128" height="128"/></clipPath>'
            '<clipPath id="c2"><rect x="128" y="0" width="128" height="128"/></clipPath>'
            '<clipPath id="c3"><rect x="0" y="128" width="256" height="128"/></clipPath>'
            '</defs>'
            f'<image href="{u1}" x="0" y="0" width="128" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c1)"/>'
            f'<image href="{u2}" x="128" y="0" width="128" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c2)"/>'
            f'<image href="{u3}" x="0" y="128" width="256" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c3)"/>'
            '<line x1="128" y1="0" x2="128" y2="128" stroke="#ffffff" stroke-width="2"/>'
            '<line x1="0" y1="128" x2="256" y2="128" stroke="#ffffff" stroke-width="2"/>'
            '</svg>'
        )
    else:
        u3 = valid[2]
        u4 = valid[3]
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="256" height="256" viewBox="0 0 256 256">'
            '<defs>'
            '<clipPath id="c1"><rect x="0" y="0" width="128" height="128"/></clipPath>'
            '<clipPath id="c2"><rect x="128" y="0" width="128" height="128"/></clipPath>'
            '<clipPath id="c3"><rect x="0" y="128" width="128" height="128"/></clipPath>'
            '<clipPath id="c4"><rect x="128" y="128" width="128" height="128"/></clipPath>'
            '</defs>'
            f'<image href="{u1}" x="0" y="0" width="128" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c1)"/>'
            f'<image href="{u2}" x="128" y="0" width="128" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c2)"/>'
            f'<image href="{u3}" x="0" y="128" width="128" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c3)"/>'
            f'<image href="{u4}" x="128" y="128" width="128" height="128" preserveAspectRatio="xMidYMid slice" clip-path="url(#c4)"/>'
            '<line x1="128" y1="0" x2="128" y2="256" stroke="#ffffff" stroke-width="2"/>'
            '<line x1="0" y1="128" x2="256" y2="128" stroke="#ffffff" stroke-width="2"/>'
            '</svg>'
        )

    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


class AmazonParcelSensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Per-parcel sensor reporting the status of one tracked Amazon parcel."""

    _attr_has_entity_name = True
    _attr_translation_key = "parcel"
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"raw", "history", "live"})

    def __init__(
        self, coordinator: AmazonCoordinator, entry: ConfigEntry, barcode: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._barcode = barcode
        self._attr_unique_id = f"{entry.entry_id}_{barcode}"
        self._attr_translation_placeholders = {"barcode": barcode}
        self._attr_device_info = build_device_info(entry)
        self._update_placeholders()

    def _get_parcel(self) -> dict[str, Any] | None:
        for parcel in self.coordinator.data or []:
            if parcel.get("barcode") == self._barcode:
                return parcel
        return None

    def _update_placeholders(self) -> None:
        parcel = self._get_parcel()
        items = (parcel.get("raw") or {}).get("items") if parcel else None
        if items and isinstance(items, list):
            max_len = 128 if len(items) == 1 else 32
            shortened = []
            for item in items:
                clean = str(item).strip()
                if len(clean) > max_len:
                    clean = clean[:max_len].rstrip() + "…"
                shortened.append(f'"{clean}"')
            new_placeholders = {
                "barcode": f"{self._barcode} ({', '.join(shortened)})"
            }
        else:
            new_placeholders = {"barcode": self._barcode}

        if getattr(self, "_attr_translation_placeholders", None) != new_placeholders:
            self._attr_translation_placeholders = new_placeholders
            # Invalidate cached name and friendly_name if calculated
            self.__dict__.pop("name", None)
            self._cached_friendly_name = None
            if self.hass and self.registry_entry:
                registry = er.async_get(self.hass)
                expected_original_name = self.name
                if (
                    expected_original_name
                    and expected_original_name != self.registry_entry.original_name
                ):
                    self.registry_entry = registry.async_update_entity(
                        self.entity_id,
                        original_name=expected_original_name,
                    )

    async def async_added_to_hass(self) -> None:
        """Sync dynamic item names into registry when added to hass."""
        await super().async_added_to_hass()
        self._update_placeholders()

    def _handle_coordinator_update(self) -> None:
        self._update_placeholders()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> str | None:
        """Return the native value of the sensor."""
        parcel = self._get_parcel()
        return parcel.get("status") if parcel else None

    @property
    def entity_picture(self) -> str | None:
        """Return the product image URL (or tiled collage if multiple items), or delivery photo if delivered."""
        parcel = self._get_parcel()
        if not parcel:
            return None
        raw = parcel.get("raw") or {}
        image_urls = raw.get("image_urls")
        if not image_urls:
            order_items = raw.get("order_items")
            if order_items and isinstance(order_items, list):
                image_urls = [
                    item.get("image_url")
                    for item in order_items
                    if isinstance(item, dict) and item.get("image_url")
                ]
        if image_urls and len(image_urls) > 1:
            tiled = build_tiled_image_svg(image_urls)
            if tiled:
                return tiled

        return (
            raw.get("image_url")
            or raw.get("delivery_photo_url")
            or parcel.get("delivery_photo_url")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        parcel = self._get_parcel()
        return dict(parcel) if parcel else {}


class AmazonNextDeliverySensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Earliest expected delivery datetime across all active parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_delivery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: AmazonCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_delivery"
        self._attr_device_info = build_device_info(entry)

    def _delivery_moments(self) -> list[tuple[datetime, dict]]:
        result: list[tuple[datetime, dict]] = []
        for parcel in self.coordinator.data or []:
            moment = parse_iso(parcel.get("planned_from"))
            if moment is None:
                if parcel.get("planned_from"):
                    _LOGGER.debug(
                        "Could not parse delivery moment: %s", parcel["planned_from"]
                    )
                continue
            result.append((moment, parcel))
        return result

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        moments = self._delivery_moments()
        return min(dt for dt, _ in moments) if moments else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        moments = self._delivery_moments()
        if not moments:
            return {}
        _, earliest = min(moments, key=lambda x: x[0])
        return {
            "barcode": earliest.get("barcode"),
            "sender": earliest.get("sender"),
            "receiver": earliest.get("receiver"),
        }


class _AmazonPickupListSensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Base for the canonical pickup-point summary views."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})
    _suffix: str

    def __init__(self, coordinator: AmazonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._suffix}"
        self._attr_device_info = build_device_info(entry)

    def _parcels(self) -> list[dict]:
        raise NotImplementedError

    @property
    def native_value(self) -> int:
        return len(self._parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self._parcels()}


class AmazonEnRouteToPickupPointSensor(_AmazonPickupListSensor):
    """Active parcels destined for, but not yet at, a pickup point."""

    _attr_translation_key = "en_route_to_pickup_point"
    _suffix = "en_route_to_pickup_point"

    def _parcels(self) -> list[dict]:
        return [
            parcel
            for parcel in (self.coordinator.data or [])
            if parcel.get("pickup")
            and parcel.get("status") != ParcelStatus.AT_PICKUP_POINT
        ]


class AmazonAwaitingPickupSensor(_AmazonPickupListSensor):
    """Active parcels that have arrived at a pickup point."""

    _attr_translation_key = "awaiting_pickup"
    _suffix = "awaiting_pickup"

    def _parcels(self) -> list[dict]:
        return [
            parcel
            for parcel in (self.coordinator.data or [])
            if parcel.get("status") == ParcelStatus.AT_PICKUP_POINT
        ]


class AmazonDeliveredParcelsSensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Recently delivered tracked Amazon parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: AmazonCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_delivered_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self.coordinator.delivered)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self.coordinator.delivered}


class AmazonCourierStopsAwaySensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Fewest stops a courier still has before one of the parcels (parcel-live/1)."""

    _attr_has_entity_name = True
    _attr_translation_key = "courier_stops_away"
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: AmazonCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_courier_stops_away"
        self._attr_device_info = build_device_info(entry)

    def _nearest(self) -> dict[str, Any] | None:
        counted = [
            parcel
            for parcel in self.coordinator.data or []
            if (live := parcel.get("live"))
            and live.get("state") in ("en_route", "next_stop")
            and live.get("stops_away") is not None
        ]
        return min(counted, key=lambda p: p["live"]["stops_away"], default=None)

    @property
    def native_value(self) -> int | None:
        """Return the native value of the sensor."""
        parcel = self._nearest()
        return parcel["live"]["stops_away"] if parcel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the parcel the count is for."""
        parcel = self._nearest()
        if parcel is None:
            return {}
        return {
            "barcode": parcel.get("barcode"),
            "live_state": parcel["live"].get("state"),
            "message": parcel["live"].get("message"),
        }


class AmazonLastUpdateSensor(CoordinatorEntity[AmazonCoordinator], SensorEntity):
    """Diagnostic sensor reporting when Amazon was last polled successfully."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: AmazonCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_update"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        return self.coordinator.last_success_time
