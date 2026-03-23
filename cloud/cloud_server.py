from flask import Flask, render_template, request, jsonify
from datetime import datetime

app = Flask(__name__)

cloud_data = []

@app.route('/')
def dashboard():
    return render_template('hangars.html', hangars=cloud_data)

@app.route('/receive', methods=['POST'])
def receive():
    data = request.json
    data['timestamp'] = datetime.now().isoformat()
    cloud_data.append(data)
    return jsonify({"status": "received"})

@app.route('/get_data')
def get_data():
    return jsonify(cloud_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)