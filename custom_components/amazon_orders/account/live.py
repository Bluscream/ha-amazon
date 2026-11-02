"""Read Amazon's courier map: what a tracking page needs and what it answers.

Pure: no I/O, no Home Assistant objects. While Amazon's own fleet delivers a
parcel, its tracking page polls a location endpoint for the courier's recent
positions and a map state (the courier's status and Amazon's own callout,
"4 stops away"). This module turns one such answer into the suite's
``parcel-live/1`` shape (see the fork's PARCEL_EXTENSIONS.md): a ``live`` dict
beside the canonical parcel keys. The destination Amazon also knows is
deliberately never read: the home location is the dashboard's to know.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .parcels import warn_once

LIVE_STATE_PATH = "/progress-tracker/package/actions/package-location/get-state"
TRAIL_MAX_POINTS = 30

# The token the tracking page hands its own scripts; the location endpoint
# rejects a request without it.
_CSRF_RE = re.compile(r"""window\.csrfToken\s*=\s*(['"])([^'"\s]{8,})\1""")
# "4 stops away", "4 Stopps entfernt", "4 arrêts", "4 fermate", "4 paradas":
# Amazon's callout in the account's language; the number before the word.
_STOPS_RE = re.compile(
    r"(\d{1,3})\s*(?:stops?|stopps?|haltes?|arr[eê]ts?|fermate|fermata|paradas?|przystank\w*)\b",
    re.IGNORECASE,
)
# Response codes after which a fresh tracking page (new token) is worth a try.
STALE_TOKEN_CODES = frozenset(
    {"INVALID_TOKEN", "UNSTRUSTED_REQUEST", "NOT_AUTHENTICATED"}
)


class LiveState(StrEnum):
    """Where a live delivery session stands (parcel-live/1)."""

    EN_ROUTE = "en_route"
    NEXT_STOP = "next_stop"
    DELIVERED = "delivered"
    ENDED = "ended"


# Amazon map states, as the tracking page's own script names them. The page
# keeps drawing the courier for the first group, reloads on a delivery and
# removes the courier for the rest.
_STATES = {
    "RENDERED": LiveState.EN_ROUTE,
    "OUT_FOR_DELIVERY": LiveState.EN_ROUTE,
    "PICKED_UP": LiveState.EN_ROUTE,
    "SKIPPED_STOP": LiveState.EN_ROUTE,
    "YOU_ARE_NEXT": LiveState.NEXT_STOP,
    "DELIVERED": LiveState.DELIVERED,
    "DELIVERED_CONFIRMED": LiveState.DELIVERED,
    "PARTIALLY_DELIVERED": LiveState.ENDED,
    "DELIVERY_ATTEMPTED": LiveState.ENDED,
    "DELIVERY_FAILED": LiveState.ENDED,
    "NOT_DELIVERED": LiveState.ENDED,
    "REJECTED": LiveState.ENDED,
    "ADDRESS_NOT_FOUND": LiveState.ENDED,
    "FATAL": LiveState.ENDED,
    "ERROR": LiveState.ENDED,
}


@dataclass(frozen=True, slots=True)
class LiveTarget:
    """What one location request for one parcel needs, read off its tracking page."""

    tracking_id: str
    shipment_id: str | None
    csrf_token: str
    track_path: str


@dataclass(frozen=True, slots=True)
class LiveReading:
    """One answer of the location endpoint, already in the suite's terms."""

    state: LiveState
    stops_away: int | None
    latitude: float | None
    longitude: float | None
    accuracy: float | None
    position_at: str | None
    message: str | None
    raw_state: str | None
    trail: list[dict[str, Any]] = field(default_factory=list)

    def as_live(self, read_at: datetime) -> dict[str, Any]:
        """Return the ``live`` dict a parcel publishes (parcel-live/1)."""
        return {
            "state": str(self.state),
            "stops_away": self.stops_away,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy": self.accuracy,
            "position_at": self.position_at,
            "message": self.message,
            "trail": list(self.trail),
            "updated_at": read_at.isoformat(),
        }


def parse_csrf_token(page: str) -> str | None:
    """Return the token a tracking page gives its own scripts, if it has one."""
    match = _CSRF_RE.search(page)
    return html.unescape(match.group(2)) if match else None


def live_target(state: dict[str, Any], page: str, track_path: str) -> LiveTarget | None:
    """Return what a location request needs, or ``None`` if the page lacks it."""
    tracking_id = state.get("trackingId")
    token = parse_csrf_token(page)
    if not isinstance(tracking_id, str) or not tracking_id or token is None:
        return None
    shipment = state.get("shipmentId")
    return LiveTarget(
        tracking_id=tracking_id,
        shipment_id=shipment if isinstance(shipment, str) and shipment else None,
        csrf_token=token,
        track_path=track_path,
    )


def request_form(target: LiveTarget, polls: int) -> dict[str, str]:
    """Form fields of one location request, as the tracking page sends them."""
    form = {
        "trackingId": target.tracking_id,
        "timezone": "GMT",
        "csrfToken": target.csrf_token,
        "numberOfMemoizations": str(polls),
    }
    if target.shipment_id:
        form["shipmentId"] = target.shipment_id
    return form


def _coordinate(value: object, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > limit or number == 0.0:
        return None
    return number


def _epoch(value: object) -> datetime | None:
    """Read a millisecond epoch, the unit the page's own map script uses."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _trail(points: object) -> list[dict[str, Any]]:
    """Return the courier's recent positions, oldest first, capped."""
    if not isinstance(points, list):
        return []
    trail: list[tuple[datetime, dict[str, Any]]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        latitude = _coordinate(point.get("latitude"), 90)
        longitude = _coordinate(point.get("longitude"), 180)
        moment = _epoch(point.get("epochTimestamp"))
        if latitude is None or longitude is None or moment is None:
            continue
        accuracy = point.get("accuracy")
        trail.append(
            (
                moment,
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "timestamp": moment.isoformat(),
                    "accuracy": float(accuracy)
                    if isinstance(accuracy, int | float)
                    and not isinstance(accuracy, bool)
                    and math.isfinite(accuracy)
                    and accuracy >= 0
                    else None,
                },
            )
        )
    trail.sort(key=lambda item: item[0])
    return [point for _, point in trail[-TRAIL_MAX_POINTS:]]


def _stops(
    map_state: dict[str, Any], state: LiveState, message: str | None
) -> int | None:
    for key in ("stopsRemaining", "stops", "numberOfStops"):
        value = map_state.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    if state is LiveState.NEXT_STOP:
        return 0
    match = _STOPS_RE.search(message or "")
    return int(match.group(1)) if match else None


def _state(raw_state: str | None) -> LiveState:
    if raw_state is None:
        return LiveState.EN_ROUTE
    state = _STATES.get(raw_state)
    if state is None:
        warn_once(
            f"live-state={raw_state}",
            f'Amazon\'s courier map reported the state "{raw_state}", which is not'
            " mapped yet, so the courier is shown as on the way; please share it.",
        )
        return LiveState.EN_ROUTE
    return state


def parse_location_state(payload: object) -> LiveReading | None:
    """Turn one location-endpoint answer into a reading, ``None`` if it has none.

    A successful answer without any position is still a reading (Amazon
    sometimes knows the stop count before it shares the courier's position).
    """
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    value = payload.get("value")
    if not isinstance(value, dict):
        return None
    map_state = value.get("mapState")
    map_state = map_state if isinstance(map_state, dict) else {}
    raw_state = map_state.get("status")
    raw_state = raw_state if isinstance(raw_state, str) and raw_state else None
    message = map_state.get("calloutMessage")
    message = html.unescape(message).strip() if isinstance(message, str) else None
    state = _state(raw_state)
    trail = _trail(value.get("packageLocationTimeline"))
    if not trail and raw_state is None and not message:
        return None
    last = trail[-1] if trail else None
    return LiveReading(
        state=state,
        stops_away=_stops(map_state, state, message),
        latitude=last["latitude"] if last else None,
        longitude=last["longitude"] if last else None,
        accuracy=last["accuracy"] if last else None,
        position_at=last["timestamp"] if last else None,
        message=message or None,
        raw_state=raw_state,
        trail=[
            {k: p[k] for k in ("latitude", "longitude", "timestamp")} for p in trail
        ],
    )


def response_code(payload: object) -> str | None:
    """Return the response code a failed answer names, if any."""
    if isinstance(payload, dict) and isinstance(
        code := payload.get("responseCode"), str
    ):
        return code
    return None


def strip_trail(live: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``live`` without its trail, for event payloads."""
    return None if live is None else {**live, "trail": []}
