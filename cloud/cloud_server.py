# ============================================================
# CLOUD SERVER — Nexa Sens
# Runs on Render
#
# RESPONSIBILITIES:
#   - Store data received from Pi
#   - Serve dashboard to farmer/admin
#   - Handle farmer overrides (push to Pi immediately)
#   - Monitor Pi heartbeat (alert if Pi goes down)
#   - NO equipment decisions (Pi handles all of that)
#   - NO alert escalation (Pi handles all of that — Push 4)
#
# BUGS FIXED:
#   B1. Cloud no longer re-evaluates alert_level from sensor values
#   B2. get_alert_level() removed from ALL display paths
#   B3. init_db() called only once at startup, never in watchdog loop
#   B4. Ownership check added to remove_node, add_node, set_threshold,
#       update_age, control, history, alerts
#   B5. f-string SQL uses validated column mapping dict (not raw input)
#   B6. Equipment display uses hangars table override state, not
#       re-derived from a potentially stale reading
#   B7. add_hangar() sets nsr_pin correctly
#   B8. NSR pin assignment per hangar with 3-hangar limit
#   B9. notify deduplication by condition type not exact message
#   B10. sms_override updates only correct hangar
#   B11. push_config_to_pi logs warning if Pi URL unknown
#   B12. hangar_page() now resolves AUTO equipment to Pi's last decision
#
# PUSH 4:
#   - /receive accepts new level "attention" (yellow)
#   - Cloud is fully passive — Pi handles all alert lifecycle (escalation,
#     stability, SMS, voice calls). Cloud just records what Pi says.
# ============================================================

from flask import Flask, request, jsonify, render_template, redirect, session
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, random, string, threading, time, os, requests as http

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "nexasens_dev_secret")
API_KEY        = os.environ.get("API_KEY",    "NEXASENS_SECRET_KEY")

# ── TWILIO (cloud's own SMS path — only used if explicitly enabled) ──
TWILIO_ENABLED = os.environ.get("TWILIO_ENABLED", "false").lower() == "true"
TWILIO_SID     = os.environ.get("TWILIO_SID",   "")
TWILIO_TOKEN   = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM    = os.environ.get("TWILIO_FROM",  "")

def send_twilio_sms(to, msg):
    if not TWILIO_ENABLED:
        print(f"[TWILIO] {to}: {msg}")
        return
    try:
        from twilio.rest import Client
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            to=to, from_=TWILIO_FROM, body=msg)
    except Exception as e:
        print(f"[TWILIO ERROR] {e}")

