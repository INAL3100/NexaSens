# ============================================================
# CLOUD SERVER — Nexa Sens v10 (final)
# ============================================================

from flask import Flask, request, jsonify, render_template, redirect, session
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, random, string, threading, time, os

app = Flask(__name__)

# BUG 4 FIX — secrets from environment variables
app.secret_key = os.environ.get("SECRET_KEY", "nexasens_dev_secret")
API_KEY        = os.environ.get("API_KEY",    "NEXASENS_SECRET_KEY")

# ── SMS ───────────────────────────────────────────────────────────
TWILIO_ENABLED = os.environ.get("TWILIO_ENABLED", "false").lower() == "true"
TWILIO_SID     = os.environ.get("TWILIO_SID",   "")
TWILIO_TOKEN   = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM    = os.environ.get("TWILIO_FROM",  "")

def send_sms(to, msg):
    if not TWILIO_ENABLED:
        print(f"[SMS] {to}: {msg}")
        return
    try:
        from twilio.rest import Client
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(to=to, from_=TWILIO_FROM, body=msg)
    except Exception as e:
        print(f"[SMS ERROR] {e}")

# BUG 3 FIX — thread-safe DB connections
DB_PATH   = "nexasens_cloud.db"
_db_local = threading.local()

def get_db():
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        _db_local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return _db_local.conn

def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    return cur.fetchone() if one else cur.fetchall()

def execute(sql, params=()):
    conn = get_db()
    cur  = conn.execute(sql, params)
    conn.commit()
    return cur

