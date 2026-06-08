# ============================================================
# NSR-BOX SERVER — Nexa Sens
# Runs on Raspberry Pi
#
# RESPONSIBILITIES:
#   1. Pull config from cloud ONCE on startup
#   2. Receive data from NS-Edge sensors via MQTT
#   3. Make ALL equipment decisions locally (per hangar)
#   4. Control GPIO relays
#   5. Send SMS alerts directly via GSM modem
#   6. Receive SMS commands from farmer (FAN ON H1)
#   7. Monitor edges (offline detection)
#   8. Monitor power outage via UPS
#   9. Monitor internet connectivity
#   10. Send heartbeat to cloud every 60s
#   11. Forward data to cloud, retry when offline
#
# BUGS FIXED:
#   B1. Thread-safe SQLite via threading.local()
#   B2. Edge watchdog populates all known pins on startup
#   B3. STATUS H1 SMS command now correctly filters by hangar
#   B4. Alert dedup key includes condition type not just level
#   B5. power_monitor restructured — no continue inside try
#
# CHANGE: NS-Edge → NSR-BOX is now MQTT (matches memoir section 2.5.1).
#         NSR-BOX → Cloud remains HTTP.
#
# PUSH 3: Fan and Ventilation have clearly separated roles:
#   - Fan (Ventilateur): cooling + dehumidification (high airflow)
#   - Ventilation: air renewal for NH3 (low airflow, can run with heater)
#
# PUSH 4: Alert lifecycle overhaul
#   - Pi tracks each condition's stability and escalation locally
#   - When abnormal: emits "en_traitement" (the system is on it)
#   - After ALERT_ESCALATION_SECONDS unresolved: upgrades to "critical"
#     + sends SMS + makes a silent call to the farmer
#   - When normal again: requires ALERT_STABILITY_SECONDS of stable
#     readings before emitting "log" (prevents flapping)
#   - Cloud is passive — just displays what Pi sends.
# ============================================================

from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import sqlite3, requests, threading, time, json, os

import paho.mqtt.client as mqtt

app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

CLOUD_URL    = os.environ.get("CLOUD_URL", "https://nexasens.onrender.com")
API_KEY      = os.environ.get("API_KEY",   "NEXASENS_SECRET_KEY")
NSR_PIN      = os.environ.get("NSR_PIN",   "NSR1")
MY_URL       = os.environ.get("MY_URL",    "http://192.168.43.181:5000")

# ── MQTT BROKER ───────────────────────────────────────────────
MQTT_BROKER   = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT     = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC    = "nexasens/edge/+/data"

# ── ALERT LIFECYCLE TIMING (Push 4) ───────────────────────────
# Both configurable via env vars so demo/testing can shorten them.
# Production defaults: 600s escalation, 120s stability.
ALERT_ESCALATION_SECONDS = int(os.environ.get("ALERT_ESCALATION_SECONDS", "90"))
ALERT_STABILITY_SECONDS  = int(os.environ.get("ALERT_STABILITY_SECONDS",  "60"))

# ── GSM MODEM ─────────────────────────────────────────────────
GSM_ENABLED  = False
GSM_PORT     = "/dev/ttyUSB0"
GSM_BAUDRATE = 115200
FARMER_PHONE = "+213XXXXXXXXX"

# ── GPIO RELAYS ───────────────────────────────────────────────
GPIO_ENABLED = False
GPIO_PINS = {"fan": 17, "heater": 27, "mister": 22, "ventilation": 23}

# ── UPS POWER MONITORING ──────────────────────────────────────
UPS_ENABLED = False
UPS_AC_PATH = "/sys/class/power_supply/AC/online"

# ============================================================
# GPIO
# ============================================================

def setup_gpio():
    if not GPIO_ENABLED:
        return
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        for pin in GPIO_PINS.values():
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        print("[GPIO] Ready")
    except Exception as e:
        print(f"[GPIO ERROR] {e}")

def set_relay(name, state):
    if not GPIO_ENABLED:
        print(f"[RELAY] {name} → {state}")
        return
    try:
        import RPi.GPIO as GPIO
        GPIO.output(GPIO_PINS[name], GPIO.HIGH if state == "ON" else GPIO.LOW)
    except Exception as e:
        print(f"[RELAY ERROR] {e}")

