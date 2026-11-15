"""Constants for the Rowenta (Groupe SEB) cloud integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "rowenta"

# Groupe SEB platform (public values from the com.groupeseb.aspirobot 10.0.0 app)
APP_VERSION: Final = "10.0.0"
SEB_API: Final = "https://sebplatform.api.groupe-seb.com"
IOT_API: Final = "https://iot.api.groupe-seb.com"
SEB_API_KEY: Final = "19UfOfJmK6365857UYNzjn427R6q1XFA"
RCU_BASE: Final = "https://account.groupeseb.com"
RCU_CLIENT_ID: Final = (
    "3MVG99qusVZJwhskrTckKb5cJcZ6TRAFaBT4BFiKrzcUhle5GnfyASjYwMxTEEZruROQUz_lAj8HnLFbES7nv"
)
REDIRECT_URI: Final = "gsmoduser://tokens"
COGNITO_ENDPOINT: Final = "https://consumers.api.groupe-seb.com/"
COGNITO_POOL_ID: Final = "eu-west-1:f25c4598-48da-4fda-9c1a-27fe30bd2ffe"
COGNITO_LOGIN_KEY: Final = "account.groupeseb.com"
AWS_REGION: Final = "eu-west-1"
MQTT_HOST: Final = "prod-new.iot.mqtt.groupe-seb.com"
MQTT_PORT: Final = 443

LOGIN_URL: Final = (
    f"{RCU_BASE}/ContinuationForExpId?apikey={SEB_API_KEY}&redirect_uri={REDIRECT_URI}"
    "&state=STATE&response_type=token+id_token+refresh_token&nonce=NONCE"
    f"&isUserName=false&version={APP_VERSION}"
)

CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_PERIMETER: Final = "profile_perimeter"
CONF_MARKET: Final = "market"
CONF_COUNTRY: Final = "country"

# How often to request the status if the robot sends nothing by itself.
STATUS_POLL_INTERVAL: Final = timedelta(seconds=60)
# While cleaning (for the live map).
STATUS_POLL_INTERVAL_ACTIVE: Final = timedelta(seconds=15)
# How often to download the map while cleaning.
MAP_REFRESH_INTERVAL: Final = timedelta(seconds=30)
# How often to request the history (totals).
HISTORY_POLL_INTERVAL: Final = timedelta(minutes=30)
# Cognito AWS credentials last ~1 h; MQTT reconnects with a safety margin.
CREDENTIALS_REFRESH_MARGIN: Final = timedelta(minutes=5)
CREDENTIALS_FALLBACK_LIFETIME: Final = timedelta(minutes=55)

# Values of the SEB VAC001 protocol
STATUSES: Final = [
    "cleaning",
    "standby",
    "charging",
    "docking",
    "stop",
    "upgrading",
    "error",
]
ERROR_CODES: Final = [
    "normal",
    "left_side_brush_error",
    "right_side_brush_error",
    "main_brush_error",
    "left_wheel_error",
    "right_wheel_error",
    "fan_motor_error",
    "cliff_sensor_error",
    "off_ground_error",
    "bumper_error",
    "charging_error",
    "wall_sensor_error",
    "dustbin_error",
    "front_wheel_error",
    "no_battery_error",
    "low_battery_error",
]
CLEANING_METHODS: Final = [
    "pause",
    "auto_cleaning",
    "wall_follow",
    "smart_cleaning",
    "spiral",
    "left_spiral",
    "right_spiral",
    "right_bow",
    "left_bow",
    "partial_bow",
    "charge_go",
    "mop",
    "small_room_clean",
    "back_to_dock",
    "sleep",
    "schedule_clean",
]
# Modes that make sense to start from HA
START_METHODS: Final = ["smart_cleaning", "auto_cleaning", "wall_follow", "small_room_clean", "mop"]
DEFAULT_START_METHOD: Final = "smart_cleaning"
FAN_SPEEDS: Final = ["silent", "normal", "strong"]
ACCESSORIES: Final = {"SIDE-BRUSH": "side_brush", "MAIN-BRUSH": "main_brush", "FILTER": "filter"}