# ── DATABASE SETUP ────────────────────────────────────────────────
def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE, password TEXT, phone TEXT,
        role TEXT DEFAULT 'client', active INTEGER DEFAULT 1)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pin TEXT UNIQUE, pin_type TEXT,
        used INTEGER DEFAULT 0, used_by INTEGER DEFAULT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hangars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, name TEXT, flock_age INTEGER DEFAULT 1,
        temp_min REAL DEFAULT 32, temp_max REAL DEFAULT 35,
        hum_min REAL DEFAULT 60, hum_max REAL DEFAULT 70,
        ammonia_max REAL DEFAULT 10, nsr_pin TEXT DEFAULT NULL,
        eq_fan TEXT DEFAULT 'AUTO', eq_heater TEXT DEFAULT 'AUTO',
        eq_mister TEXT DEFAULT 'AUTO', eq_ventilation TEXT DEFAULT 'AUTO')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, hangar_id INTEGER,
        node_id TEXT, pin TEXT,
        node_type TEXT DEFAULT 'ns_edge',
        active INTEGER DEFAULT 1, last_seen TEXT DEFAULT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hangar_id INTEGER, node_id TEXT,
        temperature REAL, humidity REAL, ammonia REAL,
        fan TEXT, heater TEXT, mister TEXT, ventilation TEXT,
        alert_level TEXT DEFAULT 'log', alert TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hangar_id INTEGER, node_id TEXT, message TEXT,
        level TEXT DEFAULT 'log',
        status TEXT DEFAULT 'Non traité', timestamp TEXT)""")
    conn.commit()
    conn.execute("INSERT OR IGNORE INTO users VALUES (NULL,'admin',?,'','admin',1)",
                 (generate_password_hash("admin1234"),))
    for pin, pt in [("NSR1","nsr_box"),("NSR2","nsr_box"),("NSR3","nsr_box"),
                    ("ED01","ns_edge"),("ED02","ns_edge"),("ED03","ns_edge"),
                    ("ED04","ns_edge"),("ED05","ns_edge"),("ED06","ns_edge")]:
        conn.execute("INSERT OR IGNORE INTO pins VALUES (NULL,?,?,0,NULL)", (pin, pt))
    conn.commit()

init_db()

# ── THRESHOLDS ────────────────────────────────────────────────────
THRESHOLDS = {
    1: {"temp_min":32,"temp_max":35,"hum_min":60,"hum_max":70,"ammonia_max":10},
    2: {"temp_min":29,"temp_max":32,"hum_min":60,"hum_max":70,"ammonia_max":10},
    3: {"temp_min":26,"temp_max":29,"hum_min":55,"hum_max":65,"ammonia_max":15},
    4: {"temp_min":23,"temp_max":26,"hum_min":55,"hum_max":65,"ammonia_max":20},
    5: {"temp_min":18,"temp_max":23,"hum_min":50,"hum_max":60,"ammonia_max":25},
}

# BUG 1 FIX — reads actual saved thresholds from DB
def get_thresh(hangar_id):
    row = query("SELECT temp_min,temp_max,hum_min,hum_max,ammonia_max FROM hangars WHERE id=?",
                (hangar_id,), one=True)
    if row:
        return {"temp_min":row[0],"temp_max":row[1],"hum_min":row[2],
                "hum_max":row[3],"ammonia_max":row[4]}
    return THRESHOLDS[1]

def get_phone(hangar_id):
    row = query("SELECT u.phone FROM users u JOIN hangars h ON h.user_id=u.id WHERE h.id=?",
                (hangar_id,), one=True)
    return row[0] if row else None

# ── EQUIPMENT LOGIC ───────────────────────────────────────────────
def decide(temp, hum, nh3, t, prev=None, hangar_id=None):
    prev = prev or {"fan":"OFF","heater":"OFF","mister":"OFF","ventilation":"OFF"}
    ov   = {"fan":None,"heater":None,"mister":None,"ventilation":None}
    if hangar_id:
        row = query("SELECT eq_fan,eq_heater,eq_mister,eq_ventilation FROM hangars WHERE id=?",
                    (hangar_id,), one=True)
        if row:
            for i, k in enumerate(["fan","heater","mister","ventilation"]):
                if row[i] in ("ON","OFF"): ov[k] = row[i]
    heater = ov["heater"] or ("ON" if temp <= t["temp_min"] else "OFF" if temp >= t["temp_min"]+1 else prev["heater"])
    fan    = ov["fan"]    or ("ON" if temp >= t["temp_max"] or nh3 >= t["ammonia_max"] else "OFF" if temp <= t["temp_max"]-1 and nh3 <= t["ammonia_max"]-1 else prev["fan"])
    mister = ov["mister"] or ("ON" if hum <= t["hum_min"] or temp >= t["temp_max"] else "OFF" if hum >= t["hum_min"]+2 and temp <= t["temp_max"]-1 else prev["mister"])
    ventil = ov["ventilation"] or ("ON" if hum >= t["hum_max"]-2 or nh3 >= t["ammonia_max"]-2 else "OFF" if hum <= t["hum_max"]-4 and nh3 <= t["ammonia_max"]-4 else prev["ventilation"])
    return fan, heater, mister, ventil

# ── ALERT LOGIC ───────────────────────────────────────────────────
def check_alert(temp, hum, nh3, t):
    if nh3  >= t["ammonia_max"]+2: return "critical", f"Ammoniac critique: {nh3} ppm (max {t['ammonia_max']})"
    if temp >= t["temp_max"]  +2:  return "critical", f"Température critique: {temp}°C (max {t['temp_max']})"
    if temp <= t["temp_min"]  -2:  return "critical", f"Température trop basse: {temp}°C (min {t['temp_min']})"
    if hum  >= t["hum_max"]   +4:  return "critical", f"Humidité critique: {hum}% (max {t['hum_max']})"
    if hum  <= t["hum_min"]   -4:  return "critical", f"Humidité trop basse: {hum}% (min {t['hum_min']})"
    if nh3  >= t["ammonia_max"]:   return "notify",   f"Ammoniac élevé: {nh3} ppm (max {t['ammonia_max']})"
    if temp >= t["temp_max"]:      return "notify",   f"Température élevée: {temp}°C (max {t['temp_max']})"
    if temp <= t["temp_min"]:      return "notify",   f"Température basse: {temp}°C (min {t['temp_min']})"
    if hum  >= t["hum_max"]:       return "notify",   f"Humidité élevée: {hum}% (max {t['hum_max']})"
    if hum  <= t["hum_min"]:       return "notify",   f"Humidité basse: {hum}% (min {t['hum_min']})"
    return "log", "Normal"

# ── WATCHDOG ──────────────────────────────────────────────────────
def watchdog():
    while True:
        time.sleep(90)
        try:
            init_db()
            ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cutoff = (datetime.now()-timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
            for _, hid, node_id, last_seen in query(
                "SELECT id,hangar_id,node_id,last_seen FROM nodes WHERE active=1 AND node_type='ns_edge'"):
                if last_seen and last_seen < cutoff:
                    if not query("SELECT id FROM alerts WHERE hangar_id=? AND node_id=? AND message LIKE '%hors ligne%' AND status='Non traité'", (hid, node_id), one=True):
                        msg = f"Capteur hors ligne: {node_id}"
                        execute("INSERT INTO alerts (hangar_id,node_id,message,level,status,timestamp) VALUES (?,?,?,'critical','Non traité',?)", (hid, node_id, msg, ts))
                        phone = get_phone(hid)
                        if phone: send_sms(phone, f"🚨 Nexa Sens — {msg}")
                else:
                    execute("UPDATE alerts SET status='Traité' WHERE hangar_id=? AND node_id=? AND message LIKE '%hors ligne%' AND status='Non traité'", (hid, node_id))
        except Exception as e:
            print(f"[WATCHDOG] {e}")

threading.Thread(target=watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u, p = request.form["username"], request.form["password"]
        row = query("SELECT id,password,role,active FROM users WHERE username=?", (u,), one=True)
        if row and check_password_hash(row[1], p):
            if not row[3]: return render_template("login.html", error="Compte désactivé.")
            session.update({"user_id":row[0],"username":u,"role":row[2]})
            return redirect("/admin" if row[2]=="admin" else "/")
        return render_template("login.html", error="Identifiants incorrects")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        phone    = request.form["phone"]
        pin      = request.form["pin"].upper().strip()
        pin_row  = query("SELECT id,used FROM pins WHERE pin=? AND pin_type='nsr_box'", (pin,), one=True)
        if not pin_row: return render_template("register.html", error="PIN NSR-BOX invalide.")
        if pin_row[1]:  return render_template("register.html", error="PIN déjà utilisé.")
        try:
            cur = execute("INSERT INTO users VALUES (NULL,?,?,?,'client',1)",
                          (username, generate_password_hash(password), phone))
            uid = cur.lastrowid
            execute("UPDATE pins SET used=1,used_by=? WHERE id=?", (uid, pin_row[0]))
            t    = THRESHOLDS[1]
            cur2 = execute("INSERT INTO hangars (user_id,name,flock_age,temp_min,temp_max,hum_min,hum_max,ammonia_max,nsr_pin) VALUES (?,?,1,?,?,?,?,?,?)",
                           (uid,"Hangar 1",t["temp_min"],t["temp_max"],t["hum_min"],t["hum_max"],t["ammonia_max"],pin))
            execute("INSERT INTO nodes (user_id,hangar_id,node_id,pin,node_type) VALUES (?,?,?,?,'nsr_box')",
                    (uid, cur2.lastrowid, "NSR-BOX", pin))
            return redirect("/login")
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Nom d'utilisateur déjà pris.")
    return render_template("register.html")

# ═══════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    if "user_id" not in session: return redirect("/login")
    if session["role"] == "admin": return redirect("/admin")
    uid        = session["user_id"]
    master_row = query("SELECT nsr_pin FROM hangars WHERE user_id=? AND nsr_pin IS NOT NULL LIMIT 1", (uid,), one=True)
    master_pin = master_row[0] if master_row else None
    hangars    = []
    for hid, name, age, nsr_pin in query("SELECT id,name,flock_age,nsr_pin FROM hangars WHERE user_id=?", (uid,)):
        t, avg, vals = get_thresh(hid), None, []
        for (nid,) in query("SELECT node_id FROM nodes WHERE hangar_id=? AND active=1 AND node_type='ns_edge'", (hid,)):
            row = query("SELECT temperature,humidity,ammonia,fan,heater,mister,ventilation FROM readings WHERE node_id=? AND hangar_id=? ORDER BY id DESC LIMIT 1", (nid, hid), one=True)
            if row: vals.append(row)
        if vals:
            at = round(sum(v[0] for v in vals)/len(vals),1)
            ah = round(sum(v[1] for v in vals)/len(vals),1)
            an = round(sum(v[2] for v in vals)/len(vals),1)
            prev = {"fan":vals[-1][3],"heater":vals[-1][4],"mister":vals[-1][5],"ventilation":vals[-1][6]}
            fan,heater,mister,ventil = decide(at,ah,an,t,prev,hid)
            al, am = check_alert(at,ah,an,t)
            avg = {"temperature":at,"humidity":ah,"ammonia":an,"fan":fan,"heater":heater,
                   "mister":mister,"ventilation":ventil,"alert_level":al,"alert":am}
        alert_count = query("SELECT COUNT(*) FROM alerts WHERE hangar_id=? AND level='critical' AND status='Non traité'", (hid,), one=True)[0]
        hangars.append({"id":hid,"name":name,"flock_age":age,"nsr_pin":nsr_pin or master_pin,
                        "thresholds":t,"avg":avg,"alert_count":alert_count})
    return render_template("dashboard.html", hangars=hangars, username=session["username"])

# ═══════════════════════════════════════════════════════════════
# HANGAR PAGE
# ═══════════════════════════════════════════════════════════════

@app.route("/hangar/<int:hid>")
def hangar_page(hid):
    if "user_id" not in session: return redirect("/login")
    if session["role"] != "admin":
        if not query("SELECT id FROM hangars WHERE id=? AND user_id=?", (hid, session["user_id"]), one=True):
            return "Accès refusé", 403
    h = query("SELECT name,flock_age,temp_min,temp_max,hum_min,hum_max,ammonia_max,nsr_pin,eq_fan,eq_heater,eq_mister,eq_ventilation FROM hangars WHERE id=?", (hid,), one=True)
    if not h: return "Hangar introuvable", 404
    t      = get_thresh(hid)
    hangar = {"id":hid,"name":h[0],"flock_age":h[1],"temp_min":h[2],"temp_max":h[3],
              "hum_min":h[4],"hum_max":h[5],"ammonia_max":h[6],"nsr_pin":h[7],
              "eq_fan":h[8],"eq_heater":h[9],"eq_mister":h[10],"eq_ventilation":h[11],"eff":t}
    cutoff = (datetime.now()-timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
    nodes  = {}
    for node_id, last_seen, pin in query("SELECT node_id,last_seen,pin FROM nodes WHERE hangar_id=? AND active=1 AND node_type='ns_edge'", (hid,)):
        offline = not last_seen or last_seen < cutoff
        row     = query("SELECT temperature,humidity,ammonia,timestamp FROM readings WHERE node_id=? AND hangar_id=? ORDER BY id DESC LIMIT 1", (node_id, hid), one=True)
        al      = "log"
        if row and not offline: al, _ = check_alert(row[0], row[1], row[2], t)
        nodes[node_id] = {"pin":pin,"temperature":row[0] if row else None,
                          "humidity":row[1] if row else None,"ammonia":row[2] if row else None,
                          "timestamp":row[3] if row else None,
                          "alert_level":"critical" if offline else al,"offline":offline}
    avg, active = None, [n for n in nodes.values() if not n["offline"] and n["temperature"] is not None]
    if active:
        at = round(sum(n["temperature"] for n in active)/len(active),1)
        ah = round(sum(n["humidity"]    for n in active)/len(active),1)
        an = round(sum(n["ammonia"]     for n in active)/len(active),1)
        pr = query("SELECT fan,heater,mister,ventilation FROM readings WHERE hangar_id=? ORDER BY id DESC LIMIT 1", (hid,), one=True)
        prev = {"fan":pr[0],"heater":pr[1],"mister":pr[2],"ventilation":pr[3]} if pr else None
        fan,heater,mister,ventil = decide(at,ah,an,t,prev,hid)
        al, am = check_alert(at,ah,an,t)
        avg = {"temperature":at,"humidity":ah,"ammonia":an,"fan":fan,"heater":heater,
               "mister":mister,"ventilation":ventil,"alert_level":al,"alert":am}
    active_alerts = query("SELECT id,node_id,message,level,status,timestamp FROM alerts WHERE hangar_id=? AND status!='Traité' AND level!='log' ORDER BY id DESC LIMIT 5", (hid,))
    owner         = query("SELECT user_id FROM hangars WHERE id=?", (hid,), one=True)
    return render_template("hangar.html", hangar=hangar, nodes=nodes, avg=avg,
                           active_alerts=active_alerts, username=session["username"],
                           error=request.args.get("error",""), is_admin=session["role"]=="admin",
                           owner_id=owner[0] if owner else None, thresholds=t)

# ═══════════════════════════════════════════════════════════════
# HANGAR ACTIONS
# ═══════════════════════════════════════════════════════════════

@app.route("/set_threshold/<int:hid>", methods=["POST"])
def set_threshold(hid):
    if "user_id" not in session: return jsonify({"error":"not logged in"}), 401
    data  = request.get_json()
    field = data.get("field")
    if field not in ["temp_min","temp_max","hum_min","hum_max","ammonia_max"]:
        return jsonify({"error":"champ invalide"}), 400
    try:
        execute(f"UPDATE hangars SET {field}=? WHERE id=?", (float(data.get("value")), hid))
        return jsonify({"ok":True, "thresholds":get_thresh(hid)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/remove_node/<int:hid>/<node_id>", methods=["POST"])
def remove_node(hid, node_id):
    if "user_id" not in session: return redirect("/login")
    row = query("SELECT pin FROM nodes WHERE hangar_id=? AND node_id=? AND node_type='ns_edge'", (hid, node_id), one=True)
    if row: execute("UPDATE pins SET used=0,used_by=NULL WHERE pin=?", (row[0],))
    execute("DELETE FROM nodes WHERE hangar_id=? AND node_id=? AND node_type='ns_edge'", (hid, node_id))
    return redirect(f"/hangar/{hid}")

@app.route("/add_hangar", methods=["POST"])
def add_hangar():
    if "user_id" not in session: return redirect("/login")
    uid   = session["user_id"]
    count = query("SELECT COUNT(*) FROM hangars WHERE user_id=?", (uid,), one=True)[0]
    if count >= 3: return redirect("/")
    t = THRESHOLDS[1]
    execute("INSERT INTO hangars (user_id,name,flock_age,temp_min,temp_max,hum_min,hum_max,ammonia_max) VALUES (?,?,1,?,?,?,?,?)",
            (uid, f"Hangar {count+1}", t["temp_min"],t["temp_max"],t["hum_min"],t["hum_max"],t["ammonia_max"]))
    return redirect("/")

@app.route("/add_node/<int:hid>", methods=["POST"])
def add_node(hid):
    if "user_id" not in session: return redirect("/login")
    pin     = request.form.get("pin","").upper().strip()
    pin_row = query("SELECT id,used FROM pins WHERE pin=? AND pin_type='ns_edge'", (pin,), one=True)
    if not pin_row: return redirect(f"/hangar/{hid}?error=PIN+invalide")
    if pin_row[1]:  return redirect(f"/hangar/{hid}?error=PIN+déjà+utilisé")
    count = query("SELECT COUNT(*) FROM nodes WHERE hangar_id=? AND active=1 AND node_type='ns_edge'", (hid,), one=True)[0]
    if count >= 10: return redirect(f"/hangar/{hid}?error=Maximum+10+capteurs")
    execute("INSERT INTO nodes (user_id,hangar_id,node_id,pin,node_type) VALUES (?,?,?,?,'ns_edge')",
            (session["user_id"], hid, f"NS-EDGE-{count+1}", pin))
    execute("UPDATE pins SET used=1,used_by=? WHERE id=?", (session["user_id"], pin_row[0]))
    return redirect(f"/hangar/{hid}")

@app.route("/update_age/<int:hid>", methods=["POST"])
def update_age(hid):
    if "user_id" not in session: return redirect("/login")
    age = max(1, min(int(request.form.get("flock_age",1)), 10))
    t   = THRESHOLDS[min(age,5)]
    execute("UPDATE hangars SET flock_age=?,temp_min=?,temp_max=?,hum_min=?,hum_max=?,ammonia_max=? WHERE id=?",
            (age, t["temp_min"],t["temp_max"],t["hum_min"],t["hum_max"],t["ammonia_max"], hid))
    return redirect(f"/hangar/{hid}")

@app.route("/control/<int:hid>/<equipment>/<action>", methods=["POST"])
def control(hid, equipment, action):
    if "user_id" not in session: return redirect("/login")
    if equipment not in ["eq_fan","eq_heater","eq_mister","eq_ventilation"]: return redirect(f"/hangar/{hid}")
    if action not in ["ON","OFF","AUTO"]: return redirect(f"/hangar/{hid}")
    execute(f"UPDATE hangars SET {equipment}=? WHERE id=?", (action, hid))
    return redirect(f"/hangar/{hid}")

# ═══════════════════════════════════════════════════════════════
# RECEIVE DATA
# ═══════════════════════════════════════════════════════════════

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
        return jsonify({"error":"Données manquantes ou invalides"}), 400

    # BUG 2 FIX — sensor validation
    errors = []
    if not (-5 <= temp     <= 60):  errors.append(f"température={temp} hors limites [-5,60]")
    if not (0  <= humidity <= 100): errors.append(f"humidité={humidity} hors limites [0,100]")
    if not (0  <= ammonia  <= 200): errors.append(f"ammoniac={ammonia} hors limites [0,200]")
    if errors: return jsonify({"error":"Valeurs hors limites: " + "; ".join(errors)}), 422

    pin = data.get("pin","").upper().strip()
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = query("SELECT node_id,hangar_id FROM nodes WHERE pin=? AND active=1", (pin,), one=True)
    if not row: return jsonify({"error":f"PIN {pin} non enregistré"}), 403
    node_id, hid = row

    execute("UPDATE nodes SET last_seen=? WHERE pin=?", (ts, pin))
    t    = get_thresh(hid)
    pr   = query("SELECT fan,heater,mister,ventilation FROM readings WHERE hangar_id=? ORDER BY id DESC LIMIT 1", (hid,), one=True)
    prev = {"fan":pr[0],"heater":pr[1],"mister":pr[2],"ventilation":pr[3]} if pr else None
    fan, heater, mister, ventil = decide(temp, humidity, ammonia, t, prev, hid)
    level, msg = check_alert(temp, humidity, ammonia, t)

    execute("""INSERT INTO readings (hangar_id,node_id,temperature,humidity,ammonia,fan,heater,mister,ventilation,alert_level,alert,timestamp)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (hid,node_id,temp,humidity,ammonia,fan,heater,mister,ventil,level,msg,ts))

    if level == "notify":
        # BUG 5 FIX — no duplicate notify alerts
        if not query("SELECT id FROM alerts WHERE hangar_id=? AND node_id=? AND level='notify' AND status='En cours' AND message=?",
                     (hid, node_id, msg), one=True):
            execute("INSERT INTO alerts (hangar_id,node_id,message,level,status,timestamp) VALUES (?,?,?,'notify','En cours',?)",
                    (hid, node_id, msg, ts))
    elif level == "critical":
        execute("INSERT INTO alerts (hangar_id,node_id,message,level,status,timestamp) VALUES (?,?,?,'critical','Non traité',?)",
                (hid, node_id, msg, ts))
        execute("UPDATE alerts SET status='Non traité' WHERE hangar_id=? AND node_id=? AND level='notify' AND status='En cours'",
                (hid, node_id))
        phone = get_phone(hid)
        if phone: send_sms(phone, f"🚨 Nexa Sens — {msg}")
    elif level == "log":
        execute("UPDATE alerts SET status='Traité' WHERE hangar_id=? AND node_id=? AND level='notify' AND status='En cours'",
                (hid, node_id))

    return jsonify({"ok":True,"node_id":node_id,"hangar_id":hid}), 200