setup_gpio()

# ============================================================
# GSM SMS + VOICE
# ============================================================

_modem = None

def setup_gsm():
    global _modem
    if not GSM_ENABLED:
        return
    try:
        from gsmmodem.modem import GsmModem
        _modem = GsmModem(GSM_PORT, GSM_BAUDRATE)
        _modem.connect()
        _modem.smsReceivedCallback = handle_sms_command
        print(f"[GSM] Ready on {GSM_PORT}")
    except Exception as e:
        print(f"[GSM ERROR] {e}")

def send_sms(message):
    if not GSM_ENABLED:
        print(f"[SMS] {FARMER_PHONE}: {message}")
        return
    try:
        if _modem:
            _modem.sendSms(FARMER_PHONE, message)
    except Exception as e:
        print(f"[SMS ERROR] {e}")

def make_silent_call(phone=None):
    """Ring the farmer briefly. If they answer → hang up immediately.
    Used after critical SMS to make sure they actually wake up."""
    phone = phone or FARMER_PHONE
    if not GSM_ENABLED:
        print(f"[CALL] would ring {phone} (silent, hang up on answer)")
        return
    try:
        call = _modem.dial(phone)
        # Ring for up to 8 seconds. Hang up the moment they pick up.
        for _ in range(8):
            time.sleep(1)
            if getattr(call, "answered", False):
                call.hangup()
                return
        call.hangup()  # they didn't answer — that's a free missed-call alert
    except Exception as e:
        print(f"[CALL ERROR] {e}")

threading.Thread(target=setup_gsm, daemon=True).start()

# ============================================================
# SMS COMMAND HANDLER
# ============================================================

EQUIPMENT_MAP = {
    "FAN": "fan", "VENTILATEUR": "fan",
    "HEATER": "heater", "CHAUFFAGE": "heater",
    "MISTER": "mister", "BRUMISATEUR": "mister",
    "VENTILATION": "ventilation"
}

def _parse_hangar_arg(parts):
    for p in parts:
        if p.startswith("H") and p[1:].isdigit():
            hid = _get_hangar_id_by_index(int(p[1:]) - 1)
            return hid
    return None

def _get_hangar_id_by_index(index):
    keys = sorted(_hangar_config.keys(), key=lambda x: int(x))
    if index < len(keys):
        return int(keys[index])
    return None

def _get_hangar_name(hangar_id):
    keys = sorted(_hangar_config.keys(), key=lambda x: int(x))
    for i, k in enumerate(keys):
        if int(k) == hangar_id:
            return f"H{i+1}"
    return f"Hangar {hangar_id}"

