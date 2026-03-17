# ============================================================
# NS-EDGE SIMULATOR — Nexa Sens  v3
# Runs on PC — simulates real ESP32 NS-Edge devices
#
# DATA FLOW:
#   simulator.py (PC) → server.py (Raspberry Pi) → cloud (Render)
#
# The simulator sends to the Pi just like a real ESP32 would.
# The Pi then forwards to the cloud.
# ============================================================

import requests, time, random
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================

# ── Change this to your Pi's IP address ──────────────────────
PI_IP         = "192.168.43.181"
# ── Uncomment the line you want to use ───────────────────────
SERVER_URL    = "https://nexasens.onrender.com/receive"  # Render (active)
# SERVER_URL  = f"http://{PI_IP}:5000/receive"           # Pi (when ready)
# SERVER_URL  = "http://127.0.0.1:5000/receive"          # Local PC test

# ── If you want to test WITHOUT the Pi (direct to cloud) ─────
# SERVER_URL  = "http://127.0.0.1:5000/receive"         # local PC
# SERVER_URL  = "https://your-app.onrender.com/receive"  # Render

API_KEY       = "NEXASENS_SECRET_KEY"
SEND_INTERVAL = 30  # seconds between each send cycle

# ============================================================
# EDGES TO SIMULATE
# Add the PINs you registered in the dashboard.
# Each PIN is tied to a specific hangar in the cloud.
# ============================================================

EDGES = {
    "ED01": {"temp": 33.0, "humidity": 65.0, "ammonia": 8.0},  # Hangar 1
    "ED02": {"temp": 32.5, "humidity": 63.0, "ammonia": 7.5},  # Hangar 1
    # "ED03": {"temp": 31.5, "humidity": 61.0, "ammonia": 9.0}, # Hangar 2 (uncomment when registered)
    # "ED04": {"temp": 34.0, "humidity": 67.0, "ammonia": 8.5}, # Hangar 2 (uncomment when registered)
}

# ============================================================
# SIMULATE READING
# Averages 6 samples like a real ESP32 does
# ============================================================

def simulate_reading(base):
    samples = [{"temp":     base["temp"]     + random.uniform(-1.5, 1.5),
                "humidity": base["humidity"] + random.uniform(-3.0, 3.0),
                "ammonia":  base["ammonia"]  + random.uniform(-2.0, 2.0)}
               for _ in range(6)]
    temp     = round(sum(s["temp"]     for s in samples) / 6, 2)
    humidity = round(max(0, min(100, sum(s["humidity"] for s in samples) / 6)), 2)
    ammonia  = round(max(0, sum(s["ammonia"]  for s in samples) / 6), 2)
    return temp, humidity, ammonia

# ============================================================
# SEND TO PI (or directly to cloud if Pi not available)
# ============================================================

def send(pin, temp, humidity, ammonia):
    payload = {"pin": pin, "temperature": temp,
               "humidity": humidity, "ammonia": ammonia}
    try:
        r = requests.post(SERVER_URL, json=payload,
                          headers={"X-API-KEY": API_KEY}, timeout=5)
        if r.status_code == 200:
            resp = r.json()
            print(f"  ✓ {pin} → Hangar {resp.get('hangar_id','?')} "
                  f"({resp.get('node_id','?')}) "
                  f"| T:{temp}°C H:{humidity}% NH3:{ammonia}ppm")
        elif r.status_code == 403:
            print(f"  ✗ {pin} → PIN non enregistré dans le dashboard")
        elif r.status_code == 422:
            print(f"  ✗ {pin} → Valeur hors limites: {r.json().get('error')}")
        else:
            print(f"  ✗ {pin} → Erreur {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"  ✗ {pin} → Connexion échouée: {e}")

# ============================================================
# MAIN LOOP
# ============================================================

print("=" * 58)
print("  NS-Edge Simulator — Nexa Sens  v3")
print(f"  Cible   : {SERVER_URL}")
print(f"  Edges   : {len(EDGES)}")
for pin in EDGES:
    print(f"    • PIN {pin}")
print("=" * 58)
print()
print("  ⚠️  Assurez-vous que ces PINs sont enregistrés")
print("      dans le dashboard avant de lancer!")
print()

while True:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] Envoi ({len(EDGES)} edge(s))...")
    for pin, base in EDGES.items():
        temp, humidity, ammonia = simulate_reading(base)
        send(pin, temp, humidity, ammonia)
        time.sleep(0.5)
    print(f"  ⏳ Prochain envoi dans {SEND_INTERVAL}s\n")
    time.sleep(SEND_INTERVAL)