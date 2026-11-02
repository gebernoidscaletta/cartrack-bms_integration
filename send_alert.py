from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from config import PO_ID, get_bms_credentials, get_cartrack_credentials

BMS_BASE_URL = "https://api-bms.iassupport.id/index.php"
CARTRACK_BASE_URL = "https://fleetapi-id.cartrack.com/rest"

TZ = ZoneInfo("Asia/Jakarta")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 1000

POLL_INTERVAL = 1800 
OVERLAP = timedelta(minutes=1) 
MAX_LOOKBACK = timedelta(hours=1)
SEEN_LIMIT = 10_000
TOKEN_MARGIN = timedelta(minutes=1)

BMS_SUCCESS_CODES = (0, 200)
ALERT_NAME = "Geofence BMS - API"
ROUTE_ORIGIN = "CGK"

CHECKPOINTS = {
    ("pengendapan", "entered"): "check_in",
    ("pengendapan", "left"): "check_out",
    ("terminal_1", "entered"): "t1_in",
    ("terminal_1", "left"): "t1_out",
    ("terminal_2", "entered"): "t2_in",
    ("terminal_2", "left"): "t2_out",
    ("terminal_3", "entered"): "t3_in",
    ("terminal_3", "left"): "t3_out",
}

# "D7935AJ has entered Terminal_1-Bandara_Soekarno_Hatta at 2026-11-01 22:39:01"
MESSAGE_PATTERN = re.compile(
    r"^(?P<reg>\S+) has (?P<action>entered|left) (?P<place>[^\s-]+)\S* at (?P<ts>.+)$"
)

logger = logging.getLogger(__name__)


def parse_checkpoint(message: str) -> str | None:
    """'... has left Terminal_1-Bandara ...' -> 't1_out'. None if unknown."""
    match = MESSAGE_PATTERN.match(message or "")
    if not match:
        return None
    return CHECKPOINTS.get((match["place"].lower(), match["action"]))


def parse_route(description: object) -> str | None:
    """'CGK-Bandung' -> 'Bandung'. None unless the format is exactly CGK-<city>."""
    if not isinstance(description, str):
        return None
    origin, sep, city = description.partition("-")
    city = city.strip()
    if sep and origin.strip().upper() == ROUTE_ORIGIN and city:
        return city
    return None


class CartrackClient:
    def __init__(self) -> None:
        credentials = get_cartrack_credentials()
        self._session = requests.Session()
        self._session.auth = (credentials["username"], credentials["password"])
        self._session.headers["Accept"] = "application/json"

    def _get(self, path: str, params: dict) -> dict:
        response = self._session.get(
            f"{CARTRACK_BASE_URL}{path}", params=params
        )
        response.raise_for_status()
        return response.json()

    def get_alerts(self, date_from: str, date_to: str) -> list[dict]:
        alerts: list[dict] = []
        page = 1
        while True:
            payload = self._get(
                "/alerts/notifications",
                {
                    "filter[date_from]": date_from,
                    "filter[date_to]": date_to,
                    "filter[alert_type]": "ZONE_ARRIVE_DEPART",
                    "filter[contact_type]": "RSS Feeds",
                    "sort_by": "event_ts",
                    "sort_order": "asc",
                    "page": page,
                    "limit": PAGE_LIMIT,
                },
            )
            alerts.extend(payload.get("data") or [])
            last_page = (payload.get("meta") or {}).get("last_page", 1)
            if page >= last_page:
                return alerts
            page += 1

    def get_route(self, registration: str) -> str | None:
        payload = self._get("/vehicles", {"filter[registration]": registration})
        vehicles = payload.get("data") or []
        if isinstance(vehicles, dict):
            vehicles = [vehicles]
        if not vehicles:
            return None
        return parse_route(vehicles[0].get("client_vehicle_description2"))


class BMSClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=TZ)

    def _token_is_valid(self) -> bool:
        return (
            self._token is not None
            and datetime.now(TZ) < self._expires_at - TOKEN_MARGIN
        )

    def _authenticate(self) -> None:
        response = self._session.post(
            f"{BMS_BASE_URL}/auth/api_ext",
            json=get_bms_credentials(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"BMS auth failed: {payload.get('message')}")
        data = payload["data"]
        self._token = data["token"]
        self._expires_at = datetime.strptime(data["expired"], TS_FORMAT).replace(
            tzinfo=TZ
        )
        logger.info("BMS token refreshed, expires at %s", data["expired"])

    def _post_checkpoint(self, payload: dict) -> requests.Response:
        if not self._token_is_valid():
            self._authenticate()
        return self._session.post(
            f"{BMS_BASE_URL}/info/pidsCheckPoint",
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=REQUEST_TIMEOUT,
        )

    def send_checkpoint(self, payload: dict) -> dict:
        response = self._post_checkpoint(payload)
        if response.status_code == 401:
            self._token = None
            response = self._post_checkpoint(payload)
        response.raise_for_status()
        result = response.json()
        if result.get("code") not in BMS_SUCCESS_CODES:
            raise RuntimeError(f"BMS rejected checkpoint: {result}")
        return result

def build_payload(alert: dict, 
                  cartrack: CartrackClient, 
                  route_cache: dict[str, str | None]) -> dict | None:
    if alert.get("name") != ALERT_NAME:
        return None

    registration = alert.get("registration")
    checkpoint = parse_checkpoint(alert.get("notification_msg", ""))
    if not registration or not checkpoint:
        logger.warning("Skipping unrecognised alert: %s", alert.get("notification_msg"))
        return None

    if registration not in route_cache:
        route_cache[registration] = cartrack.get_route(registration)
    route_text = route_cache[registration]
    if not route_text:
        logger.info("Skipping %s: no valid %s-<city> route", registration, ROUTE_ORIGIN)
        return None

    return {
        "plat_no": registration,
        "po_id": PO_ID,
        "checkpoint": checkpoint,
        "route_text": route_text,
    }


def poll_once(
    cartrack: CartrackClient,
    bms: BMSClient,
    window_start: datetime,
    window_end: datetime,
    seen: dict[str, None],) -> bool:
    alerts = cartrack.get_alerts(
        window_start.strftime(TS_FORMAT), window_end.strftime(TS_FORMAT)
    )
    logger.info(
        "Fetched %d alert(s) from %s to %s",
        len(alerts),
        window_start.strftime(TS_FORMAT),
        window_end.strftime(TS_FORMAT),
    )

    route_cache: dict[str, str | None] = {}
    all_ok = True

    for alert in alerts:
        alert_id = alert.get("notification_id") or alert.get("notification_msg")
        if alert_id in seen:
            continue
        try:
            payload = build_payload(alert, cartrack, route_cache)
            if payload:
                result = bms.send_checkpoint(payload)
                logger.info("Sent %s -> %s", payload, result)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            logger.error("Failed to process %s: %s", alert_id, exc)
            all_ok = False
            continue
        seen[alert_id] = None

    while len(seen) > SEEN_LIMIT:
        del seen[next(iter(seen))]
    return all_ok


def setup_logging() -> None:
    log_filename = f"alerts_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - [BMS-INTEGRATION] - %(message)s",
        handlers=[logging.FileHandler(log_filename), logging.StreamHandler()],
    )
    logger.info("Log file: %s", log_filename)


def main() -> None:
    setup_logging()
    logger.info("Starting alerts polling service")

    cartrack, bms = CartrackClient(), BMSClient()
    seen: dict[str, None] = {}
    cursor = datetime.now(TZ)

    try:
        while True:
            now = datetime.now(TZ)
            start = max(cursor - OVERLAP, now - MAX_LOOKBACK)
            try:
                if poll_once(cartrack, bms, start, now, seen):
                    cursor = now  # only advance when nothing failed
            except (requests.RequestException, ValueError) as exc:
                logger.error("Polling failed: %s", exc)

            logger.info("Waiting %d seconds before next poll...", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Alerts polling service stopped by user")
    except Exception:
        logger.exception("Unexpected error in main loop")


if __name__ == "__main__":
    main()