def handle_sms_command(sms):
    cmd   = sms.text.strip().upper()
    parts = cmd.split()
    print(f"[SMS CMD] '{cmd}'")

    if parts[0] == "STATUS":
        hangar_id = _parse_hangar_arg(parts[1:]) if len(parts) > 1 else None
        conn = get_db()
        if hangar_id is not None:
            hangar_pins = _hangar_config.get(str(hangar_id), {}).get("pins", [])
            if not hangar_pins:
                send_sms(f"❌ Aucun capteur pour {_get_hangar_name(hangar_id)}")
                conn.close()
                return
            placeholders = ",".join("?" * len(hangar_pins))
            rows = conn.execute(
                f"""SELECT pin, temperature, humidity, ammonia
                    FROM readings
                    WHERE pin IN ({placeholders})
                      AND id IN (
                          SELECT MAX(id) FROM readings
                          WHERE pin IN ({placeholders})
                          GROUP BY pin
                      )""",
                hangar_pins + hangar_pins
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT pin, temperature, humidity, ammonia
                   FROM readings
                   WHERE id IN (SELECT MAX(id) FROM readings GROUP BY pin)"""
            ).fetchall()
        conn.close()

        msg = "📊 Status:\n"
        for pin, t, h, n in rows:
            hid   = get_hangar_for_pin(pin)
            hname = _get_hangar_name(hid) if hid else "?"
            msg  += f"{hname}/{pin}: {t}°C {h}% NH3:{n}ppm\n"
        for hid_str, eq in _equipment.items():
            hname = _get_hangar_name(int(hid_str))
            msg  += f"{hname}: Fan:{eq['fan']} Chauffage:{eq['heater']}\n"
        send_sms(msg.strip())
        return

    if parts[0] == "RESET":
        if len(parts) > 1 and parts[1] != "ALL":
            hangar_id = _parse_hangar_arg(parts[1:])
            if hangar_id and str(hangar_id) in _equipment:
                for k in _equipment[str(hangar_id)]:
                    _equipment[str(hangar_id)][k] = "AUTO"
                apply_relays(hangar_id)
                for eq in ["eq_fan", "eq_heater", "eq_mister", "eq_ventilation"]:
                    notify_cloud_override_hangar(hangar_id, eq, "AUTO")
                send_sms(f"✅ {_get_hangar_name(hangar_id)} — tous équipements en AUTO")
        else:
            for hid_str in _equipment:
                for k in _equipment[hid_str]:
                    _equipment[hid_str][k] = "AUTO"
            for hid_str in _equipment:
                apply_relays(int(hid_str))
                for eq in ["eq_fan", "eq_heater", "eq_mister", "eq_ventilation"]:
                    notify_cloud_override_hangar(int(hid_str), eq, "AUTO")
            send_sms("✅ Tous les hangars — tous équipements en AUTO")
        return

    if (len(parts) >= 3
            and parts[0] in EQUIPMENT_MAP
            and parts[1] in ("ON", "OFF", "AUTO")):
        name      = EQUIPMENT_MAP[parts[0]]
        action    = parts[1]
        hangar_id = _parse_hangar_arg(parts[2:])
        if hangar_id is None:
            send_sms(f"❓ Précisez le hangar: {parts[0]} {action} H1")
            return
        hid_str = str(hangar_id)
        if hid_str not in _equipment:
            send_sms(f"❌ Hangar {_get_hangar_name(hangar_id)} non trouvé")
            return
        _equipment[hid_str][name] = action
        if action != "AUTO":
            set_relay(name, action)
        send_sms(f"✅ {_get_hangar_name(hangar_id)} — {parts[0]} → {action}")
        notify_cloud_override_hangar(hangar_id, f"eq_{name}", action)
        return

    send_sms(
        "❓ Commandes:\n"
        "FAN/HEATER/MISTER/VENTILATION ON|OFF|AUTO H1\n"
        "STATUS [H1]\n"
        "RESET [H1|ALL]"
    )

def notify_cloud_override_hangar(hangar_id, equipment, action):
    try:
        requests.post(
            f"{CLOUD_URL}/sms_override",
            json={"hangar_id": hangar_id, "equipment": equipment, "action": action},
            headers={"X-API-KEY": API_KEY},
            timeout=5
        )
    except Exception:
        pass

# ============================================================
# LOCAL DATABASE
# ============================================================

DB_PATH   = "nsr_box.db"
_db_local = threading.local()

def get_db():
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        _db_local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return _db_local.conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pin         TEXT,
        temperature REAL,
        humidity    REAL,
        ammonia     REAL,
        fan         TEXT,
        heater      TEXT,
        mister      TEXT,
        ventilation TEXT,
        alert_level TEXT,
        timestamp   TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pending (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        payload   TEXT,
        timestamp TEXT,
        sent      INTEGER DEFAULT 0)""")
    conn.commit()

init_db()

# ============================================================
# EQUIPMENT STATE & CONFIG
# ============================================================

_equipment      = {}
_hangar_config  = {}
_prev_decisions = {}

def _ensure_equipment(hangar_id):
    hid_str = str(hangar_id)
    if hid_str not in _equipment:
        _equipment[hid_str] = {
            "fan": "AUTO", "heater": "AUTO",
            "mister": "AUTO", "ventilation": "AUTO"
        }

