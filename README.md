# 🐣 IoT Smart Incubator

![Python](https://img.shields.io/badge/Python-3.11+-3776ab?logo=python&logoColor=white)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-4-c51a4a?logo=raspberrypi&logoColor=white)
![MQTT](https://img.shields.io/badge/MQTT-Mosquitto-660066?logo=eclipsemosquitto&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask&logoColor=white)
![Arduino](https://img.shields.io/badge/Arduino-C++-00878f?logo=arduino&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-00875a)

A fully-featured IoT egg incubator controller built on **Raspberry Pi 4**. Maintains precise temperature and humidity using PID control, automates egg turning every 4 hours, publishes real-time telemetry over MQTT, and serves a live web dashboard — all running 24/7 in a single Python process.

> 🎯 Portfolio project showcasing embedded Python, hardware interfacing, PID control, IoT protocols, real-time data logging, and full-stack development.

---

## ✨ Features

### 🌡️ Sensing & Control
- **DHT22** — air temperature + humidity with 5-sample rolling average and retry logic
- **DS18B20** — egg surface temperature probe via 1-Wire interface
- **PID controller** — anti-windup integral clamp, ±0.5°C tolerance band
- **4-channel relay** — heater, fan, humidifier, egg turner (Active LOW, GPIO BCM)
- **Humidity on/off control** — ultrasonic fogger, auto-raises RH in lockdown phase

### 📡 Connectivity & Data
- **MQTT telemetry** — publishes to 3 topics every 10 seconds
- **Flask REST API** — adjust setpoints, start/stop, query history remotely
- **Chart.js dashboard** — live trend graphs, auto-refresh every 5 seconds
- **JSONL data log** — persistent per-cycle log, CSV export
- **SSD1306 OLED** — 128×64 display: temp, humidity, day counter, alarms

### 🥚 Egg Profiles & Scheduling
- **5 species profiles** — chicken, duck, quail, goose, turkey
- **Auto lockdown mode** — halts turning, raises humidity on schedule
- **Egg turning** — alternating direction, limit-switch end-stops, configurable interval
- **Alarm system** — buzzer + MQTT alert on temp/humidity exceedance or sensor failure

---

## 🏗️ Project Structure

```
iot-incubator/
├── incubator_controller.py   # Main Raspberry Pi controller
│     ├── SensorManager       #   DHT22 + DS18B20 reads, smoothing
│     ├── RelayController     #   GPIO outputs, all relay states
│     ├── PIDController       #   Heater duty-cycle regulation
│     ├── MQTTPublisher       #   Telemetry to MQTT broker
│     ├── OLEDDisplay         #   SSD1306 I2C display
│     ├── DataLogger          #   JSONL rolling log
│     └── IncubatorController #   Top-level orchestrator
│
├── web_dashboard.py          # Flask REST API + HTML dashboard
│
├── egg_turner/
│   └── egg_turner.ino        # Arduino Nano — standalone turner module
│
├── requirements.txt
└── README.md
```

---

## ⚡ Quick Start

### 1. Enable Interfaces on Raspberry Pi

```bash
sudo raspi-config
# → Interface Options → I2C    → Enable
# → Interface Options → 1-Wire → Enable
sudo reboot
```

### 2. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/iot-incubator.git
cd iot-incubator
pip install -r requirements.txt
```

### 3. Install MQTT Broker

```bash
sudo apt install mosquitto mosquitto-clients -y
sudo systemctl enable --now mosquitto

# Verify:
mosquitto_sub -t 'incubator/#' -v
```

### 4. Run

```bash
# Terminal-only mode
python incubator_controller.py

# With live web dashboard → http://<pi-ip>:5000
python web_dashboard.py
```

### 5. Arduino Turner (Optional)

Open `egg_turner/egg_turner.ino` in Arduino IDE, select **Arduino Nano**, upload, then connect via USB serial to the Pi.

---

## 🐍 Python API Usage

```python
from incubator_controller import IncubatorController, IncubatorConfig, EggType

config = IncubatorConfig(
    egg_type=EggType.CHICKEN,
    start_date="2024-03-01",
    temp_setpoint=37.5,
    humidity_setpoint=57.0,
    turn_interval_hours=4,
    read_interval_sec=10,
)

controller = IncubatorController(config)
controller.start()

# Get live status
status = controller.get_status()
# {
#   "day": 12, "days_remaining": 9, "is_lockdown": False,
#   "sensors": {"temperature_c": 37.5, "humidity_pct": 58.0, ...},
#   "relays":  {"heater": True, "fan": True, ...},
#   "alarms":  {"temp_high": False, "temp_low": False, ...}
# }

controller.stop()
```

---

## 🌐 REST API Reference

Base URL: `http://<pi-ip>:5000`

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Current state — sensors, relays, alarms, day |
| `GET` | `/api/history?hours=N` | Recent log records (last N hours, max 500 pts) |
| `POST` | `/api/setpoint` | Update temp/humidity targets |
| `POST` | `/api/control` | Start or stop the controller |
| `POST` | `/api/egg_type` | Switch egg species profile |
| `GET` | `/` | Live Chart.js HTML dashboard |

**Examples:**

```bash
# Get status
curl http://pi.local:5000/api/status

# Change setpoints
curl -X POST http://pi.local:5000/api/setpoint \
     -H "Content-Type: application/json" \
     -d '{"temperature": 37.8, "humidity": 62}'

# Subscribe to MQTT topics
mosquitto_sub -t 'incubator/#' -v
#  incubator/sensors  {"temperature_c": 37.5, "humidity_pct": 58.0, ...}
#  incubator/relays   {"heater": true, "fan": true, "humidifier": false, ...}
#  incubator/status   {"day": 12, "is_lockdown": false, "alarms": {...}}
```

---

## 🔌 GPIO Wiring (Raspberry Pi BCM)

| Component | Signal | GPIO (BCM) | Notes |
|---|---|---|---|
| DHT22 | Data | GPIO 4 | 10kΩ pull-up to 3.3V |
| DS18B20 | Data (1-Wire) | GPIO 17 | 4.7kΩ pull-up · enable 1-wire in raspi-config |
| Relay 1 | Heater | GPIO 26 | Active LOW |
| Relay 2 | Fan | GPIO 19 | Active LOW |
| Relay 3 | Humidifier | GPIO 13 | Active LOW · ultrasonic fogger |
| Relay 4 | Egg Turner | GPIO 6 | Active LOW (or use Arduino module) |
| Buzzer | Alarm | GPIO 24 | Active HIGH · NPN transistor driver |
| SSD1306 OLED | SDA / SCL | GPIO 2 / 3 | I2C · enable i2c in raspi-config |

---

## 🥚 Egg Species Profiles

| Species | Days | Temp (°C) | Humidity (%) | Lockdown Day | Lockdown RH |
|---|---|---|---|---|---|
| 🐔 Chicken | 21 | 37.5 | 55–60% | Day 18 | 65% |
| 🦆 Duck | 28 | 37.5 | 55–65% | Day 25 | 70% |
| 🐦 Quail | 17 | 37.5 | 45–55% | Day 14 | 60% |
| 🪿 Goose | 30 | 37.5 | 55–75% | Day 27 | 75% |
| 🦃 Turkey | 28 | 37.5 | 55–65% | Day 25 | 70% |

Profiles are selected via `IncubatorConfig(egg_type=EggType.DUCK)` or the REST API. On lockdown day the system automatically halts turning and raises humidity.

---

## 💻 Simulation Mode

No Raspberry Pi? No problem. The controller auto-detects missing hardware libraries and runs in simulation:

```bash
python incubator_controller.py

⚠️  Hardware libraries not found — running in SIMULATION mode.
🐣 Incubator started — CHICKEN eggs
   Target: 37.5°C | 57% RH | Turn every 4h | Lockdown day 18
[14:22:07] T: 37.4°C | H: 58% | ✓ All OK | MQTT published
```

---

## 🔗 Integrations

Works with any MQTT-compatible platform:

- **Home Assistant** — auto-discover sensors, push mobile alerts
- **Node-RED** — visual flows, connect to Telegram / Slack / email
- **Grafana + InfluxDB** — long-term telemetry dashboards
- **ThingsBoard** — open-source IoT platform, drag-drop dashboards
- **MQTT Explorer** — desktop tool for browsing topics live

---

## 🧰 Bill of Materials

| Component | Qty | Notes |
|---|---|---|
| Raspberry Pi 4 (2GB+) | 1× | Main controller |
| DHT22 sensor | 1× | Temp + humidity |
| DS18B20 probe | 1× | Egg surface temperature |
| 4-channel relay module | 1× | 250V/10A AC rated |
| SSD1306 OLED (128×64) | 1× | I2C display |
| Ceramic heater element | 1× | 40–100W |
| 12V DC fan | 1× | Air circulation |
| Ultrasonic fogger | 1× | Humidity source |
| Piezo buzzer | 1× | Alarms |
| Arduino Nano *(optional)* | 1× | Standalone turner backup |
| L298N motor driver | 1× | For Arduino turner |
| 12V gear motor (5 RPM) | 1× | Egg tray rotation |
| DS3231 RTC module | 1× | For Arduino turner timing |

---

## 🚀 Push to GitHub

```bash
git init
git add .
git commit -m "feat: IoT Smart Incubator v1.0"
git remote add origin https://github.com/YOUR_USERNAME/iot-incubator.git
git push -u origin main
```

---

## 📜 License

[MIT](LICENSE) © 2024 Your Name

*Made with ❤️ and Python 🐍 on Raspberry Pi 🥧*
