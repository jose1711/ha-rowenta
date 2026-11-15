"""MQTT (AWS IoT over WebSocket + SigV4) for SEB robots."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import paho.mqtt.client as mqtt
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util.ssl import client_context

from .api import Appliance, AwsCredentials, SebAuthError, SebCloud, SebError
from .const import (
    AWS_REGION,
    CREDENTIALS_FALLBACK_LIFETIME,
    CREDENTIALS_REFRESH_MARGIN,
    MQTT_HOST,
    MQTT_PORT,
)

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAYS = (5, 15, 30, 60, 120, 300)
# A connection must survive this long before the backoff is reset.
STABLE_CONNECTION = 60
# Retry delay after an authentication failure (re-authentication is requested meanwhile).
AUTH_RETRY_DELAY = 1800

MessageCallback = Callable[[Appliance, str, Any], None]
ConnectionCallback = Callable[[bool], None]
AuthFailedCallback = Callable[[], None]


def presign_url(
    creds: AwsCredentials, host: str = MQTT_HOST, region: str = AWS_REGION, now: datetime | None = None
) -> str:
    """Return the `/mqtt?...` path signed with SigV4 (service iotdevicegateway)."""
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    service = "iotdevicegateway"
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    query = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={quote(creds.access_key + '/' + scope, safe='')}"
        f"&X-Amz-Date={amz_date}"
        "&X-Amz-SignedHeaders=host"
    )
    canonical = "\n".join(
        ["GET", "/mqtt", query, f"host:{host}\n", "host", hashlib.sha256(b"").hexdigest()]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _hmac(("AWS4" + creds.secret_key).encode(), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    # AWS IoT expects the session token to be appended after signing.
    return (
        f"/mqtt?{query}&X-Amz-Signature={signature}"
        f"&X-Amz-Security-Token={quote(creds.session_token, safe='')}"
    )


class SebMqtt:
    """A single MQTT connection shared by all robots of the account.

    Paho runs in its own thread; events are handed back to the HA event loop.
    Paho's automatic reconnect is disabled because the signed URL embeds
    temporary credentials – every (re)connect uses freshly issued ones.
    Events from a client that is no longer current are ignored, so a late
    callback from a replaced connection cannot tear down the new one.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        cloud: SebCloud,
        appliances: list[Appliance],
        on_message: MessageCallback,
        on_connection: ConnectionCallback,
        on_auth_failed: AuthFailedCallback,
    ) -> None:
        self._hass = hass
        self._cloud = cloud
        self._appliances = {a.id: a for a in appliances}
        self._on_message = on_message
        self._on_connection = on_connection
        self._on_auth_failed = on_auth_failed
        self._client: mqtt.Client | None = None
        self._client_id = ""
        self._connected = False
        # True once the *current* client got its CONNACK (publishing is possible).
        self._ready = False
        self._client_timers: list[CALLBACK_TYPE] = []
        self._stopping = False
        self._attempt = 0
        self._timer: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def can_publish(self) -> bool:
        """False while disconnected and during the short hourly client swap."""
        return self._client is not None and self._ready

    @property
    def client_id(self) -> str:
        return self._client_id

    async def async_start(self) -> None:
        self._stopping = False
        await self._async_connect()

    async def async_stop(self) -> None:
        self._stopping = True
        self._cancel_timer()
        async with self._lock:
            # A connect that was already running may have scheduled a timer meanwhile.
            self._cancel_timer()
            client = self._detach()
            self._set_connected(False)
        if client is not None:
            await self._async_stop_client(client)

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    def _schedule(self, delay: float, reason: str) -> None:
        self._cancel_timer()
        _LOGGER.debug("MQTT: reconnecting in %.0f s (%s)", delay, reason)

        @callback
        def _fire(_now: Any) -> None:
            self._timer = None
            self._hass.async_create_background_task(
                self._async_connect(), "rowenta_mqtt_reconnect"
            )

        self._timer = async_call_later(self._hass, delay, _fire)

    def _schedule_retry(self, reason: str) -> None:
        if self._stopping:
            return
        delay = RECONNECT_DELAYS[min(self._attempt, len(RECONNECT_DELAYS) - 1)]
        self._attempt += 1
        self._schedule(delay, reason)

    def _detach(self) -> mqtt.Client | None:
        """Forget the current client; its later callbacks are ignored."""
        client, self._client = self._client, None
        self._ready = False
        for cancel in self._client_timers:
            cancel()
        self._client_timers.clear()
        if client is not None:
            client.on_disconnect = None
            client.on_message = None
            client.on_connect = None
            client.on_connect_fail = None
        return client

    async def _async_stop_client(self, client: mqtt.Client) -> None:
        def _stop() -> None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()

        await self._hass.async_add_executor_job(_stop)

    async def _async_connect(self) -> None:
        async with self._lock:
            if self._stopping:
                return
            # Fetch new credentials first: if that fails, a still working
            # connection (scheduled renewal) is kept until the retry.
            try:
                creds = await self._cloud.async_get_aws_credentials()
            except SebAuthError as err:
                if self._stopping:
                    return
                _LOGGER.warning("Rowenta cloud login failed, re-authentication required: %s", err)
                self._on_auth_failed()
                # Keep trying slowly in case the rejection was temporary.
                self._schedule(AUTH_RETRY_DELAY, "authentication failed")
                return
            except SebError as err:
                _LOGGER.debug("Fetching AWS credentials failed: %s", err)
                self._schedule_retry("credentials")
                return
            if self._stopping:
                return

            old = self._detach()
            self._client_id = f"#ha-{uuid.uuid4().hex[:16]}"
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._client_id,
                transport="websockets",
                reconnect_on_failure=False,
            )
            client.ws_set_options(path=presign_url(creds))
            client.tls_set_context(client_context())
            client.on_connect = self._paho_on_connect
            client.on_disconnect = self._paho_on_disconnect
            client.on_connect_fail = self._paho_on_connect_fail
            client.on_message = self._paho_on_message
            self._client = client

            # Credentials expire – reconnect with a safety margin. Scheduled before
            # connecting, so that a fast connection failure (which schedules a
            # retry) replaces this timer and not the other way round.
            lifetime = (
                creds.expiration - time.time()
                if creds.expiration
                else CREDENTIALS_FALLBACK_LIFETIME.total_seconds()
            )
            renew = max(60.0, lifetime - CREDENTIALS_REFRESH_MARGIN.total_seconds())
            self._schedule(renew, "credential renewal")

            if old is not None:
                await self._async_stop_client(old)

            def _connect() -> None:
                client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
                client.loop_start()

            await self._hass.async_add_executor_job(_connect)

    # --- paho callbacks (run in the paho thread) ---

    def _paho_on_connect(self, client: mqtt.Client, _ud: Any, _flags: Any, rc: Any, _props: Any) -> None:
        if rc.is_failure:
            # Paho closes the socket afterwards and reports it via on_disconnect,
            # which schedules the retry (do not schedule it twice).
            _LOGGER.debug("MQTT connect refused: %s", rc)
            return
        for appl in self._appliances.values():
            for topic in (
                f"{appl.topic_base}/status",
                f"{appl.topic_base}/history",
                f"{appl.shadow_base}/get/accepted",
                f"{appl.shadow_base}/update/documents",
            ):
                client.subscribe(topic, qos=0)
        self._hass.loop.call_soon_threadsafe(self._handle_connected, client)

    def _paho_on_connect_fail(self, client: mqtt.Client, _ud: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_failure, client, "connect failed")

    def _paho_on_disconnect(self, client: mqtt.Client, _ud: Any, _flags: Any, rc: Any, _props: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_failure, client, f"disconnect {rc}")

    def _paho_on_message(self, _client: mqtt.Client, _ud: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            _LOGGER.debug("Unparsable message on %s", msg.topic)
            return
        self._hass.loop.call_soon_threadsafe(self._dispatch, msg.topic, payload)

    # --- event loop ---

    @callback
    def _set_connected(self, value: bool) -> None:
        if self._connected != value:
            self._connected = value
            self._on_connection(value)

    @callback
    def _handle_connected(self, client: mqtt.Client) -> None:
        if client is not self._client:
            return
        _LOGGER.debug("MQTT connected")
        self._ready = True
        self._set_connected(True)

        @callback
        def _stable(_now: Any) -> None:
            if self._connected and client is self._client:
                self._attempt = 0

        self._client_timers.append(async_call_later(self._hass, STABLE_CONNECTION, _stable))

        # Give the broker a moment to confirm the subscriptions
        # (otherwise the shadow/get response is lost).
        @callback
        def _initial_requests(_now: Any) -> None:
            if not self.can_publish or client is not self._client:
                return
            try:
                for appl in self._appliances.values():
                    self.request_shadow(appl)
                    self.request_status(appl, listening=True)
                    self.request_history(appl)
            except ConnectionError as err:
                _LOGGER.debug("Initial requests failed: %s", err)

        self._client_timers.append(async_call_later(self._hass, 2, _initial_requests))

    @callback
    def _handle_failure(self, client: mqtt.Client, reason: str) -> None:
        if client is not self._client or self._stopping:
            return  # stale event from a replaced or stopped client
        _LOGGER.debug("MQTT connection lost: %s", reason)
        self._detach()
        self._set_connected(False)
        self._hass.async_create_background_task(
            self._async_stop_client(client), "rowenta_mqtt_stop_client"
        )
        self._schedule_retry(reason)

    @callback
    def _dispatch(self, topic: str, payload: Any) -> None:
        for appl in self._appliances.values():
            if topic.startswith(appl.topic_base + "/"):
                kind = topic[len(appl.topic_base) + 1 :]
            elif topic.startswith(appl.shadow_base + "/"):
                kind = "shadow/" + topic[len(appl.shadow_base) + 1 :]
            else:
                continue
            self._on_message(appl, kind, payload)
            return

    # --- publishing ---

    def publish_raw(self, topic: str, payload: dict[str, Any] | str) -> None:
        """Publish or raise ConnectionError – a message is never dropped silently."""
        client = self._client
        if client is None or not self._ready:
            raise ConnectionError("MQTT is not connected")
        data = payload if isinstance(payload, str) else json.dumps(payload)
        info = client.publish(topic, data, qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT publish failed: {mqtt.error_string(info.rc)}")

    def request_shadow(self, appl: Appliance) -> None:
        self.publish_raw(f"{appl.shadow_base}/get", {})

    def request_status(self, appl: Appliance, listening: bool = False) -> None:
        if listening:
            # Same as the app while it is open – the robot then pushes its status.
            self.publish_raw(
                f"{appl.topic_base}/clientListening",
                {"clientId": self._client_id, "listening": True},
            )
        self.publish_raw(f"{appl.topic_base}/getStatus", {})

    def request_history(self, appl: Appliance) -> None:
        self.publish_raw(f"{appl.topic_base}/getHistory", {})

    def update_shadow(self, appl: Appliance, desired: dict[str, Any]) -> None:
        _LOGGER.debug("Shadow desired for %s: %s", appl.name, desired)
        self.publish_raw(f"{appl.shadow_base}/update", {"state": {"desired": desired}})

    def send_command(self, appl: Appliance, command: dict[str, Any]) -> None:
        _LOGGER.debug("Command for %s: %s", appl.name, command)
        self.publish_raw(f"{appl.topic_base}/command", command)