# ═══════════════════════════════════════════════════════════════
# HISTORY & ALERTS
# ═══════════════════════════════════════════════════════════════

@app.route("/history/<int:hid>")
def history(hid):
    if "user_id" not in session: return redirect("/login")
    date      = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    node      = request.args.get("node","")
    node_rows = query("SELECT node_id,pin FROM nodes WHERE hangar_id=? AND active=1 AND node_type='ns_edge'", (hid,))
    node_ids  = [r[0] for r in node_rows]
    node_pins = {r[0]:r[1] for r in node_rows}
    if not node and node_ids: node = node_ids[0]
    readings = []
    if node:
        readings = [{"temperature":r[0],"humidity":r[1],"ammonia":r[2],
                     "fan":r[3],"heater":r[4],"mister":r[5],"ventilation":r[6],
                     "alert_level":r[7],"alert":r[8],"timestamp":r[9]}
                    for r in query("SELECT temperature,humidity,ammonia,fan,heater,mister,ventilation,alert_level,alert,timestamp FROM readings WHERE hangar_id=? AND node_id=? AND DATE(timestamp)=? ORDER BY timestamp ASC",
                                   (hid, node, date))]
    name  = query("SELECT name,user_id FROM hangars WHERE id=?", (hid,), one=True)
    owner = name[1] if name else None
    return render_template("history.html", readings=readings, selected_date=date,
                           selected_node=node, node_ids=node_ids, node_pins=node_pins,
                           hangar_id=hid, hangar_name=name[0] if name else "",
                           is_admin=session["role"]=="admin", owner_id=owner)

