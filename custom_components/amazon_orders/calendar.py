"""Calendar platform for the Amazon parcel tracker integration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from homeassistant.components.calendar import CalendarEvent, CalendarEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AmazonConfigEntry
from .account.coordinator import AmazonCoordinator
from .account.parcels import parse_iso
from .device import ATTRIBUTION, build_device_info

PARALLEL_UPDATES = 0

_DEFAULT_EVENT_DURATION = timedelta(hours=1)


def _event_summary(parcel: dict) -> str:
    raw = parcel.get("raw") or {}
    items = raw.get("items")
    if items and isinstance(items, list):
        max_len = 64 if len(items) == 1 else 32
        shortened = []
        for item in items:
            clean = str(item.get("title") if isinstance(item, dict) else item).strip()
            if len(clean) > max_len:
                clean = clean[:max_len].rstrip() + "…"
            shortened.append(f'"{clean}"')
        return f"📦 Amazon: {', '.join(shortened)}"
    sender = parcel.get("sender")
    barcode = parcel.get("barcode") or ""
    return sender or (f"📦 Amazon: Parcel {barcode}" if barcode else "📦 Amazon delivery")


def _event_bounds(
    start: datetime, end: datetime | None
) -> tuple[date | datetime, date | datetime]:
    # Check if this represents an all-day delivery (no specific hour window)
    # A whole-day date is stored with time.min (00:00:00) either in its native zone or local time
    is_midnight = start.time() == time.min or dt_util.as_local(start).time() == time.min
    if is_midnight and (
        end is None
        or end.time() in (time.min, time(23, 59, 59))
        or dt_util.as_local(end).time() in (time.min, time(23, 59, 59))
        or end.date() > start.date()
        or end <= start
    ):
        start_date = start.date()
        if end is None or end.date() <= start_date:
            end_date = start_date + timedelta(days=1)
        elif end.time() == time.min:
            end_date = end.date()
        else:
            end_date = end.date() + timedelta(days=1)
        return start_date, end_date

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
            summary = _event_summary(parcel)
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
