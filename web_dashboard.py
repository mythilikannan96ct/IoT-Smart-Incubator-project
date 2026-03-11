"""
IoT Incubator — Flask Web Dashboard & REST API
================================================
Run: python web_dashboard.py
Access: http://<raspberry-pi-ip>:5000

Endpoints:
  GET  /api/status          – current incubator state
  GET  /api/history?hours=N – recent log data (JSON)
  POST /api/setpoint        – update temp/humidity targets
  POST /api/control         – start / stop controller
  GET  /                    – live HTML dashboard
"""

from flask import Flask, jsonify, request, render_template_string
from datetime import datetime
import json, os, threading, time

# Import our controller
from incubator_controller import (
    IncubatorController, IncubatorConfig, EggType, EggType
)

app = Flask(__name__)

# Global controller instance
config     = IncubatorConfig(egg_type=EggType.CHICKEN)
controller = IncubatorController(config)


# ─────────────────────────────────────────────────────────
#  REST API
# ─────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify(controller.get_status())


@app.route("/api/history")
def api_history():
    hours = int(request.args.get("hours", 24))
    log_file = "incubator_data.jsonl"
    records = []
    if os.path.exists(log_file):
        cutoff = datetime.now().timestamp() - hours * 3600
        with open(log_file) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp()
                    if ts >= cutoff:
                        records.append(r)
                except Exception:
                    pass
    return jsonify(records[-500:])  # cap at 500 points


@app.route("/api/setpoint", methods=["POST"])
def api_setpoint():
    data = request.get_json()
    if "temperature" in data:
        t = float(data["temperature"])
        if 35 <= t <= 40:
            controller.config.temp_setpoint = t
            controller.pid.setpoint = t
        else:
            return jsonify({"error": "Temperature must be 35–40°C"}), 400
    if "humidity" in data:
        h = float(data["humidity"])
        if 30 <= h <= 90:
            controller.config.humidity_setpoint = h
        else:
            return jsonify({"error": "Humidity must be 30–90%"}), 400
    return jsonify({"ok": True, "status": controller.get_status()})


@app.route("/api/control", methods=["POST"])
def api_control():
    data   = request.get_json()
    action = data.get("action")
    if action == "start":
        if not controller._running:
            controller.start()
        return jsonify({"ok": True, "running": True})
    elif action == "stop":
        controller.stop()
        return jsonify({"ok": True, "running": False})
    return jsonify({"error": "Unknown action"}), 400


