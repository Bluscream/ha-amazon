"""Calendar platform for the Amazon parcel tracker integration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AmazonConfigEntry
from .account.coordinator import AmazonCoordinator
from .account.parcels import items_label, parse_iso
from .device import ATTRIBUTION, build_device_info

PARALLEL_UPDATES = 0

_DEFAULT_EVENT_DURATION = timedelta(hours=1)
_LAST_SECOND = time(23, 59, 59)


def _event_bounds(
    start: datetime, end: datetime | None
) -> tuple[date | datetime, date | datetime]:
    """Return an all-day span for a window of whole days, else the timed one.

    A window naming only days runs from midnight to the last second (or the
    next midnight) in its own offset; shown as timed it would sit at midnight,
    or late the evening before for a viewer in another zone.
    """
    if (
        end is not None
        and end > start
        and start.time() == time.min
        and end.time() in (time.min, _LAST_SECOND)
    ):
        last = end.date() if end.time() == time.min else end.date() + timedelta(days=1)
        return start.date(), last
    if end is None or end <= start:
        end = start + _DEFAULT_EVENT_DURATION
    return start, end


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmazonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Amazon deliveries calendar from a config entry."""
    async_add_entities(
        [AmazonDeliveriesCalendar(entry.runtime_data.coordinator, entry)]
    )


class AmazonDeliveriesCalendar(CoordinatorEntity[AmazonCoordinator], CalendarEntity):
    """A read-only calendar of expected Amazon deliveries.

    Each active tracked parcel with a known delivery moment becomes an event.
    No extra API calls — a pure view over coordinator data — so it is enabled
    by default and can be turned off per entity if unwanted.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "deliveries"
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: AmazonCoordinator, entry: ConfigEntry) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deliveries"
        self._attr_device_info = build_device_info(entry)

    def _events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for parcel in self.coordinator.data or []:
            start = parse_iso(parcel.get("planned_from"))
            if start is None:
                continue
            end = parse_iso(parcel.get("planned_to"))
            event_start, event_end = _event_bounds(start, end)

            barcode = parcel.get("barcode") or ""
            summary = (
                items_label(parcel.get("items"))
                or parcel.get("sender")
                or (f"Parcel {barcode}" if barcode else "Amazon parcel")
            )
            description_parts = [
                f"Barcode: {barcode}" if barcode else None,
                f"Status: {parcel.get('status')}" if parcel.get("status") else None,
                parcel.get("url"),
            ]
            description = "\n".join(p for p in description_parts if p)
            location = parcel.get("pickup_point") if parcel.get("pickup") else None

            events.append(
                CalendarEvent(
                    start=event_start,
                    end=event_end,
                    summary=summary,
                    description=description or None,
                    location=location,
                    uid=barcode or None,
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming calendar event."""
        now = dt_util.now()
        upcoming = [event for event in self._events() if event.end_datetime_local > now]
        return (
            min(upcoming, key=lambda event: event.start_datetime_local)
            if upcoming
            else None
        )

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        return [
            event
            for event in self._events()
            if event.start_datetime_local < end_date
            and event.end_datetime_local > start_date
        ]
