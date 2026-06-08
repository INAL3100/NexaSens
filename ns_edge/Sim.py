# ============================================================
# NS-EDGE SIMULATOR — Nexa Sens
# Publishes simulated sensor readings via MQTT to the NSR-BOX.
#
# Three test scenarios for Push 4 alert lifecycle validation:
#
#   SCENARIO=1  Escalation       — alert escalates to critical, SMS+call fires
#   SCENARIO=2  Recovery in time — alert opens, recovers before 90s, no SMS
#   SCENARIO=3  Flapping         — brief recovery does NOT reset escalation,
#                                  alert still goes critical
#   SCENARIO=0  Noise (default)  — random walk, normal values
#
# Usage:
#   SCENARIO=1 python3 Sim.py
#
# Designed for SEND_INTERVAL_SEC=20s with Pi running at
# ALERT_ESCALATION_SECONDS=90 and ALERT_STABILITY_SECONDS=60.
# ============================================================

import json
import os
import random
import time
import paho.mqtt.client as mqtt

# ── CONFIG ────────────────────────────────────────────────────
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))

SEND_INTERVAL_SEC = float(os.environ.get("SEND_INTERVAL_SEC", "20"))

SCENARIO = int(os.environ.get("SCENARIO", "0"))  # 0=noise, 1=esc, 2=recover, 3=flapping

# Subject node: the one that will go abnormal in scenarios 1/2/3.
SUBJECT_NODE = os.environ.get("SUBJECT_NODE", "ED01")

# Base values per node — small ±0.3 noise added on every send so readings look real.
NODES = [
    {"node_id": "ED01", "temp_base": 27.0, "hum_base": 60.0, "nh3_base": 8.0},
    {"node_id": "ED02", "temp_base": 27.5, "hum_base": 62.0, "nh3_base": 9.0},
]

ABNORMAL_TEMP_OFFSET = 2.5   # +2.5°C above base = 29.5°C — above week-3 max (29) but
                             # NOT extreme (< max+2 = 31) → triggers the 90s timer

# ── MQTT CLIENT ───────────────────────────────────────────────
def make_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ns-edge-sim")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client

def publish(client, node_id, temp, hum, nh3):
    payload = {
        "node_id":     node_id,
        "temperature": round(temp, 2),
        "humidity":    round(max(0, min(100, hum)), 2),
        "ammonia":     round(max(0, nh3), 2),
    }
    topic = f"nexasens/edge/{node_id}/data"
    client.publish(topic, json.dumps(payload), qos=1)
    print(f"  [{node_id}] T:{payload['temperature']}°C "
          f"H:{payload['humidity']}% NH3:{payload['ammonia']}ppm")

def emit_all_normal(client):
    for n in NODES:
        t = n["temp_base"] + random.uniform(-0.3, 0.3)
        h = n["hum_base"]  + random.uniform(-1.0, 1.0)
        a = n["nh3_base"]  + random.uniform(-0.5, 0.5)
        publish(client, n["node_id"], t, h, a)

def emit_all_with_subject_abnormal(client):
    for n in NODES:
        if n["node_id"] == SUBJECT_NODE:
            t = n["temp_base"] + ABNORMAL_TEMP_OFFSET + random.uniform(-0.2, 0.2)
        else:
            t = n["temp_base"] + random.uniform(-0.3, 0.3)
        h = n["hum_base"] + random.uniform(-1.0, 1.0)
        a = n["nh3_base"] + random.uniform(-0.5, 0.5)
        publish(client, n["node_id"], t, h, a)

# ── PHASE PLAYER ──────────────────────────────────────────────
def play_phases(client, phases):
    """phases = list of (label, seconds, abnormal: bool)"""
    total = sum(p[1] for p in phases)
    print(f"[SIM] Total scenario time: {total}s ({total/60:.1f} min)")
    print(f"[SIM] Sending every {SEND_INTERVAL_SEC}s. Ctrl+C to stop.\n")

    for label, seconds, abnormal in phases:
        print(f"\n=== {label} ({seconds}s) ===")
        phase_end = time.time() + seconds
        while time.time() < phase_end:
            if abnormal:
                emit_all_with_subject_abnormal(client)
            else:
                emit_all_normal(client)
            # Sleep up to interval or end of phase, whichever comes first
            sleep_until = min(time.time() + SEND_INTERVAL_SEC, phase_end)
            while time.time() < sleep_until:
                time.sleep(min(0.5, sleep_until - time.time()))
    print(f"\n[SIM] Scenario complete.")

# ── SCENARIOS ─────────────────────────────────────────────────
def scenario_1_escalation(client):
    """Stuck abnormal long enough to escalate → SMS+call fires."""
    print("[SIM] SCENARIO 1 — Escalation")
    print("[SIM] Expected: alert opens (en_traitement) → escalates to critical at 90s → SMS+call → recovers → Traité")
    play_phases(client, [
        ("Phase 1: Normal",     20,  False),
        ("Phase 2: Abnormal",   130, True),   # 130s of abnormal — crosses 90s threshold
        ("Phase 3: Recovery",   80,  False),  # 60s stable + a bit → closes as Traité
    ])

def scenario_2_recovery(client):
    """Abnormal then recovers before 90s — no SMS, alert closes cleanly."""
    print("[SIM] SCENARIO 2 — Recovery before escalation")
    print("[SIM] Expected: alert opens (en_traitement) → 70s abnormal → recovers → 60s stable → Traité, NO SMS")
    play_phases(client, [
        ("Phase 1: Normal",     20,  False),
        ("Phase 2: Abnormal",   70,  True),   # 70s only — below 90s threshold
        ("Phase 3: Recovery",   80,  False),  # closes cleanly
    ])

def scenario_3_flapping(client):
    """Brief recovery does NOT reset escalation timer — alert still escalates."""
    print("[SIM] SCENARIO 3 — Flapping (brief recovery doesn't help)")
    print("[SIM] Expected: 70s abnormal → 40s normal blip (under 60s stability) → 20s abnormal → critical fires (cumulative 90s) → Traité")
    play_phases(client, [
        ("Phase 1: Normal",            20,  False),
        ("Phase 2: Abnormal #1",       70,  True),   # 70s
        ("Phase 3: Brief recovery",    40,  False),  # 40s < 60s stable → still en_traitement
        ("Phase 4: Abnormal again",    30,  True),   # cumulative 100s abnormal → critical
        ("Phase 5: Final recovery",    80,  False),  # closes as Traité
    ])

def scenario_0_noise(client):
    """Continuous normal readings with small noise. No alerts expected."""
    print("[SIM] SCENARIO 0 — Noise (default)")
    print(f"[SIM] Sending normal readings every {SEND_INTERVAL_SEC}s. Ctrl+C to stop.\n")
    while True:
        emit_all_normal(client)
        time.sleep(SEND_INTERVAL_SEC)

# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[SIM] Connecting to MQTT broker {MQTT_BROKER}:{MQTT_PORT} ...")
    client = make_client()
    print(f"[SIM] Connected.\n")
    try:
        if   SCENARIO == 1: scenario_1_escalation(client)
        elif SCENARIO == 2: scenario_2_recovery(client)
        elif SCENARIO == 3: scenario_3_flapping(client)
        else:               scenario_0_noise(client)
    except KeyboardInterrupt:
        print("\n[SIM] Stopped.")
    finally:
        client.loop_stop()
        client.disconnect()