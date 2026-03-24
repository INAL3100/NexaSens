# ============================================================
# NSR-BOX SERVER — Nexa Sens
# Runs on Raspberry Pi
#
# RESPONSIBILITIES:
#   1. Pull config from cloud ONCE on startup
#   2. Receive data from NS-Edge sensors
#   3. Make ALL equipment decisions locally
#   4. Control GPIO relays
#   5. Send SMS alerts directly via GSM modem
#   6. Receive SMS commands from farmer
#   7. Monitor edges (offline detection)
#   8. Monitor power outage via UPS
#   9. Monitor internet connectivity
#   10. Send heartbeat to cloud every 60s
#   11. Forward data to cloud, retry when offline
# ============================================================

from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import sqlite3, requests, threading, time, json, os

app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

CLOUD_URL    = os.environ.get("CLOUD_URL", "https://nexasens.onrender.com")
API_KEY      = os.environ.get("API_KEY",   "NEXASENS_SECRET_KEY")
NSR_PIN      = os.environ.get("NSR_PIN",   "NSR1")
MY_URL       = os.environ.get("MY_URL",    "http://192.168.43.181:5000")  # Pi's own URL

# ── GSM MODEM ─────────────────────────────────────────────────
GSM_ENABLED  = False
GSM_PORT     = "/dev/ttyUSB0"
GSM_BAUDRATE = 115200
FARMER_PHONE = "+213XXXXXXXXX"

# ── GPIO RELAYS ───────────────────────────────────────────────
GPIO_ENABLED = False
GPIO_PINS    = {"fan":17, "heater":27, "mister":22, "ventilation":23}

# ── UPS POWER MONITORING ──────────────────────────────────────
UPS_ENABLED  = False
UPS_AC_PATH  = "/sys/class/power_supply/AC/online"  # adjust for your UPS hat

# ============================================================
# GPIO
# ============================================================

def setup_gpio():
    if not GPIO_ENABLED: return
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
# GSM SMS
# ============================================================

_modem = None

def setup_gsm():
    global _modem
    if not GSM_ENABLED: return
    try:
        from gsmmodem.modem import GsmModem
        _modem = GsmModem(GSM_PORT, GSM_BAUDRATE)
        _modem.connect()
        _modem.smsReceivedCallback = handle_sms_command
        print(f"[GSM] Ready on {GSM_PORT}")
    except Exception as e:
        print(f"[GSM ERROR] {e}")

def send_sms(message):
    """Send SMS to farmer via GSM modem"""
    if not GSM_ENABLED:
        print(f"[SMS] {FARMER_PHONE}: {message}")
        return
    try:
        if _modem:
            _modem.sendSms(FARMER_PHONE, message)
    except Exception as e:
        print(f"[SMS ERROR] {e}")

threading.Thread(target=setup_gsm, daemon=True).start()

# ============================================================
# SMS COMMAND HANDLER
# ============================================================

EQUIPMENT_MAP = {
    "FAN":"fan","VENTILATEUR":"fan",
    "HEATER":"heater","CHAUFFAGE":"heater",
    "MISTER":"mister","BRUMISATEUR":"mister",
    "VENTILATION":"ventilation"
}

