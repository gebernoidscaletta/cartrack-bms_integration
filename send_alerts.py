import requests
import base64
from datetime import datetime, timedelta
from enum import Enum
import logging
import time
import re

PO_ID = "18"

def get_bms_credentials():
    return {
        "username": "lintas_29e16e1828",
        "password": "Lintas1826^"
    }

def get_cartrack_credentials():
    return {
        "username": "CITR00003",
        "password": "28742dd9cdded0821c8a3fc6087cb1a4c785deee108ac2c338591032c28a3975"
    }

def get_bms_auth_token():
    url = "https://api-bms.iassupport.id/index.php/auth/api_ext"
    headers = {
        "Accept": "application/json"
    }
    body = get_bms_credentials()
    try:
        response = requests.post(url, headers=headers, json=body)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {
            "status": "Error",
            "message": f"Error getting BMS Auth Token: {e}"
        }

def get_cartrack_api_key():
    auth = f"{get_cartrack_credentials()['username']}:{get_cartrack_credentials()['password']}"
    auth_bytes = auth.encode('ascii')
    base64_bytes = base64.b64encode(auth_bytes)
    return base64_bytes.decode('ascii')

def geofence_status(Enum):
    check_in = "Pengendapan" & "entered"
    check_out = "Pengendapan" & "left"
    t1_in = "Terminal_1" & "entered"
    t2_out = "Terminal_2" & "left"
    t2_in = "Terminal_2" & "entered"
    t2_out = "Terminal_2" & "left"
    t3_in = "Terminal_3" & "entered"
    t3_out = "Terminal_3" & "left"

def get_alerts_data(timestamp_from, timestamp_to):
    url = "https://fleetapi-id.cartrack.com/rest/alerts/notifications"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {get_cartrack_api_key()}"
    }
    params = {
        "filter[date_from]" :  timestamp_from,
        "filter[date_to]" : timestamp_to,
        "filter[alert_type]" : "ZONE_ARRIVE_DEPART",
        "filter[contact_type]" : "RSS Feeds",
        "sort_by" : "event_ts",
        "sort_order" : "asc",
        "page": "1",
        "limit": "1000"
    }
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {
            "status": "Error",
            "message": f"Error Fetching Alerts Data: {e}"
        }

def get_destination_data (vehicle:str) -> str: 
    url = "https://fleetapi-id.cartrack.com/rest/vehicles"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {get_cartrack_api_key()}"
    }
    params = {
        "filter[registration]" : vehicle
    }
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.get("data") or []
        vehicles_description = data.get("client_vehicle_description2")
        code, _, city  = vehicles_description.partition("-")
        if code == "CGK":
            return city
    except requests.exceptions.RequestException as e:
        return {
            "status": "Error",
            "message": f"Error Fetching Alerts Data: {e}"
        }

def get_checkpoint_data (data:str) -> str:
    pattern = re.compile(r'^(\S+) has (entered|left) (\w+)-\S+ at (.+)$')

    m = pattern.match(data)
    if m:
        reg, action, terminal, ts = m.groups()
    geofence_status = f"{terminal.lower()}_{action.lower()}"
    return geofence_status


def format_data_bms(alerts_data: dict) -> dict:
    registration = alerts_data.get("registration", "")
    po_id = PO_ID
    checkpoint = get_checkpoint_data(alerts_data.get("notification_msg", ""))
    route_text = get_destination_data(registration)
    alerts_data = {
        "plat_no": registration,
        "po_id": PO_ID,
        "checkpoint": checkpoint,
        "route_text": route_text
    }
    return alerts_data


def send_alerts_data(data):
    url = "https://api-bms.iassupport.id/index.php/info/pidsCheckPoint"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_bms_auth_token()}"
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {
            "status": "Error",
            "message": f"Error Sending Alerts Data: {e}"
        }

def main():
    # Configure logging
    log_filename = f"alerts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - [BMS-INTEGRATION] - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Log file: {log_filename}")
    
    logger.info("Starting alerts polling service")
    
    try:
        while True:
            # Calculate timestamps
            timestamp_to = datetime.now()
            timestamp_from = timestamp_to - timedelta(seconds=5)
            # timestamp_from = datetime(2026,1,21,0,0,0)
            # timestamp_to = datetime(2026,1,21,23,59,59)
            
            # Format timestamps as YYYY-MM-DD HH:MM:SS
            timestamp_to_str = timestamp_to.strftime("%Y-%m-%d %H:%M:%S")
            timestamp_from_str = timestamp_from.strftime("%Y-%m-%d %H:%M:%S")
            
            logger.info(f"Fetching alerts from {timestamp_from_str} to {timestamp_to_str}")
            
            # Get alerts data
            alerts_response = get_alerts_data(timestamp_from_str, timestamp_to_str)
            
            logger.info(f"Alerts response received: {alerts_response}")
            
            # Process alerts if data is available
            if "data" in alerts_response and alerts_response["data"]:
                logger.info(f"Processing {len(alerts_response['data'])} alert(s)")
                for alert in alerts_response["data"]:
                    # Map to baraka format
                    formatted_data = format_data_bms(alert)
                    logger.info(f"Formatted alert data: {formatted_data}")
                    
                    # Send formatted data
                    send_result = send_alerts_data(formatted_data)
                    logger.info(f"Send result: {send_result}")
            else:
                logger.info("No new alerts to process")
            
            # Wait 15 minutes before next poll
            logger.info("Waiting 15 minutes before next poll...")
            # time.sleep(900)
            exit()
            
    except KeyboardInterrupt:
        logger.info("Alerts polling service stopped by user")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)

if __name__ == "__main__":
    main()