def apply_relays(hangar_id):
    hid_str = str(hangar_id)
    if hid_str not in _equipment:
        return
    for name, state in _equipment[hid_str].items():
        if state in ("ON", "OFF"):
            set_relay(name, state)

# ============================================================
# THRESHOLDS
# ============================================================

THRESHOLDS = {
    1: {"temp_min": 32, "temp_max": 35, "hum_min": 60, "hum_max": 70, "ammonia_max": 10},
    2: {"temp_min": 29, "temp_max": 32, "hum_min": 60, "hum_max": 70, "ammonia_max": 10},
    3: {"temp_min": 26, "temp_max": 29, "hum_min": 55, "hum_max": 65, "ammonia_max": 15},
    4: {"temp_min": 23, "temp_max": 26, "hum_min": 55, "hum_max": 65, "ammonia_max": 20},
    5: {"temp_min": 18, "temp_max": 23, "hum_min": 50, "hum_max": 60, "ammonia_max": 25},
}

def get_thresh(hangar_id):
    cfg = _hangar_config.get(str(hangar_id)) or _hangar_config.get(hangar_id)
    if cfg:
        return cfg["thresholds"]
    return THRESHOLDS[1]

def get_hangar_for_pin(pin):
    for hid, cfg in _hangar_config.items():
        if pin in cfg.get("pins", []):
            return int(hid)
    return None

# ============================================================
# EQUIPMENT DECISION LOGIC (hysteresis) — per hangar
# Fan = cooling + humidity, Ventilation = NH3 (Push 3)
# ============================================================

def decide(temp, hum, nh3, t, hangar_id):
    hid_str = str(hangar_id)
    _ensure_equipment(hangar_id)

    prev = _prev_decisions.get(
        hangar_id,
        {"fan": "OFF", "heater": "OFF", "mister": "OFF", "ventilation": "OFF"}
    )

    cfg = _hangar_config.get(hid_str, {})
    ov  = cfg.get("overrides", {})
    eq  = _equipment[hid_str]

    def resolve(name, on_cond, off_cond):
        if eq[name] != "AUTO":
            return eq[name]
        if ov.get(f"eq_{name}") in ("ON", "OFF"):
            return ov[f"eq_{name}"]
        if on_cond:
            return "ON"
        if off_cond:
            return "OFF"
        return prev[name]

    heater = resolve("heater",
                     temp <= t["temp_min"],
                     temp >= t["temp_min"] + 1)

    fan    = resolve("fan",
                     temp >= t["temp_max"] or hum >= t["hum_max"],
                     temp <= t["temp_max"] - 1 and hum <= t["hum_max"] - 2)

    mister = resolve("mister",
                     hum <= t["hum_min"] or temp >= t["temp_max"],
                     hum >= t["hum_min"] + 2 and temp <= t["temp_max"] - 1)

    ventil = resolve("ventilation",
                     nh3 >= t["ammonia_max"],
                     nh3 <= t["ammonia_max"] - 2)

    decisions = {"fan": fan, "heater": heater, "mister": mister, "ventilation": ventil}
    _prev_decisions[hangar_id] = decisions

    for name, state in decisions.items():
        if eq[name] == "AUTO" and state in ("ON", "OFF"):
            set_relay(name, state)

    return fan, heater, mister, ventil

# ============================================================
# ALERT LIFECYCLE (Push 4)
#
# Per (pin, condition_type), we track:
#   - opened_at:  when the condition first triggered (for escalation timer)
#   - last_level: 'en_traitement' or 'critical' that we last emitted
#   - last_msg:   the human message for the last abnormal level
#   - stable_since: when we first saw a normal reading after this condition
#                   (None if currently abnormal)
#   - sms_sent:   True once we've fired SMS+call for this condition
#                 (prevents duplicate notifications)
#
# Lifecycle:
#   normal              → emit "log"  (clear tracker)
#   first abnormal      → start opened_at, emit en_traitement
#   abnormal for >10min → emit critical, SMS+call once, set sms_sent=True
#   normal again        → start stable_since, keep emitting last_level
#   stable for >2min    → emit "log", clear tracker entirely
# ============================================================

