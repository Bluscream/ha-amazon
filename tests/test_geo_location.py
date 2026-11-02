"""Tests for Amazon courier geo_location entities."""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import UnitOfLength
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_orders.const import DOMAIN, live_signal
from custom_components.amazon_orders.geo_location import (
    AmazonCourierLocation,
    async_setup_entry,
)


def _live_dict(lat=52.37, lon=4.9, state="en_route", stops=3):
    return {
        "state": state,
        "stops_away": stops,
        "latitude": lat,
        "longitude": lon,
        "accuracy": 10.0,
        "position_at": "2026-11-02T12:00:00+00:00",
        "message": "3 stops away",
        "trail": [],
        "updated_at": "2026-11-02T12:00:15+00:00",
    }


async def test_geo_location_properties(hass):
    hass.config.latitude = 52.36
    hass.config.longitude = 4.89

    live = _live_dict()
    courier = AmazonCourierLocation("TBA123", live)
    courier.hass = hass
    courier.entity_id = "geo_location.amazon_courier_tba123"

    assert courier.name == "Amazon courier TBA123"
    assert courier.unit_of_measurement == UnitOfLength.KILOMETERS
    assert courier.latitude == 52.37
    assert courier.longitude == 4.9
    assert courier.distance is not None
    assert courier.extra_state_attributes["stops_away"] == 3
    assert courier.extra_state_attributes["barcode"] == "TBA123"
    assert courier.extra_state_attributes["live_state"] == "en_route"

    # Update with changed location
    new_live = _live_dict(lat=52.365, lon=4.895, stops=1)
    courier.async_update_live(new_live)
    assert courier.latitude == 52.365
    assert courier.extra_state_attributes["stops_away"] == 1


async def test_geo_location_setup_sync(hass):
    hass.config.latitude = 52.36
    hass.config.longitude = 4.89

    entry = MockConfigEntry(domain=DOMAIN, unique_id="amazon_de")
    entry.add_to_hass(hass)

    tracker = MagicMock()
    tracker.readings = {"TBA123": _live_dict()}

    runtime_data = MagicMock()
    runtime_data.live = tracker
    entry.runtime_data = runtime_data

    added = []

    def async_add_entities(entities):
        for e in entities:
            e.hass = hass
            e.async_remove = AsyncMock()
        added.extend(entities)

    await async_setup_entry(hass, entry, async_add_entities)
    await hass.async_block_till_done()

    assert len(added) == 1
    assert added[0].name == "Amazon courier TBA123"

    # End the live session
    tracker.readings = {}
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    async_dispatcher_send(hass, live_signal(entry.entry_id))
    await hass.async_block_till_done()
