import requests, time, random
from datetime import datetime
# ============================================================
# CONFIGURATION
# ============================================================
PI_IP         = "192.168.43.181"
SERVER_URL    = f"http://https://nexasens.onrender.com/receive"
API_KEY       = "NEXASENS_SECRET_KEY"
SEND_INTERVAL = 30 
# ============================================================
# EDGES TO SIMULATE
# Add the PINs you registered in the dashboard.
# Each PIN is tied to a specific hangar in the cloud.
# ============================================================

EDGES = {
    "ED01": {"temp": 33.0, "humidity": 65.0, "ammonia": 8.0},  
    "ED02": {"temp": 32.5, "humidity": 63.0, "ammonia": 7.5},  
     "ED03": {"temp": 31.5, "humidity": 61.0, "ammonia": 9.0}, 
     "ED04": {"temp": 34.0, "humidity": 67.0, "ammonia": 8.5}, 
}

# ============================================================
# SIMULATE READING
# Averages 6 samples 
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
# SEND TO PI 
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