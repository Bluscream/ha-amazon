"""Tests for Amazon sensor property logic."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.amazon_orders.const import ParcelStatus
from custom_components.amazon_orders.sensor import (
    AmazonAwaitingPickupSensor,
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
    raw: dict | None = None,
) -> dict:
    return {
        "carrier": "Amazon",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
        "raw": raw or {},
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = AmazonIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel(
        "A",
        status=ParcelStatus.OUT_FOR_DELIVERY,
        raw={
            "items": ["First Item That Is Quite Long And Will Be Truncated", "Second Item"],
            "image_url": "https://m.media-amazon.com/images/I/sample.jpg",
        },
    )
    sensor = AmazonParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.entity_picture == "https://m.media-amazon.com/images/I/sample.jpg"
    assert sensor.extra_state_attributes["barcode"] == "A"
    assert sensor.translation_placeholders == {
        "barcode": 'with "First Item That Is Quite Long An…", "Second Item"'
    }


def test_parcel_sensor_tiled_entity_picture():
    img1 = "https://m.media-amazon.com/images/I/item1.jpg"
    img2 = "https://m.media-amazon.com/images/I/item2.jpg"
    parcel = _parcel(
        "A",
        status=ParcelStatus.OUT_FOR_DELIVERY,
        raw={
            "items": ["Item 1", "Item 2"],
            "image_urls": [img1, img2],
            "order_items": [
                {"title": "Item 1", "image_url": img1},
                {"title": "Item 2", "image_url": img2},
            ],
        },
    )
    sensor = AmazonParcelSensor(_coordinator([parcel]), _entry(), "A")
    picture = sensor.entity_picture
    assert picture is not None
    assert picture.startswith("data:image/svg+xml;utf8,")
    assert "topleft" in picture
    assert "bottomright" in picture
    assert "item1.jpg" in picture
    assert "item2.jpg" in picture


def test_parcel_sensor_single_item_truncation():
    # 80 chars - should not be truncated because single item limit is 128
    title_80 = "Veet Expert Hair Removal Cream Sensitive Skin with Shea Butter & Tested Formula"
    parcel = _parcel(
        "A",
        raw={"items": [title_80]},
    )
    sensor = AmazonParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.translation_placeholders == {
        "barcode": f'with "{title_80}"'
    }

    # 140 chars - should be truncated to 128 chars + ellipsis
    long_title = "A" * 140
    parcel_long = _parcel(
        "B",
        raw={"items": [long_title]},
    )
    sensor_long = AmazonParcelSensor(_coordinator([parcel_long]), _entry(), "B")
    expected_title = ("A" * 128) + "…"
    assert sensor_long.translation_placeholders == {
        "barcode": f'with "{expected_title}"'
    }


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
