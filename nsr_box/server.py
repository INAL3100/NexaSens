# ============================================================
# NSR-BOX SERVER — Nexa Sens
# Runs on Raspberry Pi
# Receives data from NS-Edge devices (or simulator on PC)
# Forwards every reading directly to the cloud
# ============================================================

from flask import Flask, request, jsonify
from datetime import datetime
import sqlite3, requests, threading, time, json, os

app = Flask(__name__)

# ============================================================
# CONFIGURATION — change CLOUD_URL after Render deployment
# ============================================================

CLOUD_URL = os.environ.get("CLOUD_URL", "http://127.0.0.1:5000/receive")
API_KEY   = os.environ.get("API_KEY",   "NEXASENS_SECRET_KEY")

# ============================================================
# LOCAL DATABASE
# Stores readings locally in case cloud is unreachable
# Retries failed forwards automatically
# ============================================================

db = sqlite3.connect("nsr_box.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pin       TEXT,
    node_id   TEXT,
    temperature REAL,
    humidity  REAL,
    ammonia   REAL,
    timestamp TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS pending (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    payload   TEXT,
    timestamp TEXT,
    sent      INTEGER DEFAULT 0
)""")
db.commit()

# ============================================================
# RECEIVE FROM NS-EDGE (or simulator)
# NS-Edge sends its PIN + sensor values
# NSR-BOX stores locally then forwards to cloud immediately
# ============================================================

@app.route("/receive", methods=["POST"])
def receive():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    pin       = data.get("pin", "").upper().strip()
    temp      = float(data.get("temperature"))
    humidity  = float(data.get("humidity"))
    ammonia   = float(data.get("ammonia"))
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not pin:
        return jsonify({"error": "PIN manquant"}), 400

    # Validate sensor ranges
    if not (-5 <= temp <= 60):
        return jsonify({"error": f"Température hors limites: {temp}"}), 422
    if not (0 <= humidity <= 100):
        return jsonify({"error": f"Humidité hors limites: {humidity}"}), 422
    if not (0 <= ammonia <= 200):
        return jsonify({"error": f"Ammoniac hors limites: {ammonia}"}), 422

    # Save locally
    db.execute("INSERT INTO readings (pin,node_id,temperature,humidity,ammonia,timestamp) VALUES (?,?,?,?,?,?)",
               (pin, pin, temp, humidity, ammonia, timestamp))
    db.commit()

    print(f"[{timestamp}] PIN:{pin} | T:{temp}°C H:{humidity}% NH3:{ammonia}ppm")

    # Build payload for cloud — pass PIN so cloud resolves node/hangar
    payload = {
        "pin":         pin,
        "temperature": temp,
        "humidity":    humidity,
        "ammonia":     ammonia,
        "timestamp":   timestamp
    }

    # Try to forward to cloud immediately
    success = forward_to_cloud(payload)

    if not success:
        # Save to pending queue for retry
        db.execute("INSERT INTO pending (payload, timestamp) VALUES (?,?)",
                   (json.dumps(payload), timestamp))
        db.commit()
        print(f"  → Cloud unreachable, saved to pending queue")

    return jsonify({"ok": True, "pin": pin, "forwarded": success}), 200

# ============================================================
# FORWARD A SINGLE READING TO CLOUD
# ============================================================

def forward_to_cloud(payload):
    try:
        res = requests.post(
            CLOUD_URL,
            json=payload,
            headers={"X-API-KEY": API_KEY},
            timeout=10
        )
        if res.status_code == 200:
            print(f"  → Cloud ✓ ({res.status_code})")
            return True
        else:
            print(f"  → Cloud ✗ ({res.status_code}): {res.text[:100]}")
            return False
    except Exception as e:
        print(f"  → Cloud ✗ (connexion échouée): {e}")
        return False

# ============================================================
# RETRY THREAD — retries failed forwards every 30 seconds
# ============================================================

def retry_pending():
    while True:
        time.sleep(30)
        try:
            cur = db.execute("SELECT id, payload FROM pending WHERE sent=0 ORDER BY id ASC LIMIT 20")
            rows = cur.fetchall()
            if rows:
                print(f"[RETRY] {len(rows)} envoi(s) en attente...")
            for row_id, payload_str in rows:
                payload = json.loads(payload_str)
                if forward_to_cloud(payload):
                    db.execute("UPDATE pending SET sent=1 WHERE id=?", (row_id,))
                    db.commit()
                    print(f"  → ID {row_id} envoyé avec succès")
        except Exception as e:
            print(f"[RETRY ERROR] {e}")

threading.Thread(target=retry_pending, daemon=True).start()

# ============================================================
# STATUS — shows last reading per PIN
# ============================================================

@app.route("/status", methods=["GET"])
def status():
    cur = db.execute("""
        SELECT pin, temperature, humidity, ammonia, timestamp
        FROM readings
        WHERE id IN (
            SELECT MAX(id) FROM readings GROUP BY pin
        )
        ORDER BY timestamp DESC
    """)
    rows = cur.fetchall()
    result = {}
    for pin, temp, hum, nh3, ts in rows:
        result[pin] = {
            "temperature": temp,
            "humidity":    hum,
            "ammonia":     nh3,
            "timestamp":   ts
        }
    return jsonify(result), 200

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  NSR-BOX Server — Nexa Sens")
    print(f"  Cloud URL : {CLOUD_URL}")
    print(f"  Listening : 0.0.0.0:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)