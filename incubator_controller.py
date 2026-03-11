"""
╔══════════════════════════════════════════════════════════╗
║       IoT Smart Incubator Controller v1.0                ║
║       Raspberry Pi + DHT22 + Relay + MQTT                ║
╚══════════════════════════════════════════════════════════╝

Hardware:
  - Raspberry Pi 4 (controller)
  - DHT22 sensor (temperature + humidity)
  - DS18B20 (egg surface temp probe)
  - 4-channel relay module (heater, fan, humidifier, turner)
  - SSD1306 OLED display (128x64)
  - Buzzer (alarms)

Wiring:
  DHT22  → GPIO 4
  DS18B20→ GPIO 17 (1-Wire)
  Relay 1→ GPIO 26  (Heater)
  Relay 2→ GPIO 19  (Fan)
  Relay 3→ GPIO 13  (Humidifier)
  Relay 4→ GPIO 6   (Egg Turner Motor)
  Buzzer → GPIO 24
  OLED   → I2C (SDA=GPIO2, SCL=GPIO3)
"""

import time
import json
import logging
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

# ── Hardware libraries (install via pip) ──────────────────
try:
    import Adafruit_DHT
    import RPi.GPIO as GPIO
    import board
    import busio
    import adafruit_ssd1306
    from PIL import Image, ImageDraw, ImageFont
    import paho.mqtt.client as mqtt
    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False
    print("⚠️  Hardware libraries not found — running in SIMULATION mode.")

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("incubator.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("incubator")


# ─────────────────────────────────────────────────────────
#  Enums & Constants
# ─────────────────────────────────────────────────────────

class IncubatorMode(Enum):
    IDLE     = "idle"
    RUNNING  = "running"
    ALARM    = "alarm"
    LOCKDOWN = "lockdown"   # last 3 days before hatch — no turning


class EggType(Enum):
    CHICKEN = "chicken"   # 21 days, 37.5°C, 55–60% RH
    DUCK    = "duck"      # 28 days, 37.5°C, 55–65% RH
    QUAIL   = "quail"     # 17 days, 37.5°C, 45–55% RH
    GOOSE   = "goose"     # 30 days, 37.5°C, 55–75% RH
    TURKEY  = "turkey"    # 28 days, 37.5°C, 55–65% RH


EGG_PROFILES = {
    EggType.CHICKEN: dict(incubation_days=21, temp_c=37.5, humidity_pct=57, lockdown_day=18, lockdown_humidity=65),
    EggType.DUCK:    dict(incubation_days=28, temp_c=37.5, humidity_pct=60, lockdown_day=25, lockdown_humidity=70),
    EggType.QUAIL:   dict(incubation_days=17, temp_c=37.5, humidity_pct=50, lockdown_day=14, lockdown_humidity=60),
    EggType.GOOSE:   dict(incubation_days=30, temp_c=37.5, humidity_pct=65, lockdown_day=27, lockdown_humidity=75),
    EggType.TURKEY:  dict(incubation_days=28, temp_c=37.5, humidity_pct=60, lockdown_day=25, lockdown_humidity=70),
}

# GPIO Pins
PIN_DHT22      = 4
PIN_DS18B20    = 17
PIN_RELAY_HEAT = 26
PIN_RELAY_FAN  = 19
PIN_RELAY_HUM  = 13
PIN_RELAY_TURN = 6
PIN_BUZZER     = 24

SENSOR_DHT22 = 22   # Adafruit_DHT.DHT22


# ─────────────────────────────────────────────────────────
#  Data Classes
# ─────────────────────────────────────────────────────────

@dataclass
class SensorReading:
    temperature_c: float
    humidity_pct: float
    egg_temp_c: Optional[float]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def temperature_f(self) -> float:
        return round(self.temperature_c * 9 / 5 + 32, 2)

    def to_dict(self) -> dict:
        return {**asdict(self), "temperature_f": self.temperature_f}


@dataclass
class RelayState:
    heater:     bool = False
    fan:        bool = False
    humidifier: bool = False
    turner:     bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlarmState:
    temp_high:   bool = False
    temp_low:    bool = False
    humid_high:  bool = False
    humid_low:   bool = False
    sensor_fail: bool = False

    @property
    def any_active(self) -> bool:
        return any(asdict(self).values())


@dataclass
class IncubatorConfig:
    egg_type:       EggType = EggType.CHICKEN
    start_date:     Optional[str] = None          # ISO date string
    temp_setpoint:  float = 37.5                  # °C
    humidity_setpoint: float = 57.0               # %
    temp_tolerance: float = 0.5                   # ±°C
    humid_tolerance: float = 5.0                  # ±%
    turn_interval_hours: float = 4.0              # hours between turns
    turn_duration_sec:   int   = 60               # seconds to run turner motor
    read_interval_sec:   int   = 10               # sensor poll interval
    mqtt_broker:    str = "localhost"
    mqtt_port:      int = 1883
    mqtt_topic_base: str = "incubator"

    @property
    def profile(self) -> dict:
        return EGG_PROFILES[self.egg_type]

    @property
    def incubation_days(self) -> int:
        return self.profile["incubation_days"]

    @property
    def lockdown_day(self) -> int:
        return self.profile["lockdown_day"]

    @property
    def day_number(self) -> Optional[int]:
        if not self.start_date:
            return None
        delta = datetime.now() - datetime.fromisoformat(self.start_date)
        return delta.days + 1

    @property
    def is_lockdown(self) -> bool:
        day = self.day_number
        return day is not None and day >= self.lockdown_day

    @property
    def days_remaining(self) -> Optional[int]:
        day = self.day_number
        if day is None:
            return None
        return max(0, self.incubation_days - day + 1)


# ─────────────────────────────────────────────────────────
#  Sensor Manager
# ─────────────────────────────────────────────────────────

class SensorManager:
    """Handles all sensor reads with retry logic and smoothing."""

    def __init__(self, config: IncubatorConfig):
        self.config = config
        self._readings: list[SensorReading] = []
        self._window = 5   # rolling average window

    def read_dht22(self) -> tuple[Optional[float], Optional[float]]:
        """Read temperature and humidity from DHT22."""
        if not HARDWARE_AVAILABLE:
            # Simulate realistic readings
            import random
            base_t = self.config.temp_setpoint + random.uniform(-0.8, 0.8)
            base_h = self.config.humidity_setpoint + random.uniform(-3, 3)
            return round(base_t, 2), round(base_h, 1)

        for attempt in range(3):
            humidity, temperature = Adafruit_DHT.read_retry(
                SENSOR_DHT22, PIN_DHT22, retries=3, delay_seconds=2
            )
            if temperature is not None and humidity is not None:
                if 10 <= temperature <= 50 and 0 <= humidity <= 100:
                    return round(temperature, 2), round(humidity, 1)
            log.warning(f"DHT22 read attempt {attempt+1} failed")
            time.sleep(1)
        return None, None

    def read_ds18b20(self) -> Optional[float]:
        """Read egg surface temperature from DS18B20 via 1-Wire."""
        if not HARDWARE_AVAILABLE:
            import random
            return round(self.config.temp_setpoint + random.uniform(-0.3, 0.3), 2)

        try:
            base_dir = "/sys/bus/w1/devices/"
            import glob
            device_folder = glob.glob(base_dir + "28*")[0]
            device_file = device_folder + "/w1_slave"
            with open(device_file, "r") as f:
                lines = f.readlines()
            if lines[0].strip()[-3:] == "YES":
                temp_str = lines[1].split("t=")[1]
                return round(float(temp_str) / 1000.0, 2)
        except Exception as e:
            log.warning(f"DS18B20 read failed: {e}")
        return None

    def get_reading(self) -> Optional[SensorReading]:
        """Get a validated, smoothed sensor reading."""
        temp, humid = self.read_dht22()
        if temp is None or humid is None:
            log.error("Sensor read failed — both DHT22 values are None.")
            return None

        egg_temp = self.read_ds18b20()

        reading = SensorReading(
            temperature_c=temp,
            humidity_pct=humid,
            egg_temp_c=egg_temp,
        )
        self._readings.append(reading)
        if len(self._readings) > self._window:
            self._readings.pop(0)

        return reading

    def get_smoothed(self) -> Optional[SensorReading]:
        """Return a rolling-average smoothed reading."""
        if not self._readings:
            return None
        n = len(self._readings)
        avg_t = sum(r.temperature_c for r in self._readings) / n
        avg_h = sum(r.humidity_pct   for r in self._readings) / n
        egg_temps = [r.egg_temp_c for r in self._readings if r.egg_temp_c]
        avg_e = sum(egg_temps) / len(egg_temps) if egg_temps else None
        return SensorReading(round(avg_t, 2), round(avg_h, 1), avg_e)


# ─────────────────────────────────────────────────────────
#  GPIO / Relay Controller
# ─────────────────────────────────────────────────────────

class RelayController:
    """Manages all GPIO outputs (relays + buzzer)."""

    PINS = {
        "heater":     PIN_RELAY_HEAT,
        "fan":        PIN_RELAY_FAN,
        "humidifier": PIN_RELAY_HUM,
        "turner":     PIN_RELAY_TURN,
        "buzzer":     PIN_BUZZER,
    }

    def __init__(self):
        self.state = RelayState()
        self._setup_gpio()

    def _setup_gpio(self):
        if not HARDWARE_AVAILABLE:
            log.info("GPIO simulation mode active.")
            return
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in self.PINS.values():
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)  # HIGH = relay OFF

    def _set_pin(self, pin: int, active: bool):
        if HARDWARE_AVAILABLE:
            GPIO.output(pin, GPIO.LOW if active else GPIO.HIGH)  # Active LOW relay

    def set_heater(self, on: bool):
        self.state.heater = on
        self._set_pin(self.PINS["heater"], on)
        log.debug(f"Heater → {'ON' if on else 'OFF'}")

    def set_fan(self, on: bool):
        self.state.fan = on
        self._set_pin(self.PINS["fan"], on)
        log.debug(f"Fan → {'ON' if on else 'OFF'}")

    def set_humidifier(self, on: bool):
        self.state.humidifier = on
        self._set_pin(self.PINS["humidifier"], on)
        log.debug(f"Humidifier → {'ON' if on else 'OFF'}")

    def set_turner(self, on: bool):
        self.state.turner = on
        self._set_pin(self.PINS["turner"], on)
        log.debug(f"Turner → {'ON' if on else 'OFF'}")

    def beep(self, times: int = 1, duration: float = 0.2):
        """Sound the buzzer n times."""
        if not HARDWARE_AVAILABLE:
            print(f"  🔔 BEEP x{times}")
            return
        for _ in range(times):
            GPIO.output(self.PINS["buzzer"], GPIO.HIGH)
            time.sleep(duration)
            GPIO.output(self.PINS["buzzer"], GPIO.LOW)
            time.sleep(0.1)

    def all_off(self):
        """Safety: turn off all outputs."""
        self.set_heater(False)
        self.set_fan(False)
        self.set_humidifier(False)
        self.set_turner(False)
        log.info("All relays OFF (safety shutdown).")

    def cleanup(self):
        self.all_off()
        if HARDWARE_AVAILABLE:
            GPIO.cleanup()


