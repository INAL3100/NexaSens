# ============================================================
# NS-EDGE SIMULATOR — Nexa Sens v4
# Realistic simulation — temperature/humidity/ammonia follow
# a wave pattern that crosses thresholds and comes back down
# triggering the full alert cycle: Normal → Notify → Critical → Normal
# ============================================================

import requests, time, math
from datetime import datetime

# ── SERVER TARGET ─────────────────────────────────────────────
PI_IP      = "192.168.43.181"
SERVER_URL = "https://nexasens.onrender.com/receive"   # Render
# SERVER_URL = f"http://{PI_IP}:5000/receive"          # Pi (when ready)
# SERVER_URL = "http://127.0.0.1:5000/receive"         # Local test

API_KEY       = "NEXASENS_SECRET_KEY"
SEND_INTERVAL = 30  # seconds between cycles

# ── EDGES ─────────────────────────────────────────────────────
# Only list PINs that are registered in the dashboard!
# Each edge has a wave pattern centered around a base value
# The wave amplitude makes it cross thresholds naturally
#
# wave_center : the middle value of the wave
# wave_amp    : how far above/below center it goes
# wave_period : how many cycles for one full wave (up and back)
# noise       : small random variation per reading

EDGES = {
    "ED01": {
        "temp":     {"center": 33.0, "amp": 4.0,  "period": 20, "noise": 0.3},
        "humidity": {"center": 65.0, "amp": 6.0,  "period": 25, "noise": 1.0},
        "ammonia":  {"center": 8.0,  "amp": 3.0,  "period": 30, "noise": 0.5},
    },
    "ED02": {
        "temp":     {"center": 32.0, "amp": 4.5,  "period": 22, "noise": 0.3},
        "humidity": {"center": 64.0, "amp": 7.0,  "period": 28, "noise": 1.0},
        "ammonia":  {"center": 7.5,  "amp": 3.5,  "period": 35, "noise": 0.5},
    },
    # Uncomment when registered in dashboard:
    # "ED03": {
    #     "temp":     {"center": 27.0, "amp": 4.0,  "period": 18, "noise": 0.3},
    #     "humidity": {"center": 60.0, "amp": 6.0,  "period": 22, "noise": 1.0},
    #     "ammonia":  {"center": 12.0, "amp": 4.0,  "period": 25, "noise": 0.5},
    # },
    # "ED04": {
    #     "temp":     {"center": 28.0, "amp": 3.5,  "period": 20, "noise": 0.3},
    #     "humidity": {"center": 62.0, "amp": 5.0,  "period": 24, "noise": 1.0},
    #     "ammonia":  {"center": 11.0, "amp": 3.0,  "period": 28, "noise": 0.5},
    # },
}

# ── WAVE GENERATOR ────────────────────────────────────────────
import random

cycle = 0  # global cycle counter

def wave_value(w, cycle):
    """Generate a realistic value using a sine wave + noise"""
    sine    = math.sin(2 * math.pi * cycle / w["period"])
    noise   = random.uniform(-w["noise"], w["noise"])
    value   = w["center"] + (w["amp"] * sine) + noise
    return round(value, 2)

# ── SEND ──────────────────────────────────────────────────────
def send(pin, temp, humidity, ammonia):
    payload = {"pin": pin, "temperature": temp,
               "humidity": humidity, "ammonia": ammonia}
    try:
        r = requests.post(SERVER_URL, json=payload,
                          headers={"X-API-KEY": API_KEY}, timeout=8)
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

# ── MAIN LOOP ─────────────────────────────────────────────────
print("=" * 58)
print("  NS-Edge Simulator — Nexa Sens v4 (realistic)")
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
    print(f"[{ts}] Cycle {cycle} — Envoi ({len(EDGES)} edge(s))...")
    for pin, waves in EDGES.items():
        temp     = wave_value(waves["temp"],     cycle)
        humidity = round(max(0, min(100, wave_value(waves["humidity"], cycle))), 2)
        ammonia  = round(max(0, wave_value(waves["ammonia"],  cycle)), 2)
        send(pin, temp, humidity, ammonia)
        time.sleep(0.5)
    print(f"  ⏳ Prochain envoi dans {SEND_INTERVAL}s\n")
    cycle += 1
    time.sleep(SEND_INTERVAL)