@app.route("/alerts/<int:hid>")
def alerts_page(hid):
    if "user_id" not in session: return redirect("/login")
    alerts = [{"id":r[0],"node_id":r[1],"message":r[2],"level":r[3],"status":r[4],"timestamp":r[5]}
              for r in query("SELECT id,node_id,message,level,status,timestamp FROM alerts WHERE hangar_id=? ORDER BY id DESC LIMIT 200", (hid,))]
    row   = query("SELECT name,user_id FROM hangars WHERE id=?", (hid,), one=True)
    owner = row[1] if row else None
    return render_template("alerts.html", alerts=alerts, hangar_id=hid,
                           hangar_name=row[0] if row else "",
                           is_admin=session["role"]=="admin", owner_id=owner)

@app.route("/update_alert/<int:aid>", methods=["POST"])
def update_alert(aid):
    if "user_id" not in session: return redirect("/login")
    hid = request.form.get("hangar_id")
    execute("UPDATE alerts SET status=? WHERE id=?", (request.form.get("status"), aid))
    return redirect(f"/alerts/{hid}")

# ═══════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════

@app.route("/admin")
def admin():
    if session.get("role") != "admin": return redirect("/login")
    clients = [{"id":r[0],"username":r[1],"phone":r[2],"active":r[3]}
               for r in query("SELECT id,username,phone,active FROM users WHERE role='client'")]
    pins    = [{"pin":r[0],"type":r[1],"used":r[2]}
               for r in query("SELECT pin,pin_type,used FROM pins ORDER BY id DESC")]
    return render_template("admin.html", clients=clients, pins=pins)

