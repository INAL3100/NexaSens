"""
NS-Edge simulator — publishes simulated sensor readings via MQTT.
Replaces the defective ESP32 hardware for prototype testing.
Emulates 2 nodes: node-01 and node-02.
"""
import json
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# ===== Configuration =====
BROKER_HOST = "localhost"       # Mosquitto runs on the same Pi
BROKER_PORT = 1883
TOPIC_TEMPLATE = "nexasens/edge/{node_id}/data"
PUBLISH_INTERVAL_S = 5          # Send a reading every 5 seconds

# Two simulated nodes, each with slightly different baseline values
NODES = [
    {"node_id": "ED01", "temp_base": 24.0, "hum_base": 60.0, "nh3_base": 8.0},
    {"node_id": "ED02", "temp_base": 25.5, "hum_base": 65.0, "nh3_base": 10.0},
]


def make_reading(node):
    """Generate a realistic noisy reading for the given node."""
    return {
        "node_id": node["node_id"],
        "ts": datetime.now(timezone.utc).isoformat(),
        "temperature": round(node["temp_base"] + random.uniform(-1.5, 1.5), 2),
        "humidity":    round(node["hum_base"]  + random.uniform(-3.0, 3.0), 2),
        "nh3":         round(node["nh3_base"]  + random.uniform(-2.0, 2.0), 2),
    }


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[simp] Connected to MQTT broker at {BROKER_HOST}:{BROKER_PORT}")
    else:
        print(f"[simp] Connection failed, rc={rc}")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ns-edge-simulator")
    client.on_connect = on_connect
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()

    print(f"[simp] Simulating {len(NODES)} NS-Edge nodes. Publish interval: {PUBLISH_INTERVAL_S}s")
    print("[simp] Press Ctrl+C to stop.\n")

    try:
        while True:
            for node in NODES:
                reading = make_reading(node)
                topic = TOPIC_TEMPLATE.format(node_id=node["node_id"])
                payload = json.dumps(reading)
                client.publish(topic, payload, qos=1)
                print(f"  → {topic}: T={reading['temperature']}°C  H={reading['humidity']}%  NH3={reading['nh3']}ppm")
            print()
            time.sleep(PUBLISH_INTERVAL_S)
    except KeyboardInterrupt:
        print("\n[simp] Stopping simulator…")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()