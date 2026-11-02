"""Tests for following Amazon's courier map between full polls (parcel-live/1)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.amazon_orders.account.coordinator import AmazonCoordinator
from custom_components.amazon_orders.account.errors import (
    AmazonApiError,
    AmazonAuthError,
)
from custom_components.amazon_orders.account.live import LiveReading, LiveState
from custom_components.amazon_orders.const import (
    CONF_LIVE_TRACKING,
    DOMAIN,
    EVENT_PARCEL_LIVE_UPDATED,
    LIVE_BACKOFF_CAP_SECONDS,
    LIVE_INTERVAL_SECONDS,
    LIVE_PROBE_SECONDS,
    ParcelStatus,
)
from custom_components.amazon_orders.live_tracker import LiveTracker, _wants_live

CODE = "TBA000000000001"


def _reading(state: LiveState = LiveState.EN_ROUTE, stops: int | None = 3):
    return LiveReading(
        state=state,
        stops_away=stops,
        latitude=54.5,
        longitude=3.5,
        accuracy=8.0,
        position_at="2026-11-02T17:00:00+00:00",
        message=f"{stops} stops away",
        raw_state="OUT_FOR_DELIVERY",
        trail=[{"latitude": 54.5, "longitude": 3.5, "timestamp": "t"}],
    )


def _parcel(
    code: str = CODE,
    *,
    carrier: str = "Amazon",
    status: ParcelStatus = ParcelStatus.OUT_FOR_DELIVERY,
    page_state: dict | None = None,
) -> dict:
    return {
        "carrier": carrier,
        "barcode": code,
        "status": status,
        "delivered": status is ParcelStatus.DELIVERED,
        "live": None,
        "raw": {"page_state": page_state or {}},
    }


@pytest.fixture
def setup(hass):
    """A tracker over a real coordinator whose client is scripted."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_LIVE_TRACKING: True})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.can_read_live = MagicMock(return_value=True)
    client.async_read_live = AsyncMock(return_value=_reading())
    coordinator = AmazonCoordinator(hass, client, entry)
    coordinator.async_request_refresh = AsyncMock()
    tracker = coordinator.live = LiveTracker(hass, entry, client, coordinator)
    yield entry, client, coordinator, tracker
    tracker.async_stop()


def _publish(coordinator: AmazonCoordinator, tracker: LiveTracker, *parcels: dict):
    coordinator.data = list(parcels)
    tracker.async_apply(coordinator.data)


def test_only_amazons_own_fleet_out_for_delivery_or_with_a_map_is_followed():
    assert _wants_live(_parcel())
    assert _wants_live(
        _parcel(status=ParcelStatus.IN_TRANSIT, page_state={"mapTracking": {"x": 1}})
    )
    assert not _wants_live(_parcel(status=ParcelStatus.IN_TRANSIT))
    assert not _wants_live(_parcel(carrier="DHL"))
    assert not _wants_live(_parcel(status=ParcelStatus.DELIVERED))


async def test_a_reading_lands_on_the_parcel_and_announces_the_session(hass, setup):
    _, _, coordinator, tracker = setup
    events = async_capture_events(hass, EVENT_PARCEL_LIVE_UPDATED)
    _publish(coordinator, tracker, _parcel())

    await tracker.async_read()
    await hass.async_block_till_done()

    live = coordinator.data[0]["live"]
    assert live["state"] == "en_route" and live["stops_away"] == 3
    (event,) = events
    assert event.data["barcode"] == CODE
    assert (event.data["old_live_state"], event.data["new_live_state"]) == (
        None,
        "en_route",
    )
    assert (event.data["old_stops_away"], event.data["new_stops_away"]) == (None, 3)
    # The event row stays small: no trail, while the parcel keeps it.
    assert event.data["live"]["trail"] == []
    assert live["trail"]


async def test_only_a_new_state_or_stop_count_is_announced(hass, setup):
    _, client, coordinator, tracker = setup
    events = async_capture_events(hass, EVENT_PARCEL_LIVE_UPDATED)
    _publish(coordinator, tracker, _parcel())

    await tracker.async_read()  # starts: announced
    await tracker.async_read()  # same state and stops, courier moved: silent
    client.async_read_live.return_value = _reading(stops=2)
    await tracker.async_read()  # one stop closer: announced
    client.async_read_live.return_value = _reading(LiveState.NEXT_STOP, 0)
    await tracker.async_read()  # next stop: announced
    await hass.async_block_till_done()

    assert [e.data["new_stops_away"] for e in events] == [3, 2, 0]


async def test_a_vanished_courier_is_dropped_and_announced(hass, setup):
    _, client, coordinator, tracker = setup
    events = async_capture_events(hass, EVENT_PARCEL_LIVE_UPDATED)
    _publish(coordinator, tracker, _parcel())
    await tracker.async_read()

    client.async_read_live.return_value = None
    await tracker.async_read()
    await hass.async_block_till_done()

    assert coordinator.data[0]["live"] is None
    assert events[-1].data["new_live_state"] is None
    assert events[-1].data["live"] is None


