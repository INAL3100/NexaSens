from flask import Flask, render_template, request, jsonify
from datetime import datetime
import json

app = Flask(__name__)

# Simulated data storage
hangars = [
    {
        "id": 1,
        "name": "Hangar A",
        "status": "ok",
        "sensors": {"temp": 28, "humidity": 65, "co2": 400},
        "equipment": {"fan": True, "heater": False},
        "alerts": []
    },
    {
        "id": 2,
        "name": "Hangar B",
        "status": "warning",
        "sensors": {"temp": 32, "humidity": 70, "co2": 450},
        "equipment": {"fan": True, "heater": True},
        "alerts": ["High temp"]
    }
]

thresholds = {"temp": [20, 30], "humidity": [50, 70], "co2": [300, 500]}

def check_alerts(sensors):
    alerts = []
    for k, v in sensors.items():
        low, high = thresholds.get(k, [0, 999])
        if v < low or v > high:
            alerts.append(f"{k} out of range")
    return alerts

@app.route('/')
def index():
    for hangar in hangars:
        hangar['alerts'] = check_alerts(hangar['sensors'])
    return render_template('hangars.html', hangars=hangars)

@app.route('/update_equipment', methods=['POST'])
def update_equipment():
    data = request.json
    for hangar in hangars:
        if hangar['id'] == data['hangar_id']:
            hangar['equipment'][data['equipment']] = data['state']
    return jsonify({"success": True})

@app.route('/get_data')
def get_data():
    return jsonify(hangars)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)