def handle_sms_command(sms):
    cmd    = sms.text.strip().upper()
    parts  = cmd.split()
    print(f"[SMS CMD] '{cmd}'")

    if cmd == "STATUS":
        conn = get_db()
        rows = conn.execute("""SELECT pin,temperature,humidity,ammonia
            FROM readings WHERE id IN (SELECT MAX(id) FROM readings GROUP BY pin)""").fetchall()
        conn.close()
        msg = "📊 Status:\n"
        for pin, t, h, n in rows:
            msg += f"{pin}: {t}°C {h}% NH3:{n}ppm\n"
        msg += f"Fan:{_equipment['fan']} Chauffage:{_equipment['heater']}"
        send_sms(msg)
        return

    if cmd == "RESET":
        for k in _equipment: _equipment[k] = "AUTO"
        apply_relays()
        send_sms("✅ Tous les équipements en AUTO")
        notify_cloud_override()
        return

    if len(parts) == 2 and parts[0] in EQUIPMENT_MAP and parts[1] in ("ON","OFF","AUTO"):
        name   = EQUIPMENT_MAP[parts[0]]
        action = parts[1]
        _equipment[name] = action
        if action != "AUTO": set_relay(name, action)
        send_sms(f"✅ {parts[0]} → {action}")
        notify_cloud_override()
        return

    send_sms("❓ Commandes: FAN ON/OFF/AUTO, HEATER ON/OFF/AUTO, STATUS, RESET")

def notify_cloud_override():
    """Tell cloud about equipment changes so dashboard stays in sync"""
    for name, state in _equipment.items():
        try:
            requests.post(f"{CLOUD_URL}/sms_override",
                         json={"pin": NSR_PIN, "equipment": f"eq_{name}", "action": state},
                         headers={"X-API-KEY": API_KEY}, timeout=5)
        except: pass

# ============================================================
# LOCAL DATABASE
# ============================================================

DB_PATH = "nsr_box.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    return conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pin TEXT, temperature REAL, humidity REAL, ammonia REAL,
        fan TEXT, heater TEXT, mister TEXT, ventilation TEXT,
        alert_level TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payload TEXT, timestamp TEXT, sent INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()

init_db()

# ============================================================
# EQUIPMENT STATE & CONFIG
# ============================================================

_equipment = {"fan":"AUTO","heater":"AUTO","mister":"AUTO","ventilation":"AUTO"}
_hangar_config = {}   # {hangar_id: {thresholds, overrides, pins}}
_prev_decisions = {}  # {hangar_id: last decisions}

def apply_relays():
    for name, state in _equipment.items():
        if state in ("ON","OFF"):
            set_relay(name, state)

# ============================================================
# THRESHOLDS
# ============================================================

THRESHOLDS = {
    1: {"temp_min":32,"temp_max":35,"hum_min":60,"hum_max":70,"ammonia_max":10},
    2: {"temp_min":29,"temp_max":32,"hum_min":60,"hum_max":70,"ammonia_max":10},
    3: {"temp_min":26,"temp_max":29,"hum_min":55,"hum_max":65,"ammonia_max":15},
    4: {"temp_min":23,"temp_max":26,"hum_min":55,"hum_max":65,"ammonia_max":20},
    5: {"temp_min":18,"temp_max":23,"hum_min":50,"hum_max":60,"ammonia_max":25},
}

def get_thresh(hangar_id):
    cfg = _hangar_config.get(str(hangar_id)) or _hangar_config.get(hangar_id)
    if cfg: return cfg["thresholds"]
    return THRESHOLDS[1]

def get_hangar_for_pin(pin):
    for hid, cfg in _hangar_config.items():
        if pin in cfg.get("pins", []):
            return int(hid)
    return None

# ============================================================
# EQUIPMENT DECISION LOGIC (hysteresis)
# ============================================================

