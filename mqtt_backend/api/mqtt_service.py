"""
MQTT service module encapsulating paho-mqtt client setup and basic operations.
"""

import logging
import ssl
import time
from typing import Optional, Callable

from django.conf import settings

try:
    import paho.mqtt.client as mqtt
except Exception as exc:  # pragma: no cover
    raise RuntimeError("paho-mqtt is required. Please ensure it's installed.") from exc

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# PUBLIC_INTERFACE
class MQTTBridge:
    """High-level MQTT bridge to subscribe to a topic and publish messages to another topic.

    This class sets up a paho-mqtt client, connects to the broker based on Django settings,
    subscribes to a configured topic, and republishes messages to a destination topic.
    """

    def __init__(
        self,
        on_message_hook: Optional[Callable[[mqtt.Client, mqtt.MQTTMessage], Optional[bytes]]] = None,
    ):
        """Initialize the bridge.

        Parameters:
        - on_message_hook: Optional hook invoked with (client, message). If it returns bytes,
          the returned payload will be used for publishing; if it returns None, the original
          payload is forwarded; if it raises, the error is logged and original payload forwarded.
        """
        cfg = getattr(settings, "MQTT_SETTINGS", {})
        self.host = cfg.get("HOST", "localhost")
        self.port = cfg.get("PORT", 1883)
        self.username = cfg.get("USERNAME")
        self.password = cfg.get("PASSWORD")
        self.client_id = cfg.get("CLIENT_ID")
        self.subscribe_topic = cfg.get("SUBSCRIBE_TOPIC", "test/in")
        self.publish_topic = cfg.get("PUBLISH_TOPIC", "test/out")
        self.qos = cfg.get("QOS", 0)
        self.keepalive = cfg.get("KEEPALIVE", 60)
        self.use_tls = cfg.get("TLS", False)

        # paho-mqtt v2 client defaults to MQTTv311 unless specified
        self.client = mqtt.Client(client_id=self.client_id or mqtt.base62(uuid=None), callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        if self.use_tls:
            # Basic TLS context without cert validation override
            context = ssl.create_default_context()
            self.client.tls_set_context(context)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # external hook for message transformation/processing
        self.on_message_hook = on_message_hook

        self._connected = False

    def connect(self):
        """Connect to the MQTT broker and start the network loop in a background thread."""
        logger.info(
            "Connecting to MQTT broker %s:%s (client_id=%s, tls=%s)",
            self.host,
            self.port,
            self.client._client_id.decode() if isinstance(self.client._client_id, (bytes, bytearray)) else self.client._client_id,
            self.use_tls,
        )
        try:
            self.client.connect(self.host, self.port, self.keepalive)
        except Exception as exc:
            logger.error("Failed to connect to MQTT broker: %s", exc, exc_info=True)
            raise

        # Start loop in a background thread
        self.client.loop_start()

        # Wait briefly for connection
        timeout = time.time() + 10
        while not self._connected and time.time() < timeout:
            time.sleep(0.1)

        if not self._connected:
            logger.warning("MQTT connection not established within timeout; continuing...")

    def subscribe(self):
        """Subscribe to the configured topic with the configured QoS."""
        if not self.subscribe_topic:
            logger.warning("No subscribe topic configured; skipping subscription.")
            return
        try:
            result = self.client.subscribe(self.subscribe_topic, qos=self.qos)
            logger.info("Subscribed to '%s' (qos=%s, result=%s)", self.subscribe_topic, self.qos, result)
        except Exception as exc:
            logger.error("Subscription error: %s", exc, exc_info=True)
            raise

    def publish(self, payload: bytes):
        """Publish payload to the configured publish topic."""
        if not self.publish_topic:
            logger.warning("No publish topic configured; skipping publish.")
            return
        try:
            res = self.client.publish(self.publish_topic, payload=payload, qos=self.qos)
            if res.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error("Publish failed rc=%s", res.rc)
            else:
                logger.info("Published message to '%s' (qos=%s)", self.publish_topic, self.qos)
        except Exception as exc:
            logger.error("Publish error: %s", exc, exc_info=True)

    def stop(self):
        """Stop network loop and disconnect."""
        try:
            self.client.loop_stop()
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass

    # Internal callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or getattr(reason_code, "value", reason_code) == 0:
            self._connected = True
            logger.info("Connected to MQTT broker successfully.")
            # Auto-subscribe on connect
            self.subscribe()
        else:
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, reason_code, properties=None):
        self._connected = False
        if reason_code != 0:
            logger.warning("Unexpected MQTT disconnection: %s", reason_code)
        else:
            logger.info("Disconnected from MQTT broker.")

    def _on_message(self, client, userdata, message: "mqtt.MQTTMessage"):
        try:
            payload = message.payload
            logger.info(
                "Received message on '%s' (qos=%s, retained=%s, bytes=%s)",
                message.topic,
                getattr(message, "qos", self.qos),
                getattr(message, "retain", False),
                len(payload) if payload is not None else 0,
            )

            # Optional transformation hook
            if self.on_message_hook:
                try:
                    new_payload = self.on_message_hook(client, message)
                    if new_payload is not None:
                        payload = new_payload
                except Exception as hook_exc:
                    logger.error("on_message_hook error: %s", hook_exc, exc_info=True)

            # Publish to destination
            self.publish(payload)
        except Exception as exc:
            logger.error("Error handling message: %s", exc, exc_info=True)