@app.route("/api/egg_type", methods=["POST"])
def api_egg_type():
    data     = request.get_json()
    egg_name = data.get("type", "").upper()
    try:
        egg_type = EggType[egg_name]
        controller.set_egg_type(egg_type)
        return jsonify({"ok": True, "egg_type": egg_type.value})
    except KeyError:
        return jsonify({"error": f"Unknown egg type '{egg_name}'"}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409


# ─────────────────────────────────────────────────────────
#  HTML Dashboard (single-page, embedded)
# ─────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IoT Incubator Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0f14; color:#e0e8f0; font-family:monospace; font-size:14px; }
  h1 { color:#4dd8a0; font-size:20px; padding:20px; border-bottom:1px solid #1e3040; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; padding:20px; }
  .card { background:#0f1e2a; border:1px solid #1e3040; border-radius:8px; padding:16px; }
  .card h3 { color:#4dd8a0; font-size:12px; text-transform:uppercase; margin-bottom:8px; letter-spacing:1px; }
  .val { font-size:32px; font-weight:bold; color:#fff; }
  .sub { color:#6a8a9a; font-size:12px; margin-top:4px; }
  .on  { color:#4dd8a0; } .off { color:#6a8a9a; } .alarm { color:#ff5555; }
  canvas { max-height:200px; }
  .relay { display:inline-block; padding:4px 10px; border-radius:4px; margin:3px; font-size:12px; }
  .r-on  { background:#1a3a2a; color:#4dd8a0; border:1px solid #4dd8a0; }
  .r-off { background:#1a1a2a; color:#444; border:1px solid #333; }
</style>
</head>
<body>
<h1>🐣 IoT Incubator Dashboard</h1>
<div class="grid" id="cards">
  <div class="card"><h3>Temperature</h3><div class="val" id="temp">--</div><div class="sub" id="temp-sub">setpoint --</div></div>
  <div class="card"><h3>Humidity</h3><div class="val" id="humid">--</div><div class="sub" id="humid-sub">setpoint --</div></div>
  <div class="card"><h3>Day</h3><div class="val" id="day">--</div><div class="sub" id="day-sub">-- days left</div></div>
  <div class="card"><h3>Status</h3><div class="val" id="mode">--</div><div class="sub" id="egg-type">--</div></div>
  <div class="card"><h3>Relays</h3><div id="relays"></div></div>
  <div class="card"><h3>Alarms</h3><div id="alarms" style="font-size:13px;"></div></div>
</div>
<div style="padding:0 20px 20px">
  <div class="card"><h3>Temperature History (last 2h)</h3><canvas id="chart"></canvas></div>
</div>
<script>
const chart = new Chart(document.getElementById('chart'), {
  type:'line',
  data:{ labels:[], datasets:[
    {label:'Temp °C', data:[], borderColor:'#ff8c42', tension:.3, pointRadius:0},
    {label:'Humidity %', data:[], borderColor:'#4dd8a0', tension:.3, pointRadius:0}
  ]},
  options:{ animation:false, scales:{ y:{ticks:{color:'#6a8a9a'}}, x:{ticks:{color:'#6a8a9a',maxTicksLimit:6}} },
    plugins:{legend:{labels:{color:'#e0e8f0'}}} }
});

async function refresh() {
  const s = await fetch('/api/status').then(r=>r.json());
  const sensors = s.sensors || {};
  document.getElementById('temp').textContent    = sensors.temperature_c ? sensors.temperature_c.toFixed(1)+'°C' : '--';
  document.getElementById('temp-sub').textContent = 'setpoint '+s.setpoints.temperature+'°C';
  document.getElementById('humid').textContent   = sensors.humidity_pct ? sensors.humidity_pct.toFixed(0)+'%' : '--';
  document.getElementById('humid-sub').textContent = 'setpoint '+s.setpoints.humidity+'%';
  document.getElementById('day').textContent     = s.day || '--';
  document.getElementById('day-sub').textContent = (s.days_remaining||'--')+' days remaining'+(s.is_lockdown?' 🔒':'');
  document.getElementById('mode').className      = 'val '+(s.mode==='alarm'?'alarm':'on');
  document.getElementById('mode').textContent    = s.mode.toUpperCase();
  document.getElementById('egg-type').textContent = s.egg_type;

  const r = s.relays||{};
  document.getElementById('relays').innerHTML = ['heater','fan','humidifier','turner']
    .map(k=>`<span class="relay ${r[k]?'r-on':'r-off'}">${k.toUpperCase()}</span>`).join('');

  const a = s.alarms||{};
  const active = Object.entries(a).filter(([,v])=>v).map(([k])=>k);
  document.getElementById('alarms').innerHTML = active.length
    ? active.map(k=>`<div class="alarm">⚠ ${k.replace('_',' ')}</div>`).join('')
    : '<span class="on">✓ All clear</span>';
}

async function loadHistory() {
  const data = await fetch('/api/history?hours=2').then(r=>r.json());
  chart.data.labels = data.map(d=>d.timestamp.slice(11,16));
  chart.data.datasets[0].data = data.map(d=>d.temperature_c);
  chart.data.datasets[1].data = data.map(d=>d.humidity_pct);
  chart.update();
}

refresh(); loadHistory();
setInterval(refresh, 5000);
setInterval(loadHistory, 30000);
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    # Auto-start controller
    controller.start()
    print("🌐 Web dashboard: http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