async def test_a_delivery_asks_for_one_full_poll_and_stops_reading(setup):
    _, client, coordinator, tracker = setup
    _publish(coordinator, tracker, _parcel())
    await tracker.async_read()

    client.async_read_live.return_value = _reading(LiveState.DELIVERED, None)
    await tracker.async_read()
    coordinator.async_request_refresh.assert_awaited_once()

    # The session is over: no more reads, no second refresh.
    client.async_read_live.reset_mock()
    await tracker.async_read()
    client.async_read_live.assert_not_awaited()
    coordinator.async_request_refresh.assert_awaited_once()


async def test_a_fresh_poll_keeps_readings_of_parcels_still_listed(setup):
    _, _, coordinator, tracker = setup
    _publish(coordinator, tracker, _parcel(), _parcel("OTHER", carrier="DHL"))
    await tracker.async_read()

    _publish(coordinator, tracker, _parcel(), _parcel("OTHER", carrier="DHL"))
    assert coordinator.data[0]["live"]["stops_away"] == 3
    assert coordinator.data[1]["live"] is None

    _publish(coordinator, tracker, _parcel("OTHER", carrier="DHL"))
    assert tracker.readings == {}


async def test_parcels_without_the_map_target_are_not_read(setup):
    _, client, coordinator, tracker = setup
    client.can_read_live.return_value = False
    _publish(coordinator, tracker, _parcel(), {**_parcel(), "barcode": None})
    await tracker.async_read()
    client.async_read_live.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        AmazonAuthError("signed out"),
        AmazonApiError("HTTP 429", status_code=429),
        aiohttp.ClientError("offline"),
        TimeoutError(),
    ],
)
async def test_a_failed_read_keeps_the_last_reading_and_backs_off(setup, error):
    _, client, coordinator, tracker = setup
    _publish(coordinator, tracker, _parcel())
    await tracker.async_read()

    client.async_read_live.side_effect = error
    with patch(
        "custom_components.amazon_orders.live_tracker.async_call_later"
    ) as call_later:
        await tracker.async_read()
        await tracker.async_read()
    assert coordinator.data[0]["live"]["stops_away"] == 3
    delays = [call.args[1] for call in call_later.call_args_list]
    assert delays == [LIVE_INTERVAL_SECONDS * 2, LIVE_INTERVAL_SECONDS * 4]

    client.async_read_live.side_effect = None
    with patch(
        "custom_components.amazon_orders.live_tracker.async_call_later"
    ) as call_later:
        await tracker.async_read()
    assert call_later.call_args.args[1] == LIVE_INTERVAL_SECONDS


async def test_the_back_off_is_capped(setup):
    _, client, coordinator, tracker = setup
    client.async_read_live.side_effect = TimeoutError()
    _publish(coordinator, tracker, _parcel())
    with patch(
        "custom_components.amazon_orders.live_tracker.async_call_later"
    ) as call_later:
        for _ in range(12):
            await tracker.async_read()
    assert call_later.call_args.args[1] == LIVE_BACKOFF_CAP_SECONDS


async def test_the_timer_probes_slowly_until_a_courier_shows_then_reads_often(
    hass, setup
):
    _, client, coordinator, tracker = setup
    client.async_read_live.return_value = None
    _publish(coordinator, tracker, _parcel())

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=LIVE_PROBE_SECONDS - 5)
    )
    await hass.async_block_till_done()
    client.async_read_live.assert_not_awaited()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=LIVE_PROBE_SECONDS + 1)
    )
    await hass.async_block_till_done()
    assert client.async_read_live.await_count == 1

    client.async_read_live.return_value = _reading()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=2 * LIVE_PROBE_SECONDS + 2)
    )
    await hass.async_block_till_done()
    assert client.async_read_live.await_count == 2
    async_fire_time_changed(
        hass,
        dt_util.utcnow()
        + timedelta(seconds=2 * LIVE_PROBE_SECONDS + LIVE_INTERVAL_SECONDS + 3),
    )
    await hass.async_block_till_done()
    assert client.async_read_live.await_count == 3


async def test_turned_off_or_nothing_out_nothing_is_scheduled(hass, setup):
    entry, _, coordinator, tracker = setup
    with patch(
        "custom_components.amazon_orders.live_tracker.async_call_later"
    ) as call_later:
        _publish(coordinator, tracker, _parcel(status=ParcelStatus.IN_TRANSIT))
        hass.config_entries.async_update_entry(
            entry, options={CONF_LIVE_TRACKING: False}
        )
        _publish(coordinator, tracker, _parcel())
    call_later.assert_not_called()
    assert tracker.enabled is False


async def test_a_parcel_event_for_a_parcel_no_longer_listed_is_dropped(hass, setup):
    _, _, coordinator, _ = setup
    events = async_capture_events(hass, EVENT_PARCEL_LIVE_UPDATED)
    coordinator.data = []
    coordinator.async_fire_parcel_event(CODE, {}, EVENT_PARCEL_LIVE_UPDATED)
    await hass.async_block_till_done()
    assert events == []
