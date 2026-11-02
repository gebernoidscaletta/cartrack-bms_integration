# Cartrack x BMS Integration (Soekarno Hatta Airport)

Polls Cartrack geofence alerts (`ZONE_ARRIVE_DEPART`) and pushes each one to BMS as a PIDS checkpoint.

## How it works

1. Every `POLL_INTERVAL` seconds, fetch `GET /alerts/notifications` from the Cartrack Fleet API (paginated).
2. Keep only alerts where `name == "Geofence BMS - API"`.
3. From `notification_msg` (e.g. `D7935AJ has entered Terminal_1-Bandara_Soekarno_Hatta at ...`) take the action (`entered` / `left`) and the geofence prefix (`Terminal_1`), and map them to a checkpoint code.
4. From `registration`, call `GET /vehicles` and read `client_vehicle_description2`. It must be `CGK-<city>`; the `<city>` part becomes `route_text`. Anything else is skipped.
5. Send one `POST /info/pidsCheckPoint` per alert, in `event_ts` order, with a Bearer token from `/auth/api_ext`.

Request body sent to BMS:

```json
{
  "plat_no": "D7935AJ",
  "po_id": 18,
  "checkpoint": "t1_in",
  "route_text": "Bandung"
}
```

### Checkpoint codes

| Geofence      | entered     | left         |
|---------------|-------------|--------------|
| Pengendapan   | `check_in`  | `check_out`  |
| Terminal_1    | `t1_in`     | `t1_out`     |
| Terminal_2    | `t2_in`     | `t2_out`     |
| Terminal_3    | `t3_in`     | `t3_out`     |

Edit the `CHECKPOINTS` dict in `bms_integration.py` to add or change geofences.

### Reliability

- **Token**: fetched once and reused until `data.expired` (refreshed 1 minute early). On a `401` it re-authenticates and retries once.
- **No gaps**: each poll starts where the previous one ended, with a 1-minute overlap (`OVERLAP`) to catch late alerts.
- **No duplicates**: alerts already handled are skipped by `notification_id` (last `SEEN_LIMIT` ids kept in memory).
- **Retries**: if any request fails, the window does not advance, so failed alerts are retried on the next poll, up to `MAX_LOOKBACK` (1 hour) back. Alerts that are skipped on purpose (wrong name, unknown geofence, no valid route) are not retried.
- **Restarts**: on start the script begins from "now"; alerts that arrived while it was stopped are not backfilled, and the de-duplication memory is empty.

## Setup

Requires Python 3.9+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create `config.py` next to `bms_integration.py` (or change the import at the top of the script) providing:

```python
PO_ID = 18  # po_id returned by BMS auth

def get_bms_credentials() -> dict:
    return {"username": "...", "password": "..."}

def get_cartrack_credentials() -> dict:
    return {"username": "...", "password": "..."}
```

Keep credentials out of version control (environment variables or an untracked file).

## Run

```bash
python bms_integration.py
```

Logs go to the console and to `alerts_YYYYmmdd_HHMMSS.log` in the working directory. Stop with `Ctrl+C`.

## Configuration

Constants at the top of `bms_integration.py`:

| Name              | Default             | Purpose                                              |
|-------------------|---------------------|------------------------------------------------------|
| `POLL_INTERVAL`   | `900` (seconds)     | Time between polls                                   |
| `OVERLAP`         | 1 minute            | Re-read part of the previous window                  |
| `MAX_LOOKBACK`    | 1 hour              | Furthest back a failed window is retried             |
| `SEEN_LIMIT`      | `10000`             | Notification ids remembered for de-duplication       |
| `REQUEST_TIMEOUT` | `30` (seconds)      | Timeout for every HTTP request                       |
| `ALERT_NAME`      | `Geofence BMS - API`| Only alerts with this name are processed             |
| `ROUTE_ORIGIN`    | `CGK`               | Required prefix of `client_vehicle_description2`     |

## Response codes

- BMS auth (`/auth/api_ext`) succeeds with `"code": 200`.
- `pidsCheckPoint` succeeds with `"code": 0` and `"message": "Sukses"`.

Both are handled; any other code is logged as a failure and retried.