def decide(temp, hum, nh3, t, hangar_id):
    prev = _prev_decisions.get(hangar_id,
           {"fan":"OFF","heater":"OFF","mister":"OFF","ventilation":"OFF"})

    # Check cloud overrides
    cfg = _hangar_config.get(str(hangar_id), {})
    ov  = cfg.get("overrides", {})

    def resolve(name, on_cond, off_cond):
        if _equipment[name] != "AUTO": return _equipment[name]
        if ov.get(f"eq_{name}") in ("ON","OFF"): return ov[f"eq_{name}"]
        if on_cond:  return "ON"
        if off_cond: return "OFF"
        return prev[name]

    heater = resolve("heater", temp <= t["temp_min"], temp >= t["temp_min"]+1)
    fan    = resolve("fan",    temp >= t["temp_max"] or nh3 >= t["ammonia_max"],
                               temp <= t["temp_max"]-1 and nh3 <= t["ammonia_max"]-1)
    mister = resolve("mister", hum <= t["hum_min"] or temp >= t["temp_max"],
                               hum >= t["hum_min"]+2 and temp <= t["temp_max"]-1)
    ventil = resolve("ventilation", hum >= t["hum_max"]-2 or nh3 >= t["ammonia_max"]-2,
                                    hum <= t["hum_max"]-4 and nh3 <= t["ammonia_max"]-4)

    decisions = {"fan":fan,"heater":heater,"mister":mister,"ventilation":ventil}
    _prev_decisions[hangar_id] = decisions

    # Apply to relays
    for name, state in decisions.items():
        if _equipment[name] == "AUTO" and state in ("ON","OFF"):
            set_relay(name, state)

    return fan, heater, mister, ventil

# ============================================================
# ALERT LOGIC
# ============================================================

_alert_sent = {}  # prevent duplicate SMS

def check_alert(temp, hum, nh3, t, pin):
    if   nh3  >= t["ammonia_max"]+2: level, msg = "critical", f"🚨 Ammoniac critique: {nh3}ppm"
    elif temp >= t["temp_max"]  +2:  level, msg = "critical", f"🚨 Température critique: {temp}°C"
    elif temp <= t["temp_min"]  -2:  level, msg = "critical", f"🚨 Température trop basse: {temp}°C"
    elif hum  >= t["hum_max"]   +4:  level, msg = "critical", f"🚨 Humidité critique: {hum}%"
    elif hum  <= t["hum_min"]   -4:  level, msg = "critical", f"🚨 Humidité trop basse: {hum}%"
    elif nh3  >= t["ammonia_max"]:   level, msg = "notify",   f"⚠️ Ammoniac élevé: {nh3}ppm"
    elif temp >= t["temp_max"]:      level, msg = "notify",   f"⚠️ Température élevée: {temp}°C"
    elif temp <= t["temp_min"]:      level, msg = "notify",   f"⚠️ Température basse: {temp}°C"
    elif hum  >= t["hum_max"]:       level, msg = "notify",   f"⚠️ Humidité élevée: {hum}%"
    elif hum  <= t["hum_min"]:       level, msg = "notify",   f"⚠️ Humidité basse: {hum}%"
    else:
        _alert_sent.pop(pin, None)
        return "log", "Normal"

    key = f"{pin}:{level}"
    if key not in _alert_sent:
        _alert_sent[key] = True
        send_sms(f"Nexa Sens [{pin}] {msg}")

    return level, msg

# ============================================================
# RECEIVE FROM NS-EDGE
# ============================================================