# Condition types — broad families that share an escalation timer
def _condition_type(temp, hum, nh3, t):
    if nh3  >= t["ammonia_max"]:    return "nh3"
    if temp >= t["temp_max"]:       return "temp_high"
    if temp <= t["temp_min"]:       return "temp_low"
    if hum  >= t["hum_max"]:        return "hum_high"
    if hum  <= t["hum_min"]:        return "hum_low"
    return None  # normal

# Compute severity ("en_traitement" by default, "critical" if value is way out of range)
def _severity(temp, hum, nh3, t, cond):
    if cond == "nh3":
        if nh3 >= t["ammonia_max"] + 2:
            return "critical", f"🚨 Ammoniac critique: {nh3}ppm"
        return "en_traitement", f"⚠️ Ammoniac élevé: {nh3}ppm"
    if cond == "temp_high":
        if temp >= t["temp_max"] + 2:
            return "critical", f"🚨 Température critique: {temp}°C"
        return "en_traitement", f"⚠️ Température élevée: {temp}°C"
    if cond == "temp_low":
        if temp <= t["temp_min"] - 2:
            return "critical", f"🚨 Température trop basse: {temp}°C"
        return "en_traitement", f"⚠️ Température basse: {temp}°C"
    if cond == "hum_high":
        if hum >= t["hum_max"] + 4:
            return "critical", f"🚨 Humidité critique: {hum}%"
        return "en_traitement", f"⚠️ Humidité élevée: {hum}%"
    if cond == "hum_low":
        if hum <= t["hum_min"] - 4:
            return "critical", f"🚨 Humidité trop basse: {hum}%"
        return "en_traitement", f"⚠️ Humidité basse: {hum}%"
    return "log", "Normal"

# Per-(pin, cond) tracker
_alert_state = {}   # key: (pin, cond) → dict with opened_at, last_level, last_msg, stable_since, sms_sent

def _notify_critical(pin, msg):
    """Send SMS + silent call. Called once per (pin, cond) when escalating."""
    full = f"Nexa Sens [{pin}] {msg}"
    send_sms(full)
    # Silent call right after — make sure farmer wakes up
    threading.Thread(target=make_silent_call, daemon=True).start()

def check_alert(temp, hum, nh3, t, pin):
    """Decide the alert level for this reading, applying lifecycle rules.
    Returns (level, msg, condition_type).
       level ∈ {'log', 'en_traitement', 'critical'}
       condition_type ∈ {None, 'nh3', 'temp_high', 'temp_low', 'hum_high', 'hum_low'}
    """
    now  = time.time()
    cond = _condition_type(temp, hum, nh3, t)

    # ── Case A: condition is currently abnormal ──────────────────
    if cond is not None:
        key      = (pin, cond)
        state    = _alert_state.get(key)
        sev, msg = _severity(temp, hum, nh3, t, cond)

        if state is None:
            # New abnormality — start tracking, emit en_traitement (or critical if extreme)
            state = {
                "opened_at":    now,
                "last_level":   sev,
                "last_msg":     msg,
                "stable_since": None,
                "sms_sent":     False
            }
            _alert_state[key] = state
            # Immediate critical (e.g. temp jumps to +2 instantly) → notify right away
            if sev == "critical" and not state["sms_sent"]:
                _notify_critical(pin, msg)
                state["sms_sent"] = True
            return sev, msg, cond

        # Existing tracker — recovering condition resumed, cancel stability counter
        state["stable_since"] = None
        state["last_msg"]     = msg

        # Did it reach extreme severity (instant critical) or has escalation time elapsed?
        elapsed = now - state["opened_at"]
        if sev == "critical" or elapsed >= ALERT_ESCALATION_SECONDS:
            if sev != "critical":
                msg = msg.replace("⚠️", "🚨") + f" — non résolu après {ALERT_ESCALATION_SECONDS}s"
            state["last_level"] = "critical"
            state["last_msg"]   = msg
            if not state["sms_sent"]:
                _notify_critical(pin, msg)
                state["sms_sent"] = True
            return "critical", msg, cond
        else:
            state["last_level"] = "en_traitement"
            return "en_traitement", msg, cond

    # ── Case B: condition is currently normal ────────────────────
    open_states = [(k, v) for k, v in _alert_state.items() if k[0] == pin]
    if not open_states:
        return "log", "Normal", None

    LEVEL_ORDER = {"critical": 2, "en_traitement": 1, "log": 0}
    open_states.sort(key=lambda kv: LEVEL_ORDER.get(kv[1]["last_level"], 0), reverse=True)

    # Walk each open condition and update its stability tracker
    any_still_pending = False
    for key, state in list(open_states):
        if state["stable_since"] is None:
            state["stable_since"] = now
        elapsed_stable = now - state["stable_since"]
        if elapsed_stable >= ALERT_STABILITY_SECONDS:
            del _alert_state[key]
        else:
            any_still_pending = True

    if not any_still_pending:
        return "log", "Normal", None

    remaining = [(k, v) for k, v in _alert_state.items() if k[0] == pin]
    if not remaining:
        return "log", "Normal", None
    remaining.sort(key=lambda kv: LEVEL_ORDER.get(kv[1]["last_level"], 0), reverse=True)
    top_key, top_state = remaining[0]
    # Return the condition name from the key so cloud knows which condition this alert is about
    return top_state["last_level"], top_state["last_msg"], top_key[1]

