/*
 * ╔══════════════════════════════════════════════════════╗
 * ║    IoT Incubator — Arduino Egg Turner Module         ║
 * ║    Standalone backup turner (no Pi needed)           ║
 * ║                                                      ║
 * ║    Hardware:                                         ║
 * ║      Arduino Nano / Uno                              ║
 * ║      L298N Motor Driver                              ║
 * ║      12V DC Gear Motor (5 RPM)                       ║
 * ║      Limit switches (left & right end-stop)         ║
 * ║      16x2 LCD (I2C)                                  ║
 * ║      RTC DS3231 (accurate timekeeping)               ║
 * ╚══════════════════════════════════════════════════════╝
 *
 * Wiring:
 *   L298N IN1  → D5
 *   L298N IN2  → D6
 *   L298N ENA  → D9 (PWM speed)
 *   Limit SW L → D2 (interrupt)
 *   Limit SW R → D3 (interrupt)
 *   LCD SDA    → A4
 *   LCD SCL    → A5
 *   DS3231 SDA → A4 (shared I2C bus)
 *   DS3231 SCL → A5
 *   Buzzer     → D11
 */

#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <RTClib.h>

// ── Pin Definitions ─────────────────────────────────────
const int PIN_MOTOR_IN1 = 5;
const int PIN_MOTOR_IN2 = 6;
const int PIN_MOTOR_ENA = 9;   // PWM
const int PIN_LIMIT_L   = 2;   // interrupt
const int PIN_LIMIT_R   = 3;   // interrupt
const int PIN_BUZZER    = 11;

// ── Configuration ────────────────────────────────────────
const unsigned long TURN_INTERVAL_MS = 4UL * 60UL * 60UL * 1000UL;  // 4 hours
const unsigned long TURN_TIMEOUT_MS  = 30UL * 1000UL;                // 30s max move
const int           MOTOR_SPEED      = 180;                           // 0–255 PWM
const int           INCUBATION_DAYS  = 21;                            // chicken
const int           LOCKDOWN_DAY     = 18;

// ── State ────────────────────────────────────────────────
enum MotorDir { STOP, LEFT, RIGHT };

struct State {
  MotorDir   direction   = STOP;
  bool       atLeftEnd   = false;
  bool       atRightEnd  = false;
  bool       isLockdown  = false;
  int        dayNumber   = 1;
  int        totalTurns  = 0;
  unsigned long lastTurnMs = 0;
} state;

// ── Peripherals ──────────────────────────────────────────
LiquidCrystal_I2C lcd(0x27, 16, 2);
RTC_DS3231 rtc;
DateTime startDate;

// ── ISRs ─────────────────────────────────────────────────
volatile bool limitL_triggered = false;
volatile bool limitR_triggered = false;

void ISR_limitL() { limitL_triggered = true; }
void ISR_limitR() { limitR_triggered = true; }


// ─────────────────────────────────────────────────────────
void setup() {
  Serial.begin(9600);

  // Motor driver
  pinMode(PIN_MOTOR_IN1, OUTPUT);
  pinMode(PIN_MOTOR_IN2, OUTPUT);
  pinMode(PIN_MOTOR_ENA, OUTPUT);
  motorStop();

  // Limit switches (internal pull-up, active LOW)
  pinMode(PIN_LIMIT_L, INPUT_PULLUP);
  pinMode(PIN_LIMIT_R, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_LIMIT_L), ISR_limitL, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_LIMIT_R), ISR_limitR, FALLING);

  // Buzzer
  pinMode(PIN_BUZZER, OUTPUT);

  // LCD
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("IoT  Incubator");
  lcd.setCursor(0, 1); lcd.print("Turner Module");
  delay(2000);

  // RTC
  if (!rtc.begin()) {
    lcd.clear();
    lcd.print("RTC FAIL!");
    while (1);
  }
  if (rtc.lostPower()) {
    rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
  }
  startDate = rtc.now();

  beep(2, 100);
  Serial.println("Incubator Turner Ready.");
}


// ─────────────────────────────────────────────────────────
void loop() {
  DateTime now = rtc.now();
  updateDayNumber(now);
  state.isLockdown = (state.dayNumber >= LOCKDOWN_DAY);

  // Handle limit switch interrupts safely in main loop
  if (limitL_triggered) {
    limitL_triggered = false;
    state.atLeftEnd = true;
    if (state.direction == LEFT) motorStop();
  }
  if (limitR_triggered) {
    limitR_triggered = false;
    state.atRightEnd = true;
    if (state.direction == RIGHT) motorStop();
  }

  // Turning logic
  if (!state.isLockdown) {
    unsigned long elapsed = millis() - state.lastTurnMs;
    if (elapsed >= TURN_INTERVAL_MS) {
      performTurn();
      state.lastTurnMs = millis();
    }
  }

  // Serial telemetry (Raspberry Pi reads this via UART)
  static unsigned long lastTelemetry = 0;
  if (millis() - lastTelemetry > 5000) {
    sendTelemetry(now);
    lastTelemetry = millis();
  }

  updateDisplay(now);
  delay(200);
}


