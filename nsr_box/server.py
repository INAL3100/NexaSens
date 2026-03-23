from flask import Flask, render_template, request, redirect, url_for, session
from datetime import datetime
import random

app = Flask(__name__)
app.secret_key = "supersecretkey"

# Sample data
hangars = [
    {"id": 1, "name": "Hangar 1", "status": "ok", "edges": [{"name": "Edge 1", "status": "ok"}]},
    {"id": 2, "name": "Hangar 2", "status": "warning", "edges": [{"name": "Edge 2", "status": "warn"}]},
]

alerts = []

# Admin credentials
ADMIN_USER = "admin"
ADMIN_PASS = "1234"

@app.route("/")
def index():
    return render_template("hangars.html", hangars=hangars, alerts=alerts, admin=session.get("admin", False))

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")
    if username == ADMIN_USER and password == ADMIN_PASS:
        session["admin"] = True
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))

@app.route("/generate_pin")
def generate_pin():
    if not session.get("admin"):
        return "Unauthorized", 403
    pin = random.randint(1000, 9999)
    alerts.append({"message": f"NSR PIN generated: {pin}", "time": datetime.now().strftime("%H:%M")})
    return redirect(url_for("index"))

@app.route("/override/<int:hangar_id>")
def override(hangar_id):
    if not session.get("admin"):
        return "Unauthorized", 403
    for h in hangars:
        if h["id"] == hangar_id:
            h["status"] = "override"
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)