# ── DATABASE ──────────────────────────────────────────────────
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

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        phone    TEXT,
        role     TEXT    DEFAULT 'client',
        active   INTEGER DEFAULT 1)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pins (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        pin      TEXT UNIQUE,
        pin_type TEXT,
        used     INTEGER DEFAULT 0,
        used_by  INTEGER DEFAULT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hangars (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER,
        name         TEXT,
        flock_age    INTEGER DEFAULT 1,
        temp_min     REAL    DEFAULT 32,
        temp_max     REAL    DEFAULT 35,
        hum_min      REAL    DEFAULT 60,
        hum_max      REAL    DEFAULT 70,
        ammonia_max  REAL    DEFAULT 10,
        nsr_pin      TEXT    DEFAULT NULL,
        eq_fan       TEXT    DEFAULT 'AUTO',
        eq_heater    TEXT    DEFAULT 'AUTO',
        eq_mister    TEXT    DEFAULT 'AUTO',
        eq_ventilation TEXT  DEFAULT 'AUTO')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id   INTEGER,
        hangar_id INTEGER,
        node_id   TEXT,
        pin       TEXT,
        node_type TEXT    DEFAULT 'ns_edge',
        active    INTEGER DEFAULT 1,
        last_seen TEXT    DEFAULT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        hangar_id   INTEGER,
        node_id     TEXT,
        temperature REAL,
        humidity    REAL,
        ammonia     REAL,
        fan         TEXT,
        heater      TEXT,
        mister      TEXT,
        ventilation TEXT,
        alert_level TEXT DEFAULT 'normal',
        alert       TEXT,
        edge_alert_level TEXT DEFAULT 'normal',
        edge_status TEXT DEFAULT 'online',
        timestamp   TEXT)""")
    # Push 6: add columns if they don't exist (for existing DBs)
    for col, decl in [
        ("edge_alert_level", "TEXT DEFAULT 'normal'"),
        ("edge_status",      "TEXT DEFAULT 'online'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # already exists
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        hangar_id  INTEGER,
        node_id    TEXT,
        message    TEXT,
        alert_type TEXT    DEFAULT 'general',
        level      TEXT    DEFAULT 'normal',
        status     TEXT    DEFAULT 'Non traité',
        timestamp  TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS nsr_heartbeat (
        pin      TEXT PRIMARY KEY,
        last_seen TEXT,
        pi_url   TEXT DEFAULT NULL)""")
    conn.commit()

    conn.execute(
        "INSERT OR IGNORE INTO users VALUES (NULL,'admin',?,'','admin',1)",
        (generate_password_hash("admin1234"),)
    )
    for pin, pt in [
        ("NSR1", "nsr_box"), ("NSR2", "nsr_box"), ("NSR3", "nsr_box"),
        ("ED01", "ns_edge"), ("ED02", "ns_edge"), ("ED03", "ns_edge"),
        ("ED04", "ns_edge"), ("ED05", "ns_edge"), ("ED06", "ns_edge"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO pins VALUES (NULL,?,?,0,NULL)", (pin, pt))
    conn.commit()

init_db()

THRESHOLDS = {
    1: {"temp_min": 32, "temp_max": 35, "hum_min": 60, "hum_max": 70, "ammonia_max": 10},
    2: {"temp_min": 29, "temp_max": 32, "hum_min": 60, "hum_max": 70, "ammonia_max": 10},
    3: {"temp_min": 26, "temp_max": 29, "hum_min": 55, "hum_max": 65, "ammonia_max": 15},
    4: {"temp_min": 23, "temp_max": 26, "hum_min": 55, "hum_max": 65, "ammonia_max": 20},
    5: {"temp_min": 18, "temp_max": 23, "hum_min": 50, "hum_max": 60, "ammonia_max": 25},
}

def get_thresh(hangar_id):
    row = query(
        "SELECT temp_min,temp_max,hum_min,hum_max,ammonia_max FROM hangars WHERE id=?",
        (hangar_id,), one=True)
    if row:
        return {"temp_min": row[0], "temp_max": row[1],
                "hum_min": row[2], "hum_max": row[3], "ammonia_max": row[4]}
    return THRESHOLDS[1]

def get_phone(hangar_id):
    row = query(
        "SELECT u.phone FROM users u JOIN hangars h ON h.user_id=u.id WHERE h.id=?",
        (hangar_id,), one=True)
    return row[0] if row else None

def owns_hangar(hid):
    if session.get("role") == "admin":
        return True
    uid = session.get("user_id")
    if not uid:
        return False
    return bool(query(
        "SELECT id FROM hangars WHERE id=? AND user_id=?",
        (hid, uid), one=True))

THRESHOLD_COLS = {
    "temp_min": "temp_min", "temp_max": "temp_max",
    "hum_min": "hum_min",   "hum_max": "hum_max",
    "ammonia_max": "ammonia_max"
}
EQUIPMENT_COLS = {
    "eq_fan": "eq_fan", "eq_heater": "eq_heater",
    "eq_mister": "eq_mister", "eq_ventilation": "eq_ventilation"
}

def push_config_to_pi(nsr_pin):
    row = query("SELECT pi_url FROM nsr_heartbeat WHERE pin=?", (nsr_pin,), one=True)
    if not row or not row[0]:
        print(f"[PUSH] WARNING — Pi URL unknown for {nsr_pin}.")
        return
    pi_url = row[0]
    try:
        config = build_nsr_config(nsr_pin)
        http.post(f"{pi_url}/update_config",
                  json=config,
                  headers={"X-API-KEY": API_KEY},
                  timeout=5)
        print(f"[PUSH] Config pushed to Pi {nsr_pin} at {pi_url}")
    except Exception as e:
        print(f"[PUSH] Failed to push to Pi {nsr_pin}: {e}")

def build_nsr_config(nsr_pin):
    rows = query(
        """SELECT id, temp_min, temp_max, hum_min, hum_max, ammonia_max,
                  eq_fan, eq_heater, eq_mister, eq_ventilation
           FROM hangars WHERE nsr_pin=?""",
        (nsr_pin,))
    hangars = {}
    for r in rows:
        hid  = r[0]
        pins = [p[0] for p in query(
            "SELECT pin FROM nodes WHERE hangar_id=? AND active=1 AND node_type='ns_edge'",
            (hid,))]
        hangars[str(hid)] = {
            "thresholds": {
                "temp_min": r[1], "temp_max": r[2],
                "hum_min":  r[3], "hum_max":  r[4], "ammonia_max": r[5]
            },
            "overrides": {
                "eq_fan": r[6], "eq_heater": r[7],
                "eq_mister": r[8], "eq_ventilation": r[9]
            },
            "pins": pins
        }
    return {"hangars": hangars}

def pi_watchdog():
    while True:
        time.sleep(60)
        try:
            cutoff = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows   = query("SELECT pin, last_seen FROM nsr_heartbeat")
            for nsr_pin, last_seen in rows:
                if last_seen and last_seen < cutoff:
                    existing = query(
                        """SELECT id FROM alerts
                           WHERE node_id=? AND alert_type='pi_offline'
                             AND status='Non traité'""",
                        (nsr_pin,), one=True)
                    if not existing:
                        msg = (f"NSR-BOX hors ligne: {nsr_pin} "
                               f"— aucune donnée depuis 5 minutes")
                        for (hid,) in query(
                                "SELECT id FROM hangars WHERE nsr_pin=?", (nsr_pin,)):
                            execute(
                                """INSERT INTO alerts
                                   (hangar_id, node_id, message, alert_type,
                                    level, status, timestamp)
                                   VALUES (?,?,?,'pi_offline','critique','Non traité',?)""",
                                (hid, nsr_pin, msg, ts))
                        for (hid,) in query(
                                "SELECT id FROM hangars WHERE nsr_pin=?", (nsr_pin,)):
                            phone = get_phone(hid)
                            if phone:
                                send_twilio_sms(phone, f"🚨 Nexa Sens — {msg}")
                                break
                else:
                    for (hid,) in query(
                            "SELECT id FROM hangars WHERE nsr_pin=?", (nsr_pin,)):
                        execute(
                            """UPDATE alerts SET status='Traité'
                               WHERE hangar_id=? AND node_id=?
                                 AND alert_type='pi_offline'
                                 AND status='Non traité'""",
                            (hid, nsr_pin))
        except Exception as e:
            print(f"[PI WATCHDOG] {e}")

threading.Thread(target=pi_watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u   = request.form["username"]
        p   = request.form["password"]
        row = query(
            "SELECT id, password, role, active FROM users WHERE username=?",
            (u,), one=True)
        if row and check_password_hash(row[1], p):
            if not row[3]:
                return render_template("login.html", error="Compte désactivé.")
            session.update({"user_id": row[0], "username": u, "role": row[2]})
            return redirect("/admin" if row[2] == "admin" else "/")
        return render_template("login.html", error="Identifiants incorrects")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        phone    = request.form["phone"]
        pin      = request.form["pin"].upper().strip()
        pin_row  = query(
            "SELECT id, used FROM pins WHERE pin=? AND pin_type='nsr_box'",
            (pin,), one=True)
        if not pin_row:
            return render_template("register.html", error="PIN NSR-BOX invalide.")
        if pin_row[1]:
            return render_template("register.html", error="PIN déjà utilisé.")
        try:
            cur = execute(
                "INSERT INTO users VALUES (NULL,?,?,?,'client',1)",
                (username, generate_password_hash(password), phone))
            uid = cur.lastrowid
            execute("UPDATE pins SET used=1, used_by=? WHERE id=?",
                    (uid, pin_row[0]))
            t    = THRESHOLDS[1]
            cur2 = execute(
                """INSERT INTO hangars
                   (user_id, name, flock_age, temp_min, temp_max,
                    hum_min, hum_max, ammonia_max, nsr_pin)
                   VALUES (?,?,1,?,?,?,?,?,?)""",
                (uid, "Hangar 1", t["temp_min"], t["temp_max"],
                 t["hum_min"], t["hum_max"], t["ammonia_max"], pin))
            execute(
                """INSERT INTO nodes (user_id, hangar_id, node_id, pin, node_type)
                   VALUES (?,?,?,?,'nsr_box')""",
                (uid, cur2.lastrowid, "NSR-BOX", pin))
            return redirect("/login")
        except sqlite3.IntegrityError:
            return render_template("register.html",
                                   error="Nom d'utilisateur déjà pris.")
    return render_template("register.html")

# ═══════════════════════════════════════════════════════════════
# DASHBOARD + HANGAR PAGE
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")
    if session["role"] == "admin":
        return redirect("/admin")
    uid        = session["user_id"]
    master_row = query(
        "SELECT nsr_pin FROM hangars WHERE user_id=? AND nsr_pin IS NOT NULL LIMIT 1",
        (uid,), one=True)
    master_pin = master_row[0] if master_row else None
    hangars    = _build_hangar_summaries(uid, master_pin)
    return render_template("dashboard.html",
                           hangars=hangars,
                           username=session["username"])

def _build_hangar_summaries(uid, master_pin):
    hangars = []
    for hid, name, age, nsr_pin in query(
            "SELECT id, name, flock_age, nsr_pin FROM hangars WHERE user_id=?",
            (uid,)):
        t    = get_thresh(hid)
        cutoff = (datetime.now() - timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
        vals = []
        for nid, last_seen in query(
                """SELECT node_id, last_seen FROM nodes
                   WHERE hangar_id=? AND active=1 AND node_type='ns_edge'""",
                (hid,)):
            if not last_seen or last_seen < cutoff:
                continue
            row = query(
                """SELECT temperature, humidity, ammonia,
                          fan, heater, mister, ventilation, alert_level
                   FROM readings
                   WHERE node_id=? AND hangar_id=? AND temperature IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (nid, hid), one=True)
            if row:
                vals.append(row)
        avg = None
        if vals:
            at = round(sum(v[0] for v in vals) / len(vals), 1)
            ah = round(sum(v[1] for v in vals) / len(vals), 1)
            an = round(sum(v[2] for v in vals) / len(vals), 1)
            # Push 6: alert_level from open alerts table (source of truth)
            open_alerts = query(
                """SELECT level FROM alerts
                   WHERE hangar_id=? AND status IN ('En traitement','Non traité')
                     AND level IN ('attention','critique')""",
                (hid,))
            if open_alerts:
                level_order = {"critique": 3, "attention": 2, "normal": 0}
                al = max((r[0] for r in open_alerts),
                         key=lambda x: level_order.get(x, 0))
            else:
                al = "normal"
            eq_row = query(
                "SELECT eq_fan, eq_heater, eq_mister, eq_ventilation FROM hangars WHERE id=?",
                (hid,), one=True)
            last_reading = query(
                """SELECT fan, heater, mister, ventilation FROM readings
                   WHERE hangar_id=? ORDER BY id DESC LIMIT 1""", (hid,), one=True)
            def resolve_eq(override, actual):
                if override in ("ON", "OFF"):
                    return override
                return actual if actual else "OFF"
            avg = {
                "temperature": at, "humidity": ah, "ammonia": an,
                "fan":         resolve_eq(eq_row[0] if eq_row else "AUTO",
                                          last_reading[0] if last_reading else "OFF"),
                "heater":      resolve_eq(eq_row[1] if eq_row else "AUTO",
                                          last_reading[1] if last_reading else "OFF"),
                "mister":      resolve_eq(eq_row[2] if eq_row else "AUTO",
                                          last_reading[2] if last_reading else "OFF"),
                "ventilation": resolve_eq(eq_row[3] if eq_row else "AUTO",
                                          last_reading[3] if last_reading else "OFF"),
                "alert_level": al
            }
        alert_count = query(
            """SELECT COUNT(*) FROM alerts
               WHERE hangar_id=? AND level='critique' AND status='Non traité'""",
            (hid,), one=True)[0]
        hangars.append({
            "id": hid, "name": name, "flock_age": age,
            "nsr_pin": nsr_pin or master_pin,
            "thresholds": t, "avg": avg, "alert_count": alert_count
        })
    return hangars

@app.route("/hangar/<int:hid>")
def hangar_page(hid):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect("/")

    h = query(
        """SELECT name, flock_age, temp_min, temp_max, hum_min, hum_max,
                  ammonia_max, nsr_pin, eq_fan, eq_heater, eq_mister, eq_ventilation
           FROM hangars WHERE id=?""",
        (hid,), one=True)
    if not h:
        return "Hangar introuvable", 404

    t      = get_thresh(hid)
    hangar = {
        "id": hid, "name": h[0], "flock_age": h[1],
        "temp_min": h[2], "temp_max": h[3], "hum_min": h[4],
        "hum_max": h[5], "ammonia_max": h[6], "nsr_pin": h[7],
        "eq_fan": h[8], "eq_heater": h[9], "eq_mister": h[10],
        "eq_ventilation": h[11], "eff": t
    }

    cutoff = (datetime.now() - timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
    nodes  = {}
    for node_id, last_seen, pin in query(
            """SELECT node_id, last_seen, pin FROM nodes
               WHERE hangar_id=? AND active=1 AND node_type='ns_edge'""",
            (hid,)):
        offline = not last_seen or last_seen < cutoff
        row     = query(
            """SELECT temperature, humidity, ammonia, timestamp, alert_level
               FROM readings
               WHERE node_id=? AND hangar_id=? AND temperature IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (node_id, hid), one=True)
        al = "normal"
        if row and not offline:
            al = row[4]
        nodes[node_id] = {
            "pin":         pin,
            "temperature": row[0] if row else None,
            "humidity":    row[1] if row else None,
            "ammonia":     row[2] if row else None,
            "timestamp":   row[3] if row else None,
            "alert_level": "critique" if offline else al,
            "offline":     offline
        }

    active = [n for n in nodes.values()
              if not n["offline"] and n["temperature"] is not None]
    avg    = None
    if active:
        at = round(sum(n["temperature"] for n in active) / len(active), 1)
        ah = round(sum(n["humidity"]    for n in active) / len(active), 1)
        an = round(sum(n["ammonia"]     for n in active) / len(active), 1)

        # Push 6: NSR bar alert_level comes from the alerts TABLE (source of truth),
        # not from individual reading rows which may be stale.
        open_alerts = query(
            """SELECT level FROM alerts
               WHERE hangar_id=? AND status IN ('En traitement','Non traité')
                 AND level IN ('attention','critique')""",
            (hid,))
        if open_alerts:
            level_order = {"critique": 3, "attention": 2, "normal": 0}
            al = max((r[0] for r in open_alerts),
                     key=lambda x: level_order.get(x, 0))
        else:
            al = "normal"

        last_reading = query(
            """SELECT fan, heater, mister, ventilation FROM readings
               WHERE hangar_id=? ORDER BY id DESC LIMIT 1""", (hid,), one=True)

        def resolve_eq(override, actual):
            if override in ("ON", "OFF"):
                return override
            return actual if actual else "OFF"

        avg = {
            "temperature": at, "humidity": ah, "ammonia": an,
            "fan":         resolve_eq(hangar["eq_fan"],
                                      last_reading[0] if last_reading else "OFF"),
            "heater":      resolve_eq(hangar["eq_heater"],
                                      last_reading[1] if last_reading else "OFF"),
            "mister":      resolve_eq(hangar["eq_mister"],
                                      last_reading[2] if last_reading else "OFF"),
            "ventilation": resolve_eq(hangar["eq_ventilation"],
                                      last_reading[3] if last_reading else "OFF"),
            "alert_level": al
        }

    active_alerts = query(
        """SELECT id, node_id, message, level, status, timestamp
           FROM alerts
           WHERE hangar_id=? AND status!='Traité' AND level!='normal'
           ORDER BY id DESC LIMIT 5""",
        (hid,))
    owner = query("SELECT user_id FROM hangars WHERE id=?", (hid,), one=True)
    return render_template(
        "hangar.html",
        hangar=hangar, nodes=nodes, avg=avg,
        active_alerts=active_alerts,
        username=session["username"],
        error=request.args.get("error", ""),
        is_admin=session["role"] == "admin",
        owner_id=owner[0] if owner else None,
        thresholds=t)

# ═══════════════════════════════════════════════════════════════
# HANGAR ACTIONS
# ═══════════════════════════════════════════════════════════════

@app.route("/set_threshold/<int:hid>", methods=["POST"])
def set_threshold(hid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not owns_hangar(hid):
        return jsonify({"error": "Accès refusé"}), 403
    data  = request.get_json()
    field = data.get("field")
    col = THRESHOLD_COLS.get(field)
    if not col:
        return jsonify({"error": "champ invalide"}), 400
    try:
        execute(f"UPDATE hangars SET {col}=? WHERE id=?",
                (float(data.get("value")), hid))
        nsr = query("SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
        if nsr and nsr[0]:
            push_config_to_pi(nsr[0])
        return jsonify({"ok": True, "thresholds": get_thresh(hid)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/remove_node/<int:hid>/<node_id>", methods=["POST"])
def remove_node(hid, node_id):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect(f"/hangar/{hid}")
    row = query(
        "SELECT pin FROM nodes WHERE hangar_id=? AND node_id=? AND node_type='ns_edge'",
        (hid, node_id), one=True)
    if row:
        execute("UPDATE pins SET used=0, used_by=NULL WHERE pin=?", (row[0],))
    execute(
        "DELETE FROM nodes WHERE hangar_id=? AND node_id=? AND node_type='ns_edge'",
        (hid, node_id))
    nsr = query("SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
    if nsr and nsr[0]:
        push_config_to_pi(nsr[0])
    return redirect(f"/hangar/{hid}")

@app.route("/add_hangar", methods=["POST"])
def add_hangar():
    if "user_id" not in session:
        return redirect("/login")
    uid   = session["user_id"]
    count = query(
        "SELECT COUNT(*) FROM hangars WHERE user_id=?", (uid,), one=True)[0]
    if count >= 3:
        return redirect("/")
    t = THRESHOLDS[1]
    existing_names = [r[0] for r in query(
        "SELECT name FROM hangars WHERE user_id=?", (uid,))]
    candidate = count + 1
    while f"Hangar {candidate}" in existing_names:
        candidate += 1
    execute(
        """INSERT INTO hangars
           (user_id, name, flock_age, temp_min, temp_max,
            hum_min, hum_max, ammonia_max, nsr_pin)
           VALUES (?,?,1,?,?,?,?,?,NULL)""",
        (uid, f"Hangar {candidate}", t["temp_min"], t["temp_max"],
         t["hum_min"], t["hum_max"], t["ammonia_max"]))
    return redirect("/")

@app.route("/assign_nsr/<int:hid>", methods=["POST"])
def assign_nsr(hid):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect("/")
    nsr_pin = request.form.get("nsr_pin", "").upper().strip()
    pin_row = query(
        "SELECT id, used FROM pins WHERE pin=? AND pin_type='nsr_box'",
        (nsr_pin,), one=True)
    if not pin_row:
        return redirect(f"/hangar/{hid}?error=PIN+NSR-BOX+invalide")

    nsr_hangar_count = query(
        "SELECT COUNT(*) FROM hangars WHERE nsr_pin=?", (nsr_pin,), one=True)[0]
    current_nsr      = query(
        "SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
    already_assigned = current_nsr and current_nsr[0] == nsr_pin

    if not already_assigned and nsr_hangar_count >= 3:
        return redirect(
            f"/hangar/{hid}?error=Ce+NSR+contrôle+déjà+3+hangars+(maximum)")

    execute("UPDATE hangars SET nsr_pin=? WHERE id=?", (nsr_pin, hid))
    if not pin_row[1]:
        execute("UPDATE pins SET used=1, used_by=? WHERE id=?",
                (session["user_id"], pin_row[0]))
    push_config_to_pi(nsr_pin)
    return redirect(f"/hangar/{hid}")

@app.route("/add_node/<int:hid>", methods=["POST"])
def add_node(hid):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect("/")
    pin     = request.form.get("pin", "").upper().strip()
    pin_row = query(
        "SELECT id, used FROM pins WHERE pin=? AND pin_type='ns_edge'",
        (pin,), one=True)
    if not pin_row:
        return redirect(f"/hangar/{hid}?error=PIN+invalide")
    if pin_row[1]:
        return redirect(f"/hangar/{hid}?error=PIN+déjà+utilisé")
    count = query(
        """SELECT COUNT(*) FROM nodes
           WHERE hangar_id=? AND active=1 AND node_type='ns_edge'""",
        (hid,), one=True)[0]
    if count >= 10:
        return redirect(f"/hangar/{hid}?error=Maximum+10+capteurs")
    execute(
        """INSERT INTO nodes (user_id, hangar_id, node_id, pin, node_type)
           VALUES (?,?,?,?,'ns_edge')""",
        (session["user_id"], hid, f"NS-EDGE-{count+1}", pin))
    execute("UPDATE pins SET used=1, used_by=? WHERE id=?",
            (session["user_id"], pin_row[0]))
    nsr = query("SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
    if nsr and nsr[0]:
        push_config_to_pi(nsr[0])
    return redirect(f"/hangar/{hid}")

@app.route("/update_age/<int:hid>", methods=["POST"])
def update_age(hid):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect("/")
    age = max(1, min(int(request.form.get("flock_age", 1)), 10))
    t   = THRESHOLDS[min(age, 5)]
    execute(
        """UPDATE hangars
           SET flock_age=?, temp_min=?, temp_max=?,
               hum_min=?, hum_max=?, ammonia_max=?
           WHERE id=?""",
        (age, t["temp_min"], t["temp_max"],
         t["hum_min"], t["hum_max"], t["ammonia_max"], hid))
    nsr = query("SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
    if nsr and nsr[0]:
        push_config_to_pi(nsr[0])
    return redirect(f"/hangar/{hid}")

@app.route("/control/<int:hid>/<equipment>/<action>", methods=["POST"])
def control(hid, equipment, action):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect(f"/hangar/{hid}")
    col = EQUIPMENT_COLS.get(equipment)
    if not col:
        return redirect(f"/hangar/{hid}")
    if action not in ("ON", "OFF", "AUTO"):
        return redirect(f"/hangar/{hid}")
    execute(f"UPDATE hangars SET {col}=? WHERE id=?", (action, hid))
    nsr = query("SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
    if nsr and nsr[0]:
        push_config_to_pi(nsr[0])
    return redirect(f"/hangar/{hid}")

# ═══════════════════════════════════════════════════════════════
# RECEIVE DATA — Push 4 lifecycle
#
# Pi decides everything. Cloud just records the decision:
#   - level == "attention" → open alert as level=en_traitement (yellow)
#   - level == "critique"      → upgrade existing or insert as critical (red)
#   - level == "normal"           → close all open alerts for this hangar/source
# ═══════════════════════════════════════════════════════════════

@app.route("/receive", methods=["POST"])
def receive():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    try:
        # Push 6: temperature/humidity/ammonia may be None for offline-marker rows
        t_raw = data.get("temperature")
        h_raw = data.get("humidity")
        a_raw = data.get("ammonia")
        temp     = float(t_raw) if t_raw is not None else None
        humidity = float(h_raw) if h_raw is not None else None
        ammonia  = float(a_raw) if a_raw is not None else None
    except (ValueError, TypeError):
        return jsonify({"error": "Données invalides"}), 400

    pin = data.get("pin", "").upper().strip()
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = query(
        "SELECT node_id, hangar_id FROM nodes WHERE pin=? AND active=1",
        (pin,), one=True)
    if not row:
        return jsonify({"error": f"PIN {pin} non enregistré"}), 403
    node_id, hid = row

    # Push 7: offline-marker payloads carry edge_status='offline'. They must NOT
    # refresh nodes.last_seen — otherwise the dashboard's "is this edge fresh?"
    # check sees it as just-online and the NSR bar briefly re-renders red,
    # which is confusing. last_seen represents "last real reading".
    if data.get("edge_status") != "offline":
        execute("UPDATE nodes SET last_seen=? WHERE pin=?", (ts, pin))

    fan    = data.get("fan")    or "OFF"
    heater = data.get("heater") or "OFF"
    mister = data.get("mister") or "OFF"
    ventil = data.get("ventilation") or "OFF"
    level  = data.get("alert_level", "normal")
    msg    = data.get("alert",       "Normal")
    edge_lvl = data.get("edge_alert_level", "normal")
    edge_st  = data.get("edge_status",      "online")

    # Push 6: control payloads (e.g. reconnect close-alert signal) don't go in readings
    if not data.get("_skip_history"):
        execute(
            """INSERT INTO readings
               (hangar_id, node_id, temperature, humidity, ammonia,
                fan, heater, mister, ventilation, alert_level, alert,
                edge_alert_level, edge_status, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hid, node_id, temp, humidity, ammonia,
             fan, heater, mister, ventil, level, msg,
             edge_lvl, edge_st, ts))

    # Push 4 v2: trust the Pi's condition_type (authoritative source).
    # Falls back to derived value for old payloads without the field.
    pi_cond = data.get("condition_type")
    if pi_cond and pi_cond != "normal":
        alert_type = pi_cond
    elif pi_cond == "normal":
        alert_type = "normal"
    else:
        # Legacy fallback for older Pi versions
        alert_type = _derive_alert_type(temp, humidity, ammonia, get_thresh(hid), level)

    nsr_row = query("SELECT nsr_pin FROM hangars WHERE id=?", (hid,), one=True)
    alert_source = nsr_row[0] if nsr_row and nsr_row[0] else node_id
    msg_full = msg  # Push 1: no [capteur ED01] suffix

    # ── Push 5: Pi controls BOTH alert_level AND status ─────────────────
    # The cloud just writes what the Pi tells it. No autonomous decisions.

    pi_status = data.get("alert_status")  # None | 'En traitement' | 'Non traité' | 'Traité'

    if level in ("attention", "notify"):
        # Find any open alert for this hangar+condition (regardless of current status)
        existing = query(
            """SELECT id FROM alerts
               WHERE hangar_id=? AND alert_type=?
                 AND level IN ('attention',)
                 AND status IN ('En traitement','Non traité')""",
            (hid, alert_type), one=True)
        if not existing:
            execute(
                """INSERT INTO alerts
                   (hangar_id, node_id, message, alert_type, level, status, timestamp)
                   VALUES (?,?,?,?,'attention',?,?)""",
                (hid, alert_source, msg_full, alert_type, pi_status or "En traitement", ts))
        else:
            # Just refresh the message + status the Pi sent
            if pi_status:
                execute(
                    """UPDATE alerts SET message=?, status=? WHERE id=?""",
                    (msg_full, pi_status, existing[0]))
            else:
                execute(
                    """UPDATE alerts SET message=? WHERE id=?""",
                    (msg_full, existing[0]))

    elif level == "critique":
        # Promote any open en_traitement/notify for this condition to critical.
        # Use Pi's status (typically 'Non traité' on escalation).
        new_status = pi_status or "Non traité"
        execute(
            """UPDATE alerts
                  SET level='critique', status=?, message=?
                WHERE hangar_id=? AND alert_type=?
                  AND level IN ('attention',)
                  AND status IN ('En traitement','Non traité')""",
            (new_status, msg_full, hid, alert_type))
        if not query(
                """SELECT id FROM alerts
                   WHERE hangar_id=? AND alert_type=?
                     AND level='critique' AND status IN ('En traitement','Non traité')""",
                (hid, alert_type), one=True):
            execute(
                """INSERT INTO alerts
                   (hangar_id, node_id, message, alert_type, level, status, timestamp)
                   VALUES (?,?,?,?,'critique',?,?)""",
                (hid, alert_source, msg_full, alert_type, new_status, ts))

    elif level == "normal":
        # Cloud does NOT auto-close. The Pi sends a separate alert_status='Traité'
        # in the SAME payload when the stability window finishes.
        pass

    # Push 6: handle Pi's explicit 'Traité' status.
    # - If condition_type is given (e.g. 'offline_ED01' or 'temp_high'), close only
    #   alerts matching that exact alert_type.
    # - Otherwise (legacy hangar-wide resolution): close all condition-based alerts
    #   for this hangar but DO NOT close 'offline_*' alerts (those are managed
    #   per-edge by the watchdog and have their own close signal).
    if pi_status == "Traité":
        if pi_cond and pi_cond != "normal":
            execute(
                """UPDATE alerts SET status='Traité'
                   WHERE hangar_id=? AND alert_type=?
                     AND status IN ('En traitement','Non traité')
                     AND level IN ('attention','critique')""",
                (hid, pi_cond))
        else:
            execute(
                """UPDATE alerts SET status='Traité'
                   WHERE hangar_id=?
                     AND status IN ('En traitement','Non traité')
                     AND level IN ('attention','critique')
                     AND alert_type NOT LIKE 'offline_%'""",
                (hid,))

    return jsonify({"ok": True, "node_id": node_id, "hangar_id": hid}), 200

def _derive_alert_type(temp, hum, nh3, t, level):
    if level == "normal":               return "normal"
    if nh3  >= t["ammonia_max"] - 2: return "ammonia"
    if temp >= t["temp_max"]:        return "temp_high"
    if temp <= t["temp_min"]:        return "temp_low"
    if hum  >= t["hum_max"] - 2:     return "hum_high"
    if hum  <= t["hum_min"]:         return "hum_low"
    return "general"

# ═══════════════════════════════════════════════════════════════
# PI HEARTBEAT + CONFIG + SMS OVERRIDE
# ═══════════════════════════════════════════════════════════════

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    data   = request.get_json()
    pin    = data.get("pin", "").upper()
    pi_url = data.get("url", "")
    ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute(
        "INSERT OR REPLACE INTO nsr_heartbeat (pin, last_seen, pi_url) VALUES (?,?,?)",
        (pin, ts, pi_url))
    return jsonify({"ok": True}), 200

@app.route("/nsr_config/<nsr_pin>")
def nsr_config(nsr_pin):
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(build_nsr_config(nsr_pin)), 200

@app.route("/sms_override", methods=["POST"])
def sms_override():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    data      = request.get_json()
    hangar_id = data.get("hangar_id")
    equipment = data.get("equipment")
    action    = data.get("action")
    col = EQUIPMENT_COLS.get(equipment)
    if not col:
        return jsonify({"error": "invalid equipment"}), 400
    if action not in ("ON", "OFF", "AUTO"):
        return jsonify({"error": "invalid action"}), 400
    if hangar_id:
        execute(f"UPDATE hangars SET {col}=? WHERE id=?", (action, hangar_id))
    else:
        nsr_pin = data.get("nsr_pin", "").upper()
        execute(f"UPDATE hangars SET {col}=? WHERE nsr_pin=?", (action, nsr_pin))
    return jsonify({"ok": True}), 200

# ═══════════════════════════════════════════════════════════════
# HISTORY & ALERTS
# ═══════════════════════════════════════════════════════════════

@app.route("/history/<int:hid>")
def history(hid):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect("/")
    date      = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    node      = request.args.get("node", "")
    node_rows = query(
        """SELECT node_id, pin FROM nodes
           WHERE hangar_id=? AND active=1 AND node_type='ns_edge'""",
        (hid,))
    node_ids  = [r[0] for r in node_rows]
    node_pins = {r[0]: r[1] for r in node_rows}
    if not node and node_ids:
        node = node_ids[0]
    readings = []
    if node:
        readings = [
            {"temperature": r[0], "humidity": r[1], "ammonia": r[2],
             "fan": r[3], "heater": r[4], "mister": r[5], "ventilation": r[6],
             "alert_level": r[7], "alert": r[8], "timestamp": r[9],
             "edge_alert_level": r[10] or "normal", "edge_status": r[11] or "online"}
            for r in query(
                """SELECT temperature, humidity, ammonia,
                          fan, heater, mister, ventilation,
                          alert_level, alert, timestamp,
                          edge_alert_level, edge_status
                   FROM readings
                   WHERE hangar_id=? AND node_id=? AND DATE(timestamp)=?
                   ORDER BY timestamp ASC""",
                (hid, node, date))
        ]
    row = query("SELECT name, user_id FROM hangars WHERE id=?", (hid,), one=True)
    return render_template(
        "history.html",
        readings=readings, selected_date=date,
        selected_node=node, node_ids=node_ids, node_pins=node_pins,
        hangar_id=hid, hangar_name=row[0] if row else "",
        is_admin=session["role"] == "admin",
        owner_id=row[1] if row else None)

@app.route("/alerts/<int:hid>")
def alerts_page(hid):
    if "user_id" not in session:
        return redirect("/login")
    if not owns_hangar(hid):
        return redirect("/")
    alerts = [
        {"id": r[0], "node_id": r[1], "message": r[2],
         "level": r[3], "status": r[4], "timestamp": r[5]}
        for r in query(
            """SELECT id, node_id, message, level, status, timestamp
               FROM alerts WHERE hangar_id=? ORDER BY id DESC LIMIT 200""",
            (hid,))
    ]
    row = query("SELECT name, user_id FROM hangars WHERE id=?", (hid,), one=True)
    return render_template(
        "alerts.html",
        alerts=alerts, hangar_id=hid,
        hangar_name=row[0] if row else "",
        is_admin=session["role"] == "admin",
        owner_id=row[1] if row else None)

@app.route("/update_alert/<int:aid>", methods=["POST"])
def update_alert(aid):
    if "user_id" not in session:
        return redirect("/login")
    hid    = request.form.get("hangar_id")
    status = request.form.get("status")
    if status not in ("Non traité", "En traitement", "Traité"):
        return redirect(f"/alerts/{hid}")
    if hid and not owns_hangar(int(hid)):
        return redirect("/")
    execute("UPDATE alerts SET status=? WHERE id=?", (status, aid))
    return redirect(f"/alerts/{hid}")

# ═══════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════

@app.route("/admin")
def admin():
    if session.get("role") != "admin":
        return redirect("/login")
    clients = [{"id": r[0], "username": r[1], "phone": r[2], "active": r[3]}
               for r in query(
                   "SELECT id, username, phone, active FROM users WHERE role='client'")]
    pins    = [{"pin": r[0], "type": r[1], "used": r[2]}
               for r in query(
                   "SELECT pin, pin_type, used FROM pins ORDER BY id DESC")]
    return render_template("admin.html", clients=clients, pins=pins)

@app.route("/admin/generate_pin", methods=["POST"])
def generate_pin():
    if session.get("role") != "admin":
        return redirect("/login")
    pin_type = request.form.get("pin_type")
    for _ in range(int(request.form.get("count", 1))):
        while True:
            pin = "".join(random.choices(
                string.ascii_uppercase + string.digits, k=4))
            if not query("SELECT id FROM pins WHERE pin=?", (pin,), one=True):
                break
        execute("INSERT INTO pins VALUES (NULL,?,?,0,NULL)", (pin, pin_type))
    return redirect("/admin")

@app.route("/admin/toggle_client/<int:uid>", methods=["POST"])
def toggle_client(uid):
    if session.get("role") != "admin":
        return redirect("/login")
    cur = query("SELECT active FROM users WHERE id=?", (uid,), one=True)[0]
    execute("UPDATE users SET active=? WHERE id=?", (0 if cur else 1, uid))
    return redirect("/admin")

@app.route("/admin/free_pin/<pin>", methods=["POST"])
def free_pin(pin):
    if session.get("role") != "admin":
        return redirect("/login")
    pin = pin.upper().strip()
    execute("UPDATE pins SET used=0, used_by=NULL WHERE pin=?", (pin,))
    execute("UPDATE hangars SET nsr_pin=NULL WHERE nsr_pin=?", (pin,))
    execute("UPDATE nodes SET active=0 WHERE pin=?", (pin,))
    return redirect("/admin")

@app.route("/admin/view/<int:uid>")
def admin_view(uid):
    if session.get("role") != "admin":
        return redirect("/login")
    u = query("SELECT username FROM users WHERE id=?", (uid,), one=True)
    if not u:
        return "Introuvable", 404
    master_row = query(
        "SELECT nsr_pin FROM hangars WHERE user_id=? AND nsr_pin IS NOT NULL LIMIT 1",
        (uid,), one=True)
    master_pin = master_row[0] if master_row else None
    hangars    = _build_hangar_summaries(uid, master_pin)
    return render_template(
        "dashboard.html",
        hangars=hangars, username=u[0],
        viewing_as=u[0], viewing_id=uid)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)