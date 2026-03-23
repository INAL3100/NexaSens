from flask import Flask, jsonify

app = Flask(__name__)

# Simulated cloud data
hangar_data = {
    "hangars": [
        {"id": 1, "name": "Hangar 1", "status": "ok"},
        {"id": 2, "name": "Hangar 2", "status": "warning"}
    ]
}

@app.route("/api/hangars")
def get_hangars():
    return jsonify(hangar_data)

@app.route("/api/alerts")
def get_alerts():
    return jsonify({"alerts": []})

if __name__ == "__main__":
    app.run(port=5001)