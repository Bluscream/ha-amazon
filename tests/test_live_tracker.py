"""Tests for the Amazon LiveTracker."""

from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_orders.account.live import LiveReading, LiveState
from custom_components.amazon_orders.const import (
    CONF_LIVE_TRACKING,
    DOMAIN,
    ParcelStatus,
)
from custom_components.amazon_orders.live_tracker import LiveTracker, _wants_live


def _live_reading(lat=52.37, lon=4.9, state=LiveState.EN_ROUTE, stops=3):
    return LiveReading(
        state=state,
        stops_away=stops,
        latitude=lat,
        longitude=lon,
        accuracy=10.0,
        position_at="2026-11-02T12:00:00+00:00",
        message="3 stops away",
        raw_state="OUT_FOR_DELIVERY",
        trail=[],
    )


def _parcel(
    barcode="TBA123",
    carrier="Amazon",
    status=ParcelStatus.OUT_FOR_DELIVERY,
    delivered=False,
):
    return {
        "carrier": carrier,
        "barcode": barcode,
        "status": status,
        "delivered": delivered,
        "raw": {"page_state": {"mapTracking": True}},
    }


def test_wants_live():
    assert _wants_live(_parcel()) is True
    assert _wants_live(_parcel(carrier="DHL")) is False
    assert _wants_live(_parcel(delivered=True)) is False
    assert (
        _wants_live({"carrier": "Amazon", "status": ParcelStatus.IN_TRANSIT, "raw": {}})
        is False
    )


async def test_live_tracker_lifecycle_and_reading(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="amazon_de",
        options={CONF_LIVE_TRACKING: True},
    )
    entry.add_to_hass(hass)

    client = AsyncMock()
    client.can_read_live = MagicMock(return_value=True)
    client.async_read_live.return_value = _live_reading()

    coordinator = MagicMock()
    coordinator.data = []

    tracker = LiveTracker(hass, entry, client, coordinator)
    assert tracker.enabled is True

    parcels = [_parcel("TBA123")]
    tracker.async_apply(parcels)
    assert "TBA123" in tracker._candidates()

    # Read once
    await tracker.async_read()
    assert "TBA123" in tracker.readings
    assert parcels[0]["live"] is not None
    assert parcels[0]["live"]["stops_away"] == 3

    # An empty reading removes it
    client.async_read_live.return_value = None
    await tracker.async_read()
    assert "TBA123" not in tracker.readings

    tracker.async_stop()