// ── Motor Control ─────────────────────────────────────────
void motorForward() {
  state.direction = RIGHT;
  state.atRightEnd = false;
  analogWrite(PIN_MOTOR_ENA, MOTOR_SPEED);
  digitalWrite(PIN_MOTOR_IN1, HIGH);
  digitalWrite(PIN_MOTOR_IN2, LOW);
}

void motorReverse() {
  state.direction = LEFT;
  state.atLeftEnd = false;
  analogWrite(PIN_MOTOR_ENA, MOTOR_SPEED);
  digitalWrite(PIN_MOTOR_IN1, LOW);
  digitalWrite(PIN_MOTOR_IN2, HIGH);
}

void motorStop() {
  state.direction = STOP;
  digitalWrite(PIN_MOTOR_IN1, LOW);
  digitalWrite(PIN_MOTOR_IN2, LOW);
  analogWrite(PIN_MOTOR_ENA, 0);
}

void softStop() {
  // Gradually ramp down PWM to avoid mechanical shock
  int spd = MOTOR_SPEED;
  while (spd > 0) {
    analogWrite(PIN_MOTOR_ENA, spd);
    spd -= 20;
    delay(30);
  }
  motorStop();
}


// ── Turn Sequence ─────────────────────────────────────────
void performTurn() {
  Serial.println("TURN_START");
  lcd.setCursor(0, 1);
  lcd.print("Turning...      ");

  // Alternate direction each turn
  bool goRight = (state.totalTurns % 2 == 0);

  if (goRight && !state.atRightEnd) {
    motorForward();
  } else if (!goRight && !state.atLeftEnd) {
    motorReverse();
  } else {
    // Already at end — go opposite
    goRight ? motorReverse() : motorForward();
  }

  // Wait for limit switch or timeout
  unsigned long startMs = millis();
  while (
    state.direction != STOP &&
    !limitL_triggered && !limitR_triggered &&
    (millis() - startMs) < TURN_TIMEOUT_MS
  ) {
    delay(50);
  }

  softStop();
  state.totalTurns++;
  beep(1, 80);

  Serial.print("TURN_DONE total=");
  Serial.println(state.totalTurns);
}


// ── Utilities ─────────────────────────────────────────────
void updateDayNumber(const DateTime& now) {
  TimeSpan elapsed = now - startDate;
  state.dayNumber = constrain(elapsed.days() + 1, 1, INCUBATION_DAYS);
}

void sendTelemetry(const DateTime& now) {
  Serial.print("{\"day\":");   Serial.print(state.dayNumber);
  Serial.print(",\"turns\":");  Serial.print(state.totalTurns);
  Serial.print(",\"lockdown\":"); Serial.print(state.isLockdown ? "true" : "false");
  Serial.print(",\"dir\":\"");
  if      (state.direction == LEFT)  Serial.print("left");
  else if (state.direction == RIGHT) Serial.print("right");
  else                               Serial.print("stop");
  Serial.print("\"");
  Serial.print(",\"atL\":"); Serial.print(state.atLeftEnd  ? "true" : "false");
  Serial.print(",\"atR\":"); Serial.print(state.atRightEnd ? "true" : "false");
  Serial.print(",\"time\":\"");
  Serial.print(now.hour()); Serial.print(":");
  if (now.minute() < 10) Serial.print("0");
  Serial.print(now.minute());
  Serial.println("\"}");
}

void updateDisplay(const DateTime& now) {
  char buf[17];

  lcd.setCursor(0, 0);
  snprintf(buf, sizeof(buf), "Day %02d/%02d  %02d:%02d",
           state.dayNumber, INCUBATION_DAYS, now.hour(), now.minute());
  lcd.print(buf);

  lcd.setCursor(0, 1);
  if (state.isLockdown) {
    lcd.print("** LOCKDOWN **  ");
  } else {
    unsigned long nextMs   = TURN_INTERVAL_MS - (millis() - state.lastTurnMs);
    unsigned int  nextMins = nextMs / 60000;
    snprintf(buf, sizeof(buf), "Turns:%-4d %3dm", state.totalTurns, nextMins);
    lcd.print(buf);
  }
}

void beep(int times, int ms) {
  for (int i = 0; i < times; i++) {
    tone(PIN_BUZZER, 1000, ms);
    delay(ms + 50);
  }
}
