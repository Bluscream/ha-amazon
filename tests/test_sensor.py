"""Tests for Amazon sensor property logic."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.amazon_orders.const import ParcelStatus
from custom_components.amazon_orders.sensor import (
    AmazonAwaitingPickupSensor,
    AmazonCourierStopsAwaySensor,
    AmazonDeliveredParcelsSensor,
    AmazonEnRouteToPickupPointSensor,
    AmazonIncomingParcelsSensor,
    AmazonLastUpdateSensor,
    AmazonNextDeliverySensor,
    AmazonParcelSensor,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict], delivered: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "Amazon",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = AmazonIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = AmazonParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = AmazonParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="2026-05-02T10:00:00Z"),
            _parcel("B", planned_from="2026-05-01T10:00:00Z"),
        ]
    )
    sensor = AmazonNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_none_without_moments():
    sensor = AmazonNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_unparseable_moment():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="not-a-date"),
            _parcel("B", planned_from="2026-05-01T10:00:00Z"),
        ]
    )
    sensor = AmazonNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_en_route_and_awaiting_pickup_split():
    parcels = [
        _parcel("EN_ROUTE", pickup=True),
        _parcel("READY", pickup=True, status=ParcelStatus.AT_PICKUP_POINT),
        _parcel("HOME"),
    ]
    en_route = AmazonEnRouteToPickupPointSensor(_coordinator(parcels), _entry())
    awaiting = AmazonAwaitingPickupSensor(_coordinator(parcels), _entry())
    assert en_route.unique_id == "e1_en_route_to_pickup_point"
    assert awaiting.unique_id == "e1_awaiting_pickup"
    assert [p["barcode"] for p in en_route.extra_state_attributes["parcels"]] == [
        "EN_ROUTE"
    ]
    assert [p["barcode"] for p in awaiting.extra_state_attributes["parcels"]] == [
        "READY"
    ]
    assert en_route.native_value == 1
    assert awaiting.native_value == 1


def test_pickup_sensors_zero_without_data():
    coordinator = _coordinator([])
    coordinator.data = None
    assert AmazonEnRouteToPickupPointSensor(coordinator, _entry()).native_value == 0
    assert AmazonAwaitingPickupSensor(coordinator, _entry()).native_value == 0


def test_delivered_sensor():
    coordinator = _coordinator(
        [], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)]
    )
    sensor = AmazonDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    coordinator.last_success_time = moment
    sensor = AmazonLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment


def _items(*titles: str, image: str | None = None) -> list[dict]:
    return [
        {"title": title, "image_url": image if index == 0 else None}
        for index, title in enumerate(titles)
    ]


def test_parcel_sensor_is_named_after_its_items_once_added():
    parcel = {**_parcel("A"), "items": _items("Widget", "Gadget")}
    sensor = AmazonParcelSensor(_coordinator([parcel]), _entry(), "A")
    # Until the entity has its id, the translated barcode name stands.
    assert not hasattr(sensor, "_attr_name")
    assert sensor.translation_placeholders == {"barcode": "A"}

    sensor._name_after_items()
    assert sensor.name == "Widget, Gadget"


def test_parcel_sensor_keeps_its_last_name_when_a_poll_has_no_items():
    parcel = {**_parcel("A"), "items": _items("Widget")}
    coordinator = _coordinator([parcel])
    sensor = AmazonParcelSensor(coordinator, _entry(), "A")
    sensor._name_after_items()
    coordinator.data = [{**_parcel("A"), "items": None}]
    sensor._name_after_items()
    assert sensor.name == "Widget"


def test_parcel_sensor_without_items_keeps_the_barcode_name():
    sensor = AmazonParcelSensor(_coordinator([_parcel("A")]), _entry(), "A")
    sensor._name_after_items()
    assert not hasattr(sensor, "_attr_name")


def test_parcel_sensor_picture_is_the_first_items_image():
    with_image = {
        **_parcel("A"),
        "items": _items("Widget", "Gadget", image="https://x/1.jpg"),
    }
    without = {**_parcel("B"), "items": _items("Widget")}
    assert (
        AmazonParcelSensor(_coordinator([with_image]), _entry(), "A").entity_picture
        == "https://x/1.jpg"
    )
    assert (
        AmazonParcelSensor(_coordinator([without]), _entry(), "B").entity_picture
        is None
    )
    assert AmazonParcelSensor(_coordinator([]), _entry(), "C").entity_picture is None


def _live(state: str, stops: int | None, message: str = "") -> dict:
    return {"state": state, "stops_away": stops, "message": message}


def test_courier_stops_away_takes_the_nearest_courier_on_the_road():
    parcels = [
        {**_parcel("FAR"), "live": _live("en_route", 7)},
        {**_parcel("NEAR"), "live": _live("next_stop", 0, "You're next")},
        {**_parcel("DONE"), "live": _live("delivered", None)},
        {**_parcel("NONE"), "live": None},
    ]
    sensor = AmazonCourierStopsAwaySensor(_coordinator(parcels), _entry())
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes == {
        "barcode": "NEAR",
        "live_state": "next_stop",
        "message": "You're next",
    }


def test_courier_stops_away_is_unknown_without_a_courier():
    sensor = AmazonCourierStopsAwaySensor(
        _coordinator([{**_parcel("A"), "live": _live("en_route", None)}]), _entry()
    )
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}