@app.route("/receive", methods=["POST"])
def receive():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error":"Unauthorized"}), 401

    data = request.get_json()
    if not data: return jsonify({"error":"No data"}), 400

    try:
        temp     = float(data["temperature"])
        humidity = float(data["humidity"])
        ammonia  = float(data["ammonia"])
    except (KeyError, ValueError):
        return jsonify({"error":"Données invalides"}), 400

    if not (-5 <= temp     <= 60):  return jsonify({"error":"T hors limites"}), 422
    if not (0  <= humidity <= 100): return jsonify({"error":"H hors limites"}), 422
    if not (0  <= ammonia  <= 200): return jsonify({"error":"NH3 hors limites"}), 422

    pin       = data.get("pin","").upper().strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not pin: return jsonify({"error":"PIN manquant"}), 400

    # Update edge last seen
    _edge_last_seen[pin] = timestamp

    hangar_id = get_hangar_for_pin(pin)
    t         = get_thresh(hangar_id) if hangar_id else THRESHOLDS[1]

    fan, heater, mister, ventil = decide(temp, humidity, ammonia, t, hangar_id or 0)
    level, msg = check_alert(temp, humidity, ammonia, t, pin)

    print(f"[{timestamp}] {pin} T:{temp}°C H:{humidity}% NH3:{ammonia}ppm | "
          f"Fan:{fan} Heat:{heater} | {level}")

    # Save locally
    conn = get_db()
    conn.execute("""INSERT INTO readings
        (pin,temperature,humidity,ammonia,fan,heater,mister,ventilation,alert_level,timestamp)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pin,temp,humidity,ammonia,fan,heater,mister,ventil,level,timestamp))
    conn.commit()

    payload = {"pin":pin,"temperature":temp,"humidity":humidity,"ammonia":ammonia,
               "fan":fan,"heater":heater,"mister":mister,"ventilation":ventil,
               "alert_level":level,"alert":msg,"timestamp":timestamp}

    if not forward_to_cloud(payload):
        conn.execute("INSERT INTO pending (payload,timestamp) VALUES (?,?)",
                     (json.dumps(payload), timestamp))
        conn.commit()
    conn.close()

    return jsonify({"ok":True,"pin":pin,"fan":fan,"heater":heater,
                    "mister":mister,"ventilation":ventil,"alert_level":level}), 200

# ============================================================
# RECEIVE CONFIG PUSH FROM CLOUD
# ============================================================

@app.route("/update_config", methods=["POST"])
def update_config():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json()
    _hangar_config.update(data.get("hangars", {}))
    print(f"[CONFIG] Updated from cloud push — {len(_hangar_config)} hangar(s)")
    return jsonify({"ok":True}), 200

# ============================================================
# FORWARD TO CLOUD
# ============================================================

def forward_to_cloud(payload):
    try:
        res = requests.post(f"{CLOUD_URL}/receive", json=payload,
                            headers={"X-API-KEY": API_KEY}, timeout=10)
        return res.status_code == 200
    except:
        return False

# ============================================================
# RETRY THREAD
# ============================================================

def retry_pending():
    while True:
        time.sleep(30)
        try:
            conn  = get_db()
            rows  = conn.execute("SELECT id,payload FROM pending WHERE sent=0 ORDER BY id ASC LIMIT 20").fetchall()
            if rows: print(f"[RETRY] {len(rows)} en attente...")
            for row_id, payload_str in rows:
                if forward_to_cloud(json.loads(payload_str)):
                    conn.execute("UPDATE pending SET sent=1 WHERE id=?", (row_id,))
                    conn.commit()
            conn.close()
        except Exception as e:
            print(f"[RETRY ERROR] {e}")

threading.Thread(target=retry_pending, daemon=True).start()

# ============================================================
# HEARTBEAT THREAD — tells cloud "I'm alive" every 60s
# ============================================================

def heartbeat():
    while True:
        time.sleep(60)
        try:
            requests.post(f"{CLOUD_URL}/heartbeat",
                         json={"pin": NSR_PIN, "url": MY_URL},
                         headers={"X-API-KEY": API_KEY}, timeout=5)
        except:
            pass  # offline — no problem, just retry next time

threading.Thread(target=heartbeat, daemon=True).start()

# ============================================================
# EDGE WATCHDOG — detects when an edge goes offline
# ============================================================

_edge_last_seen = {}  # {pin: last_seen_timestamp}
_edge_offline_alerted = set()

def edge_watchdog():
    while True:
        time.sleep(90)
        try:
            ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cutoff = (datetime.now()-timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
            for pin, last_seen in list(_edge_last_seen.items()):
                if last_seen < cutoff:
                    if pin not in _edge_offline_alerted:
                        _edge_offline_alerted.add(pin)
                        msg = f"⚠️ Capteur hors ligne: {pin}"
                        send_sms(msg)
                        # Tell cloud
                        hid = get_hangar_for_pin(pin)
                        if hid:
                            forward_to_cloud({
                                "pin": pin, "temperature": 0, "humidity": 0, "ammonia": 0,
                                "fan":"OFF","heater":"OFF","mister":"OFF","ventilation":"OFF",
                                "alert_level":"critical",
                                "alert": f"Capteur hors ligne: {pin}",
                                "timestamp": ts
                            })
                else:
                    if pin in _edge_offline_alerted:
                        _edge_offline_alerted.discard(pin)
                        send_sms(f"✅ Capteur reconnecté: {pin}")
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
        except:
            if _internet_was_up:
                _internet_was_up = False
                send_sms("📵 Internet indisponible — surveillance locale active")

threading.Thread(target=internet_monitor, daemon=True).start()

# ============================================================
# POWER MONITOR (UPS hat)
# ============================================================

_power_was_on = True

def power_monitor():
    global _power_was_on
    while True:
        time.sleep(5)
        try:
            if not UPS_ENABLED: continue
            with open(UPS_AC_PATH) as f:
                ac_on = f.read().strip() == "1"
            if _power_was_on and not ac_on:
                _power_was_on = False
                send_sms("⚡ Coupure d'alimentation — système sur batterie")
                forward_to_cloud({
                    "pin": NSR_PIN, "temperature":0,"humidity":0,"ammonia":0,
                    "fan":"OFF","heater":"OFF","mister":"OFF","ventilation":"OFF",
                    "alert_level":"critical",
                    "alert":"Coupure d'alimentation détectée",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            elif not _power_was_on and ac_on:
                _power_was_on = True
                send_sms("✅ Alimentation rétablie")
        except:
            pass

threading.Thread(target=power_monitor, daemon=True).start()

# ============================================================
# STATUS ENDPOINT
# ============================================================

@app.route("/status")
def status():
    conn = get_db()
    rows = conn.execute("""SELECT pin,temperature,humidity,ammonia,
        fan,heater,mister,ventilation,alert_level,timestamp
        FROM readings WHERE id IN (SELECT MAX(id) FROM readings GROUP BY pin)
        ORDER BY timestamp DESC""").fetchall()
    conn.close()
    result = {}
    for r in rows:
        result[r[0]] = {"temperature":r[1],"humidity":r[2],"ammonia":r[3],
                        "fan":r[4],"heater":r[5],"mister":r[6],"ventilation":r[7],
                        "alert_level":r[8],"timestamp":r[9]}
    return jsonify({"status":result,"equipment":_equipment,
                    "internet":_internet_was_up,"power":_power_was_on}), 200

# ============================================================
# STARTUP — pull config from cloud once
# ============================================================

def startup_sync():
    time.sleep(5)  # wait for Flask to start
    print("[STARTUP] Pulling config from cloud...")
    try:
        res = requests.get(f"{CLOUD_URL}/nsr_config/{NSR_PIN}",
                          headers={"X-API-KEY": API_KEY}, timeout=15)
        if res.status_code == 200:
            _hangar_config.update(res.json().get("hangars", {}))
            print(f"[STARTUP] Config loaded — {len(_hangar_config)} hangar(s)")
        else:
            print(f"[STARTUP] Config pull failed: {res.status_code}")
    except Exception as e:
        print(f"[STARTUP] Cloud unreachable: {e} — using defaults")

threading.Thread(target=startup_sync, daemon=True).start()

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("=" * 55)
    print("  NSR-BOX Server — Nexa Sens")
    print(f"  Cloud     : {CLOUD_URL}")
    print(f"  NSR PIN   : {NSR_PIN}")
    print(f"  My URL    : {MY_URL}")
    print(f"  GPIO      : {'ON' if GPIO_ENABLED else 'OFF (no relays)'}")
    print(f"  GSM SMS   : {'ON' if GSM_ENABLED else 'OFF (no modem)'}")
    print(f"  UPS Power : {'ON' if UPS_ENABLED else 'OFF (no UPS)'}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=False)