# ─────────────────────────────────────────────────────────
#  PID Controller
# ─────────────────────────────────────────────────────────

class PIDController:
    """
    Simple PID controller for temperature regulation.
    Output is duty-cycle percentage (0–100).
    """

    def __init__(self, kp=2.0, ki=0.5, kd=1.0, setpoint=37.5,
                 output_min=0.0, output_max=100.0):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint
        self.output_min = output_min
        self.output_max = output_max

        self._integral   = 0.0
        self._prev_error = 0.0
        self._prev_time  = None

    def update(self, measured: float) -> float:
        now = time.time()
        dt = (now - self._prev_time) if self._prev_time else 1.0
        self._prev_time = now

        error = self.setpoint - measured
        self._integral += error * dt
        # Anti-windup clamp
        self._integral = max(-50, min(50, self._integral))
        derivative = (error - self._prev_error) / dt if dt > 0 else 0
        self._prev_error = error

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        return max(self.output_min, min(self.output_max, output))

    def reset(self):
        self._integral   = 0.0
        self._prev_error = 0.0
        self._prev_time  = None


# ─────────────────────────────────────────────────────────
#  MQTT Client
# ─────────────────────────────────────────────────────────

class MQTTPublisher:
    """Publishes incubator telemetry to an MQTT broker."""

    def __init__(self, config: IncubatorConfig):
        self.config = config
        self.connected = False
        self._client = None

        if not HARDWARE_AVAILABLE:
            log.info("MQTT simulation mode — not connecting to broker.")
            return

        try:
            self._client = mqtt.Client(client_id="incubator-controller")
            self._client.on_connect    = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.connect(config.mqtt_broker, config.mqtt_port, keepalive=60)
            self._client.loop_start()
        except Exception as e:
            log.warning(f"MQTT connection failed: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = rc == 0
        log.info(f"MQTT {'connected' if self.connected else f'failed (rc={rc})'}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        log.warning(f"MQTT disconnected (rc={rc})")

    def publish(self, topic: str, payload: dict):
        full_topic = f"{self.config.mqtt_topic_base}/{topic}"
        msg = json.dumps(payload)
        if HARDWARE_AVAILABLE and self._client and self.connected:
            self._client.publish(full_topic, msg, qos=1, retain=True)
        else:
            log.debug(f"MQTT [{full_topic}]: {msg}")

    def publish_telemetry(self, reading: SensorReading, relays: RelayState, alarms: AlarmState, config: IncubatorConfig):
        self.publish("sensors", reading.to_dict())
        self.publish("relays",  relays.to_dict())
        self.publish("status", {
            "day":            config.day_number,
            "days_remaining": config.days_remaining,
            "is_lockdown":    config.is_lockdown,
            "egg_type":       config.egg_type.value,
            "mode":           "lockdown" if config.is_lockdown else "incubating",
            "alarms":         asdict(alarms),
            "timestamp":      datetime.now().isoformat(),
        })

    def disconnect(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()


# ─────────────────────────────────────────────────────────
#  OLED Display
# ─────────────────────────────────────────────────────────

class OLEDDisplay:
    """Manages the 128×64 SSD1306 OLED display."""

    WIDTH  = 128
    HEIGHT = 64

    def __init__(self):
        self._disp = None
        self._image = None
        self._draw  = None

        if not HARDWARE_AVAILABLE:
            return

        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self._disp = adafruit_ssd1306.SSD1306_I2C(self.WIDTH, self.HEIGHT, i2c)
            self._disp.fill(0)
            self._disp.show()
            self._image = Image.new("1", (self.WIDTH, self.HEIGHT))
            self._draw  = ImageDraw.Draw(self._image)
            self._font  = ImageFont.load_default()
            log.info("OLED display initialized.")
        except Exception as e:
            log.warning(f"OLED init failed: {e}")

    def update(self, reading: SensorReading, config: IncubatorConfig, alarms: AlarmState):
        """Render current state to OLED."""
        if not HARDWARE_AVAILABLE or not self._disp:
            self._print_console(reading, config, alarms)
            return

        self._draw.rectangle((0, 0, self.WIDTH, self.HEIGHT), fill=0)

        day_str  = f"Day {config.day_number or '-'}/{config.incubation_days}"
        type_str = config.egg_type.value.capitalize()
        temp_str = f"T: {reading.temperature_c:.1f}C /{config.temp_setpoint:.1f}"
        hum_str  = f"H: {reading.humidity_pct:.0f}% /{config.humidity_setpoint:.0f}"
        alarm_str = "⚠ ALARM" if alarms.any_active else "OK"
        lock_str  = "LOCKDOWN" if config.is_lockdown else ""

        lines = [type_str + "  " + day_str, temp_str, hum_str, alarm_str + "  " + lock_str]
        for i, line in enumerate(lines):
            self._draw.text((0, i * 16), line, font=self._font, fill=255)

        self._disp.image(self._image)
        self._disp.show()

    def _print_console(self, reading: SensorReading, config: IncubatorConfig, alarms: AlarmState):
        """Fallback: print status to console instead of OLED."""
        day = config.day_number or "-"
        remaining = config.days_remaining or "-"
        alarm_str = "⚠  ALARM ACTIVE" if alarms.any_active else "✓  All OK"
        lock_str  = " | 🔒 LOCKDOWN" if config.is_lockdown else ""
        print(
            f"\r  [{datetime.now().strftime('%H:%M:%S')}] "
            f"{config.egg_type.value.upper()} Day {day}/{config.incubation_days} "
            f"({remaining}d left{lock_str}) | "
            f"T: {reading.temperature_c:.1f}°C (set {config.temp_setpoint}) | "
            f"H: {reading.humidity_pct:.0f}% (set {config.humidity_setpoint:.0f}) | "
            f"{alarm_str}",
            end="", flush=True,
        )

    def clear(self):
        if self._disp:
            self._disp.fill(0)
            self._disp.show()


# ─────────────────────────────────────────────────────────
#  Data Logger
# ─────────────────────────────────────────────────────────

class DataLogger:
    """Appends sensor readings to a JSONL log file."""

    def __init__(self, filename: str = "incubator_data.jsonl"):
        self.filename = filename

    def log(self, reading: SensorReading, relays: RelayState, config: IncubatorConfig):
        record = {
            **reading.to_dict(),
            "relays":    relays.to_dict(),
            "day":       config.day_number,
            "egg_type":  config.egg_type.value,
            "setpoint_t": config.temp_setpoint,
            "setpoint_h": config.humidity_setpoint,
        }
        with open(self.filename, "a") as f:
            f.write(json.dumps(record) + "\n")


# ─────────────────────────────────────────────────────────
#  Main Incubator Controller
# ─────────────────────────────────────────────────────────

class IncubatorController:
    """
    Top-level orchestrator for the IoT incubator.

    Coordinates sensors, relays, PID control, MQTT,
    display, alarm management, and egg turning schedule.

    Example:
        config = IncubatorConfig(egg_type=EggType.CHICKEN)
        config.start_date = "2024-03-01"
        controller = IncubatorController(config)
        controller.start()
    """

    def __init__(self, config: IncubatorConfig):
        self.config  = config
        self.mode    = IncubatorMode.IDLE
        self.alarms  = AlarmState()
        self._running = False

        self.sensors = SensorManager(config)
        self.relays  = RelayController()
        self.pid     = PIDController(setpoint=config.temp_setpoint)
        self.mqtt    = MQTTPublisher(config)
        self.display = OLEDDisplay()
        self.logger  = DataLogger()

        self._last_turn_time: Optional[datetime] = None
        self._last_reading:   Optional[SensorReading] = None
        self._lock = threading.Lock()

    # ── Control Logic ─────────────────────────────────────

    def _regulate_temperature(self, reading: SensorReading):
        """Bang-bang control with PID overlay for heater + fan."""
        t   = reading.temperature_c
        sp  = self.config.temp_setpoint
        tol = self.config.temp_tolerance

        pid_output = self.pid.update(t)

        if t < sp - tol:
            self.relays.set_heater(True)
            self.relays.set_fan(True)      # circulate warm air
        elif t > sp + tol:
            self.relays.set_heater(False)
            self.relays.set_fan(True)      # cool down
        else:
            # Within tolerance — use PID duty cycle
            duty = pid_output / 100.0
            self.relays.set_heater(duty > 0.5)
            self.relays.set_fan(True)

    def _regulate_humidity(self, reading: SensorReading):
        """Simple on/off humidity control."""
        h   = reading.humidity_pct
        sp  = self.config.humidity_setpoint
        tol = self.config.humid_tolerance

        if h < sp - tol:
            self.relays.set_humidifier(True)
        elif h > sp + tol:
            self.relays.set_humidifier(False)

    def _check_turning(self):
        """Trigger egg turning if interval has elapsed (skip in lockdown)."""
        if self.config.is_lockdown:
            if self.relays.state.turner:
                self.relays.set_turner(False)
            return

        now = datetime.now()
        interval = timedelta(hours=self.config.turn_interval_hours)

        if self._last_turn_time is None or (now - self._last_turn_time) >= interval:
            log.info("🥚 Turning eggs...")
            self.relays.set_turner(True)
            time.sleep(self.config.turn_duration_sec)
            self.relays.set_turner(False)
            self._last_turn_time = now
            self.relays.beep(1)
            log.info("🥚 Turn complete.")

    def _check_alarms(self, reading: SensorReading) -> AlarmState:
        """Evaluate sensor readings against alarm thresholds."""
        alarms = AlarmState()
        t, h = reading.temperature_c, reading.humidity_pct
        sp_t = self.config.temp_setpoint
        sp_h = self.config.humidity_setpoint

        # Temperature alarms (tighter than control tolerance)
        alarms.temp_high = t > sp_t + 1.5
        alarms.temp_low  = t < sp_t - 1.5

        # Humidity alarms
        alarms.humid_high = h > sp_h + 10
        alarms.humid_low  = h < sp_h - 10

        if alarms.any_active:
            self.mode = IncubatorMode.ALARM
            self.relays.beep(3, duration=0.1)
            log.warning(f"🚨 ALARM: {alarms}")
        else:
            if self.mode == IncubatorMode.ALARM:
                self.mode = IncubatorMode.RUNNING
            self.relays.beep(1, duration=0.05)

        return alarms

    def _apply_lockdown_settings(self):
        """On lockdown day, update setpoints for hatching phase."""
        profile = self.config.profile
        self.config.humidity_setpoint = profile["lockdown_humidity"]
        self.pid.setpoint = self.config.temp_setpoint
        log.info(f"🔒 LOCKDOWN MODE — humidity raised to {self.config.humidity_setpoint}%")

    # ── Main Loop ─────────────────────────────────────────

    def _control_loop(self):
        """Main sensor-read → control → publish loop."""
        lockdown_notified = False

        while self._running:
            try:
                reading = self.sensors.get_reading()
                if reading is None:
                    self.alarms.sensor_fail = True
                    self.relays.beep(5, duration=0.05)
                    log.error("Sensor failure — heater kept at last state.")
                    time.sleep(self.config.read_interval_sec)
                    continue

                self.alarms.sensor_fail = False
                self._last_reading = reading

                # Lockdown transition
                if self.config.is_lockdown and not lockdown_notified:
                    self._apply_lockdown_settings()
                    lockdown_notified = True

                # Control actions
                self._regulate_temperature(reading)
                self._regulate_humidity(reading)
                self._check_turning()

                # Alarms
                alarms = self._check_alarms(reading)
                self.alarms = alarms

                # Output
                self.display.update(reading, self.config, alarms)
                self.logger.log(reading, self.relays.state, self.config)
                self.mqtt.publish_telemetry(reading, self.relays.state, alarms, self.config)

            except Exception as e:
                log.exception(f"Control loop error: {e}")

            time.sleep(self.config.read_interval_sec)

    # ── Public API ────────────────────────────────────────

    def start(self):
        """Start the incubator controller."""
        if self._running:
            log.warning("Controller already running.")
            return

        self._running = True
        self.mode = IncubatorMode.RUNNING

        if not self.config.start_date:
            self.config.start_date = datetime.now().strftime("%Y-%m-%d")

        log.info(f"🐣 Incubator started — {self.config.egg_type.value.upper()} eggs")
        log.info(f"   Target: {self.config.temp_setpoint}°C | {self.config.humidity_setpoint}% RH")
        log.info(f"   Start date: {self.config.start_date} | Day {self.config.day_number}")
        log.info(f"   Turn every {self.config.turn_interval_hours}h | Lockdown on day {self.config.lockdown_day}")

        self.relays.beep(2)

        self._thread = threading.Thread(target=self._control_loop, daemon=True, name="control-loop")
        self._thread.start()

    def stop(self):
        """Gracefully stop the incubator."""
        self._running = False
        self.mode = IncubatorMode.IDLE
        self.relays.all_off()
        self.display.clear()
        self.mqtt.disconnect()
        log.info("🛑 Incubator stopped.")

    def get_status(self) -> dict:
        """Return current status as a dictionary (for API / web dashboard)."""
        reading = self._last_reading
        return {
            "mode":           self.mode.value,
            "day":            self.config.day_number,
            "days_remaining": self.config.days_remaining,
            "egg_type":       self.config.egg_type.value,
            "is_lockdown":    self.config.is_lockdown,
            "setpoints": {
                "temperature": self.config.temp_setpoint,
                "humidity":    self.config.humidity_setpoint,
            },
            "sensors": reading.to_dict() if reading else None,
            "relays":  self.relays.state.to_dict(),
            "alarms":  asdict(self.alarms),
            "timestamp": datetime.now().isoformat(),
        }

    def set_egg_type(self, egg_type: EggType):
        """Change egg type and reload profile (only when not running)."""
        if self._running:
            raise RuntimeError("Cannot change egg type while running.")
        self.config.egg_type = egg_type
        profile = EGG_PROFILES[egg_type]
        self.config.temp_setpoint     = profile["temp_c"]
        self.config.humidity_setpoint = profile["humidity_pct"]
        self.pid.setpoint = self.config.temp_setpoint
        log.info(f"Egg type set to {egg_type.value} — profile loaded.")


# ─────────────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = IncubatorConfig(
        egg_type=EggType.CHICKEN,
        start_date=datetime.now().strftime("%Y-%m-%d"),
        temp_setpoint=37.5,
        humidity_setpoint=57.0,
        turn_interval_hours=4,
        read_interval_sec=10,
    )

    controller = IncubatorController(config)

    try:
        controller.start()
        print("\n  Press Ctrl+C to stop.\n")
        while True:
            time.sleep(60)
            status = controller.get_status()
            print(f"\n  STATUS: Day {status['day']} | "
                  f"Mode: {status['mode']} | "
                  f"Alarms: {'YES' if any(status['alarms'].values()) else 'none'}")
    except KeyboardInterrupt:
        print("\n\n  Shutting down...")
        controller.stop()
