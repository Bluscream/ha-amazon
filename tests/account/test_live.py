"""Tests for reading Amazon's courier map (parcel-live/1), pure parsing only."""

from datetime import datetime, timezone

import pytest

from custom_components.amazon_orders.account.live import (
    TRAIL_MAX_POINTS,
    LiveState,
    LiveTarget,
    live_target,
    parse_csrf_token,
    parse_location_state,
    request_form,
    response_code,
    strip_trail,
)
from custom_components.amazon_orders.account.pages import _page_state

from ..payloads import (
    ACTIVE_CODE,
    FAKE_CSRF,
    LIVE_SHIPMENT_ID,
    live_point,
    location_state,
    track_page,
)

TRACK_PATH = "/gp/your-account/ship-track?shipmentId=x"
READ_AT = datetime(2026, 11, 2, 17, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# what a request needs
# ---------------------------------------------------------------------------


def test_the_token_comes_from_the_page_script():
    assert parse_csrf_token(track_page(csrf_token=FAKE_CSRF)) == FAKE_CSRF
    assert parse_csrf_token('window.csrfToken = "' + FAKE_CSRF + '"') == FAKE_CSRF


@pytest.mark.parametrize(
    "page",
    [
        track_page(),
        "window.csrfToken = 'short'",
        '"aapi":{"csrfToken":"9@not-the-page-one"}',
    ],
)
def test_no_token_without_the_page_script(page):
    assert parse_csrf_token(page) is None


def test_a_target_needs_the_tracking_id_and_the_token():
    page = track_page(csrf_token=FAKE_CSRF, shipment_id=LIVE_SHIPMENT_ID)
    target = live_target(_page_state(page), page, TRACK_PATH)
    assert target == LiveTarget(ACTIVE_CODE, LIVE_SHIPMENT_ID, FAKE_CSRF, TRACK_PATH)

    no_token = track_page()
    assert live_target(_page_state(no_token), no_token, TRACK_PATH) is None
    no_id = track_page(csrf_token=FAKE_CSRF, tracking_id=None)
    assert live_target(_page_state(no_id), no_id, TRACK_PATH) is None


def test_the_request_mirrors_the_page_and_counts_up():
    target = LiveTarget(ACTIVE_CODE, LIVE_SHIPMENT_ID, FAKE_CSRF, TRACK_PATH)
    assert request_form(target, 3) == {
        "trackingId": ACTIVE_CODE,
        "timezone": "GMT",
        "csrfToken": FAKE_CSRF,
        "numberOfMemoizations": "3",
        "shipmentId": LIVE_SHIPMENT_ID,
    }
    bare = LiveTarget(ACTIVE_CODE, None, FAKE_CSRF, TRACK_PATH)
    assert "shipmentId" not in request_form(bare, 0)


# ---------------------------------------------------------------------------
# what the endpoint answers
# ---------------------------------------------------------------------------


def test_a_reading_has_the_newest_position_and_the_trail():
    payload = location_state(live_point(2), live_point(0), live_point(1))
    reading = parse_location_state(payload)

    assert reading is not None
    assert reading.state is LiveState.EN_ROUTE
    assert reading.stops_away == 4
    assert (reading.latitude, reading.longitude) == (54.502, 3.502)
    assert reading.accuracy == 8.0
    assert reading.position_at == "2026-11-02T17:02:00+00:00"
    assert reading.message == "4 stops away"
    assert reading.raw_state == "OUT_FOR_DELIVERY"
    assert [p["timestamp"] for p in reading.trail] == [
        "2026-11-02T17:00:00+00:00",
        "2026-11-02T17:01:00+00:00",
        "2026-11-02T17:02:00+00:00",
    ]
    assert set(reading.trail[0]) == {"latitude", "longitude", "timestamp"}


def test_the_published_live_dict_follows_the_contract():
    reading = parse_location_state(location_state(live_point(0)))
    assert reading is not None
    live = reading.as_live(READ_AT)
    assert list(live) == [
        "state",
        "stops_away",
        "latitude",
        "longitude",
        "accuracy",
        "position_at",
        "message",
        "trail",
        "updated_at",
    ]
    assert live["state"] == "en_route"
    assert live["updated_at"] == "2026-11-02T17:05:00+00:00"
    assert strip_trail(live)["trail"] == []
    assert live["trail"]  # the original keeps its trail
    assert strip_trail(None) is None


def test_the_trail_keeps_only_the_newest_points():
    points = [live_point(minute) for minute in range(TRAIL_MAX_POINTS + 5)]
    reading = parse_location_state(location_state(*points))
    assert reading is not None
    assert len(reading.trail) == TRAIL_MAX_POINTS
    assert reading.trail[0]["timestamp"] == "2026-11-02T17:05:00+00:00"


@pytest.mark.parametrize(
    "point",
    [
        "not a point",
        {"latitude": 0, "longitude": 0, "epochTimestamp": 1},  # the map's "no fix"
        {"latitude": 91.0, "longitude": 3.5, "epochTimestamp": 1},
        {"latitude": 54.5, "longitude": float("nan"), "epochTimestamp": 1},
        {"latitude": True, "longitude": 3.5, "epochTimestamp": 1},
        {"latitude": 54.5, "longitude": 3.5},
        {"latitude": 54.5, "longitude": 3.5, "epochTimestamp": True},
        {"latitude": 54.5, "longitude": 3.5, "epochTimestamp": 1e20},
    ],
)
def test_unusable_points_are_skipped(point):
    reading = parse_location_state(location_state(point, live_point(1)))
    assert reading is not None
    assert len(reading.trail) == 1


@pytest.mark.parametrize("accuracy", [None, -1, float("inf"), True, "8"])
def test_an_odd_accuracy_is_left_out(accuracy):
    point = {**live_point(0), "accuracy": accuracy}
    reading = parse_location_state(location_state(point))
    assert reading is not None
    assert reading.accuracy is None


def test_a_reading_without_a_position_still_counts_the_stops():
    reading = parse_location_state(location_state(callout="2 Stopps entfernt"))
    assert reading is not None
    assert reading.latitude is None and reading.trail == []
    assert reading.stops_away == 2


@pytest.mark.parametrize(
    "map_state,stops",
    [
        ({"stopsRemaining": 7}, 7),
        ({"stops": "5"}, 5),
        ({"numberOfStops": 3}, 3),
        ({"stopsRemaining": -1, "callout": "6 arrêts"}, 6),
        ({"stopsRemaining": True, "callout": "6 paradas restantes"}, 6),
        ({"callout": "Nog 9 stops"}, 9),
        ({"callout": "Out for delivery"}, None),
        ({"callout": None}, None),
    ],
)
def test_stops_come_from_the_state_else_the_callout(map_state, stops):
    callout = map_state.pop("callout", "")
    payload = location_state(live_point(0), callout=callout, **map_state)
    reading = parse_location_state(payload)
    assert reading is not None
    assert reading.stops_away == stops


def test_next_stop_means_no_stops_left():
    reading = parse_location_state(
        location_state(live_point(0), status="YOU_ARE_NEXT", callout="You're next")
    )
    assert reading is not None
    assert reading.state is LiveState.NEXT_STOP
    assert reading.stops_away == 0


@pytest.mark.parametrize(
    "status,state",
    [
        ("SKIPPED_STOP", LiveState.EN_ROUTE),
        ("DELIVERED", LiveState.DELIVERED),
        ("DELIVERY_ATTEMPTED", LiveState.ENDED),
        (None, LiveState.EN_ROUTE),
    ],
)
def test_map_states(status, state):
    reading = parse_location_state(location_state(live_point(0), status=status))
    assert reading is not None
    assert reading.state is state


def test_an_unknown_map_state_warns_once_and_reads_as_on_the_way(caplog):
    for _ in range(2):
        reading = parse_location_state(location_state(live_point(0), status="HOVERING"))
        assert reading is not None
        assert reading.state is LiveState.EN_ROUTE
    assert caplog.text.count('"HOVERING"') == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"success": False, "responseCode": "SERVICE_FAILED"},
        {"success": True},
        {"success": True, "value": "nope"},
        location_state(status=None, callout=None),
    ],
)
def test_no_reading_without_anything_to_show(payload):
    assert parse_location_state(payload) is None


def test_a_non_dict_map_state_counts_as_empty():
    payload = location_state(live_point(0))
    payload["value"]["mapState"] = "nope"
    reading = parse_location_state(payload)
    assert reading is not None
    assert reading.raw_state is None and reading.message is None


def test_the_response_code_of_a_refusal():
    assert response_code({"success": False, "responseCode": "INVALID_TOKEN"}) == (
        "INVALID_TOKEN"
    )
    assert response_code({"success": True}) is None
    assert response_code(None) is None