@app.route("/admin/generate_pin", methods=["POST"])
def generate_pin():
    if session.get("role") != "admin": return redirect("/login")
    pin_type = request.form.get("pin_type")
    for _ in range(int(request.form.get("count",1))):
        while True:
            pin = ''.join(random.choices(string.ascii_uppercase+string.digits, k=4))
            if not query("SELECT id FROM pins WHERE pin=?", (pin,), one=True): break
        execute("INSERT INTO pins VALUES (NULL,?,?,0,NULL)", (pin, pin_type))
    return redirect("/admin")

@app.route("/admin/toggle_client/<int:uid>", methods=["POST"])
def toggle_client(uid):
    if session.get("role") != "admin": return redirect("/login")
    cur = query("SELECT active FROM users WHERE id=?", (uid,), one=True)[0]
    execute("UPDATE users SET active=? WHERE id=?", (0 if cur else 1, uid))
    return redirect("/admin")

@app.route("/admin/view/<int:uid>")
def admin_view(uid):
    if session.get("role") != "admin": return redirect("/login")
    u = query("SELECT username FROM users WHERE id=?", (uid,), one=True)
    if not u: return "Introuvable", 404
    master_row = query("SELECT nsr_pin FROM hangars WHERE user_id=? AND nsr_pin IS NOT NULL LIMIT 1", (uid,), one=True)
    master_pin = master_row[0] if master_row else None
    hangars    = []
    for hid, name, age, nsr_pin in query("SELECT id,name,flock_age,nsr_pin FROM hangars WHERE user_id=?", (uid,)):
        t, vals = get_thresh(hid), []
        for (nid,) in query("SELECT node_id FROM nodes WHERE hangar_id=? AND active=1 AND node_type='ns_edge'", (hid,)):
            row = query("SELECT temperature,humidity,ammonia,fan,heater,mister,ventilation FROM readings WHERE node_id=? AND hangar_id=? ORDER BY id DESC LIMIT 1", (nid, hid), one=True)
            if row: vals.append(row)
        avg = None
        if vals:
            at = round(sum(v[0] for v in vals)/len(vals),1)
            ah = round(sum(v[1] for v in vals)/len(vals),1)
            an = round(sum(v[2] for v in vals)/len(vals),1)
            prev = {"fan":vals[-1][3],"heater":vals[-1][4],"mister":vals[-1][5],"ventilation":vals[-1][6]}
            fan,heater,mister,ventil = decide(at,ah,an,t,prev,hid)
            al, am = check_alert(at,ah,an,t)
            avg = {"temperature":at,"humidity":ah,"ammonia":an,"fan":fan,"heater":heater,
                   "mister":mister,"ventilation":ventil,"alert_level":al,"alert":am}
        alert_count = query("SELECT COUNT(*) FROM alerts WHERE hangar_id=? AND level='critical' AND status='Non traité'", (hid,), one=True)[0]
        hangars.append({"id":hid,"name":name,"flock_age":age,"nsr_pin":nsr_pin or master_pin,
                        "thresholds":t,"avg":avg,"alert_count":alert_count})
    return render_template("dashboard.html", hangars=hangars, username=u[0],
                           viewing_as=u[0], viewing_id=uid)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)