# ============================================================
# CORE READING PROCESSOR
# ============================================================

def process_reading(pin, temp, humidity, ammonia):
    pin = (pin or "").upper().strip()
    if not pin:
        print("[READING] Rejected: empty pin")
        return

    if not (-5  <= temp     <= 60):
        print(f"[READING] {pin} rejected: T={temp} out of range"); return
    if not (0   <= humidity <= 100):
        print(f"[READING] {pin} rejected: H={humidity} out of range"); return
    if not (0   <= ammonia  <= 200):
        print(f"[READING] {pin} rejected: NH3={ammonia} out of range"); return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _edge_last_seen[pin] = timestamp

    if pin in _edge_offline_alerted:
        _edge_offline_alerted.discard(pin)
        send_sms(f"✅ Capteur reconnecté: {pin}")

    hangar_id = get_hangar_for_pin(pin)
    t         = get_thresh(hangar_id) if hangar_id else THRESHOLDS[1]

    fan, heater, mister, ventil = decide(
        temp, humidity, ammonia, t, hangar_id or 0)
    level, msg, cond_type = check_alert(temp, humidity, ammonia, t, pin)

    print(f"[{timestamp}] {pin} T:{temp}°C H:{humidity}% NH3:{ammonia}ppm | "
          f"Fan:{fan} Heat:{heater} Mist:{mister} Vent:{ventil} | {level}"
          f"{' [' + cond_type + ']' if cond_type else ''}")

    conn = get_db()
    conn.execute(
        """INSERT INTO readings
           (pin, temperature, humidity, ammonia,
            fan, heater, mister, ventilation, alert_level, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pin, temp, humidity, ammonia, fan, heater, mister, ventil, level, timestamp)
    )
    conn.commit()

    payload = {
        "pin": pin, "temperature": temp, "humidity": humidity, "ammonia": ammonia,
        "fan": fan, "heater": heater, "mister": mister, "ventilation": ventil,
        "alert_level": level, "alert": msg,
        "condition_type": cond_type or "normal",   # Push 4 v2: Pi tells cloud the authoritative condition
        "timestamp": timestamp
    }

    if not forward_to_cloud(payload):
        conn.execute(
            "INSERT INTO pending (payload, timestamp) VALUES (?,?)",
            (json.dumps(payload), timestamp)
        )
        conn.commit()

# ============================================================
# MQTT SUBSCRIBER
# ============================================================

def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[MQTT] Connected to {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC, qos=1)
        print(f"[MQTT] Subscribed to {MQTT_TOPIC}")
    else:
        print(f"[MQTT] Connection failed rc={rc}")

def on_mqtt_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        topic_parts = msg.topic.split("/")
        topic_node  = topic_parts[2] if len(topic_parts) >= 3 else None
        pin = (data.get("node_id") or topic_node or "").upper()
        temp     = float(data["temperature"])
        humidity = float(data["humidity"])
        ammonia  = float(data.get("ammonia", data.get("nh3", 0)))
        process_reading(pin, temp, humidity, ammonia)
    except Exception as e:
        print(f"[MQTT ERROR] {e} — payload: {msg.payload[:200]!r}")

def start_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nsr-box")
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            print(f"[MQTT] Disconnected ({e}) — reconnecting in 5s")
            time.sleep(5)

threading.Thread(target=start_mqtt, daemon=True).start()

# ============================================================
# CONFIG PUSH FROM CLOUD
# ============================================================

@app.route("/update_config", methods=["POST"])
def update_config():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    data        = request.get_json()
    new_hangars = data.get("hangars", {})
    _hangar_config.update(new_hangars)
    for hid_str in new_hangars:
        _ensure_equipment(int(hid_str))
    for hid, cfg in _hangar_config.items():
        for pin in cfg.get("pins", []):
            if pin not in _edge_last_seen:
                _edge_last_seen[pin] = "1970-01-01 00:00:00"
    print(f"[CONFIG] Updated from cloud push — {len(_hangar_config)} hangar(s)")
    return jsonify({"ok": True}), 200

# ============================================================
# FORWARD TO CLOUD
# ============================================================

def forward_to_cloud(payload):
    try:
        res = requests.post(
            f"{CLOUD_URL}/receive",
            json=payload,
            headers={"X-API-KEY": API_KEY},
            timeout=10
        )
        return res.status_code == 200
    except Exception:
        return False

# ============================================================
# RETRY THREAD
# ============================================================

def retry_pending():
    while True:
        time.sleep(30)
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT id, payload FROM pending WHERE sent=0 ORDER BY id ASC LIMIT 20"
            ).fetchall()
            if rows:
                print(f"[RETRY] {len(rows)} en attente...")
            for row_id, payload_str in rows:
                if forward_to_cloud(json.loads(payload_str)):
                    conn.execute("UPDATE pending SET sent=1 WHERE id=?", (row_id,))
                    conn.commit()
        except Exception as e:
            print(f"[RETRY ERROR] {e}")

threading.Thread(target=retry_pending, daemon=True).start()

# ============================================================
# HEARTBEAT THREAD
# ============================================================

def heartbeat():
    while True:
        time.sleep(60)
        try:
            requests.post(
                f"{CLOUD_URL}/heartbeat",
                json={"pin": NSR_PIN, "url": MY_URL},
                headers={"X-API-KEY": API_KEY},
                timeout=5
            )
        except Exception:
            pass

threading.Thread(target=heartbeat, daemon=True).start()

# ============================================================
# EDGE WATCHDOG
# ============================================================

_edge_last_seen       = {}
_edge_offline_alerted = set()

def edge_watchdog():
    while True:
        time.sleep(30)
        try:
            cutoff = (datetime.now() - timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
            ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for pin, last_seen in list(_edge_last_seen.items()):
                if last_seen < cutoff:
                    if pin not in _edge_offline_alerted:
                        _edge_offline_alerted.add(pin)
                        send_sms(f"⚠️ Capteur hors ligne: {pin}")
                        hid = get_hangar_for_pin(pin)
                        if hid:
                            forward_to_cloud({
                                "pin": pin,
                                "temperature": 0, "humidity": 0, "ammonia": 0,
                                "fan": "OFF", "heater": "OFF",
                                "mister": "OFF", "ventilation": "OFF",
                                "alert_level": "critical",
                                "alert": f"Capteur hors ligne: {pin}",
                                "timestamp": ts
                            })
        except Exception as e:
            print(f"[EDGE WATCHDOG] {e}")

threading.Thread(target=edge_watchdog, daemon=True).start()

# ============================================================
# INTERNET MONITOR
# ============================================================

_internet_was_up = True

def internet_monitor():
    global _internet_was_up
    while True:
        time.sleep(30)
        try:
            requests.get("https://google.com", timeout=5)
            if not _internet_was_up:
                _internet_was_up = True
                send_sms("✅ Internet rétabli — données synchronisées")
        except Exception:
            if _internet_was_up:
                _internet_was_up = False
                send_sms("📵 Internet indisponible — surveillance locale active")

threading.Thread(target=internet_monitor, daemon=True).start()

# ============================================================
# POWER MONITOR
# ============================================================

_power_was_on = True

def power_monitor():
    global _power_was_on
    while True:
        time.sleep(5)
        if not UPS_ENABLED:
            continue
        try:
            with open(UPS_AC_PATH) as f:
                ac_on = f.read().strip() == "1"
            if _power_was_on and not ac_on:
                _power_was_on = False
                send_sms("⚡ Coupure d'alimentation — système sur batterie")
                forward_to_cloud({
                    "pin": NSR_PIN,
                    "temperature": 0, "humidity": 0, "ammonia": 0,
                    "fan": "OFF", "heater": "OFF", "mister": "OFF", "ventilation": "OFF",
                    "alert_level": "critical",
                    "alert": "Coupure d'alimentation détectée",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            elif not _power_was_on and ac_on:
                _power_was_on = True
                send_sms("✅ Alimentation rétablie")
        except Exception:
            pass

threading.Thread(target=power_monitor, daemon=True).start()

# ============================================================
# STATUS ENDPOINT
# ============================================================

@app.route("/status")
def status():
    conn = get_db()
    rows = conn.execute(
        """SELECT pin, temperature, humidity, ammonia,
                  fan, heater, mister, ventilation, alert_level, timestamp
           FROM readings
           WHERE id IN (SELECT MAX(id) FROM readings GROUP BY pin)
           ORDER BY timestamp DESC"""
    ).fetchall()
    result = {}
    for r in rows:
        result[r[0]] = {
            "temperature": r[1], "humidity": r[2], "ammonia": r[3],
            "fan": r[4], "heater": r[5], "mister": r[6], "ventilation": r[7],
            "alert_level": r[8], "timestamp": r[9]
        }
    return jsonify({
        "status":   result,
        "equipment": _equipment,
        "internet": _internet_was_up,
        "power":    _power_was_on
    }), 200

# ============================================================
# STARTUP
# ============================================================

def startup_sync():
    time.sleep(5)
    print("[STARTUP] Pulling config from cloud...")
    try:
        res = requests.get(
            f"{CLOUD_URL}/nsr_config/{NSR_PIN}",
            headers={"X-API-KEY": API_KEY},
            timeout=15
        )
        if res.status_code == 200:
            data = res.json()
            _hangar_config.update(data.get("hangars", {}))
            print(f"[STARTUP] Config loaded — {len(_hangar_config)} hangar(s)")
            for hid_str in _hangar_config:
                _ensure_equipment(int(hid_str))
            for hid, cfg in _hangar_config.items():
                for pin in cfg.get("pins", []):
                    if pin not in _edge_last_seen:
                        _edge_last_seen[pin] = "1970-01-01 00:00:00"
                        print(f"[STARTUP] Watching pin: {pin}")
        else:
            print(f"[STARTUP] Config pull failed: {res.status_code} — using defaults")
    except Exception as e:
        print(f"[STARTUP] Cloud unreachable: {e} — using defaults")

threading.Thread(target=startup_sync, daemon=True).start()

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("=" * 55)
    print("  NSR-BOX Server — Nexa Sens")
    print(f"  Cloud      : {CLOUD_URL}")
    print(f"  NSR PIN    : {NSR_PIN}")
    print(f"  My URL     : {MY_URL}")
    print(f"  MQTT       : {MQTT_BROKER}:{MQTT_PORT}  topic={MQTT_TOPIC}")
    print(f"  Alert esc  : {ALERT_ESCALATION_SECONDS}s")
    print(f"  Alert stab : {ALERT_STABILITY_SECONDS}s")
    print(f"  GPIO       : {'ON' if GPIO_ENABLED else 'OFF (no relays)'}")
    print(f"  GSM SMS    : {'ON' if GSM_ENABLED else 'OFF (no modem)'}")
    print(f"  UPS Power  : {'ON' if UPS_ENABLED else 'OFF (no UPS)'}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=False)