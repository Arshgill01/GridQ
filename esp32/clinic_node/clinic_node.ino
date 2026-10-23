// GridQ health-centre node (ESP32-WROOM DevKit). Spec §9.4, §10.4.
//
//  * polls  GET  http://GRIDQ_HOST:8000/api/node/clinic   every 1 s (timeout 1.5 s)
//  * LCD 16x2 (PCF8574): line 1 = l1, line 2 cycles l2[] every 3 s
//  * SG90 servo needle = battery SoC (slew-limited 3 deg / 50 ms)
//  * HC-SR04: 3 readings < 12 cm in a row -> POST /api/events {"type":"ev_fleet","source":"esp32"} (10 s cooldown)
//  * optional I2C sensor (auto-detected): BH1750 lux or BMP/BME280 temperature -> POST /api/sensor every 2 s
//  * link lost after 5 failed polls: LCD "LINK LOST / CLINIC LOCAL ON", GPIO2 blinks 5 Hz, servo holds
//
// Networking runs in its own FreeRTOS task on core 0 so a slow HTTP call never stalls
// the servo, LCD or ultrasonic on core 1.
//
// Libraries (Library Manager): ArduinoJson (7.x), LiquidCrystal I2C (Frank de Brabander), ESP32Servo.
// Copy secrets.h.example -> secrets.h and fill in Wi-Fi + the UNO Q's IP.

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <LiquidCrystal_I2C.h>
#include <ESP32Servo.h>
#include <ESPmDNS.h>
#include "secrets.h"   // WIFI_SSID, WIFI_PASS, GRIDQ_HOST (first guess; the node finds the UNO Q itself if it moves)

// ---------------------------------------------------------------- config (mirrors config/hardware.yaml)
const int PIN_SDA = 21, PIN_SCL = 22, PIN_SERVO = 18, PIN_TRIG = 5, PIN_ECHO = 19, PIN_LED = 2;
const int SERVO_MIN_DEG = 10, SERVO_MAX_DEG = 170;
const int EV_THRESHOLD_CM = 12, EV_CONSECUTIVE = 5;    // 5 readings x 200 ms: a hand held ~1 s, not a noise spike
const float EV_MIN_CM = 3.0f, EV_SPREAD_CM = 3.0f;      // a real hand reads steady; wiring noise jumps around
const unsigned long EV_COOLDOWN_MS = 10000;
uint8_t LCD_ADDR = 0x27;               // auto-falls back to 0x3F if 0x27 is absent
const int GRIDQ_PORT = 8000;

// ---------------------------------------------------------------- shared state (net task <-> loop)
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
char l1[17] = "GRIDQ CLINIC";
char l2[3][17] = {"CONNECTING...", "", ""};
int l2n = 1;
float socTarget = 0.5;
volatile bool linkLost = false;
volatile bool everLinked = false;     // until the first answer the LCD shows boot progress, not LINK LOST
volatile int failCount = 0;
volatile bool evPending = false;
volatile bool sensorReady = false;
char sensorKind[8] = "";
float sensorValue = 0;
volatile long lastSeq = -1;
volatile int lastHttp = 0;
String gridqHost = GRIDQ_HOST;     // current address of the UNO Q (rediscovered when the link is lost)

LiquidCrystal_I2C* lcd = nullptr;
Servo servo;

// ---------------------------------------------------------------- I2C sensor (raw drivers, no extra libs)
enum SensorType { SENS_NONE, SENS_BH1750, SENS_BMP280 };
SensorType sensType = SENS_NONE;
uint8_t sensAddr = 0;
uint16_t digT1; int16_t digT2, digT3;   // BMP/BME280 temperature calibration

bool i2cPresent(uint8_t a) { Wire.beginTransmission(a); return Wire.endTransmission() == 0; }

uint8_t rd8(uint8_t a, uint8_t reg) {
  Wire.beginTransmission(a); Wire.write(reg); Wire.endTransmission(false);
  Wire.requestFrom(a, (uint8_t)1); return Wire.available() ? Wire.read() : 0;
}
uint16_t rd16le(uint8_t a, uint8_t reg) { return rd8(a, reg) | (rd8(a, reg + 1) << 8); }
void wr8(uint8_t a, uint8_t reg, uint8_t v) { Wire.beginTransmission(a); Wire.write(reg); Wire.write(v); Wire.endTransmission(); }

void detectSensor() {
  Serial.print("I2C scan:");
  for (uint8_t a = 1; a < 127; a++) if (i2cPresent(a)) { Serial.printf(" 0x%02X", a); }
  Serial.println();
  const uint8_t bh[] = {0x23, 0x5C}, bmp[] = {0x76, 0x77};
  for (uint8_t a : bh) if (i2cPresent(a)) {
    sensType = SENS_BH1750; sensAddr = a;
    Wire.beginTransmission(a); Wire.write(0x10); Wire.endTransmission();   // continuous high-res
    Serial.printf("sensor: BH1750 at 0x%02X\n", a); return;
  }
  for (uint8_t a : bmp) if (i2cPresent(a)) {
    uint8_t id = rd8(a, 0xD0);
    if (id == 0x58 || id == 0x60) {
      sensType = SENS_BMP280; sensAddr = a;
      digT1 = rd16le(a, 0x88); digT2 = (int16_t)rd16le(a, 0x8A); digT3 = (int16_t)rd16le(a, 0x8C);
      wr8(a, 0xF4, 0x27);  // normal mode, temp + pressure oversampling x1
      Serial.printf("sensor: %s at 0x%02X\n", id == 0x60 ? "BME280" : "BMP280", a); return;
    }
  }
  if (i2cPresent(0x68)) Serial.println("sensor: MPU6050 at 0x68 (unused)");
  else Serial.println("sensor: none");
}

bool readSensor(float& v) {
  if (sensType == SENS_BH1750) {
    Wire.requestFrom(sensAddr, (uint8_t)2);
    if (Wire.available() < 2) return false;
    uint16_t raw = (Wire.read() << 8) | Wire.read();
    v = raw / 1.2f; return true;
  }
  if (sensType == SENS_BMP280) {
    int32_t adc = ((int32_t)rd8(sensAddr, 0xFA) << 12) | ((int32_t)rd8(sensAddr, 0xFB) << 4) | (rd8(sensAddr, 0xFC) >> 4);
    int32_t var1 = ((((adc >> 3) - ((int32_t)digT1 << 1))) * ((int32_t)digT2)) >> 11;
    int32_t var2 = (((((adc >> 4) - ((int32_t)digT1)) * ((adc >> 4) - ((int32_t)digT1))) >> 12) * ((int32_t)digT3)) >> 14;
    v = ((var1 + var2) * 5 + 128) / 25600.0f; return true;
  }
  return false;
}

// ---------------------------------------------------------------- network task (core 0)
String url(const char* path) { return String("http://") + gridqHost + ":" + GRIDQ_PORT + path; }

bool isGridq(const String& host) {        // does this address answer like GridQ's clinic endpoint?
  HTTPClient http;
  http.setConnectTimeout(250);
  http.setTimeout(600);
  if (!http.begin(String("http://") + host + ":" + GRIDQ_PORT + "/api/node/clinic")) return false;
  int code = http.GET();
  bool ok = code == 200 && http.getString().indexOf("\"l1\"") >= 0;
  http.end();
  return ok;
}

// 2 = GridQ answers there · 1 = a host is there but GridQ isn't listening yet (a fast refusal, e.g. the
// UNO Q is up and the app is still starting) · 0 = silence (board off, wrong address, or a stalled radio)
int probe(const String& host) {
  IPAddress ip;
  if (!ip.fromString(host)) return 0;
  WiFiClient c;
  unsigned long t = millis();
  bool open = c.connect(ip, GRIDQ_PORT, 2500);
  unsigned long dt = millis() - t;
  c.stop();
  int r = open ? (isGridq(host) ? 2 : 1) : (dt < 2000 ? 1 : 0);   // hotspot round trips reach ~0.5 s
  static int lastR = -1;
  if (r != lastR) { lastR = r; Serial.printf("probe %s: %s after %lu ms\n", host.c_str(), open ? "open" : r ? "refused" : "silent", dt); }
  return r;
}

// The hotspot may hand the UNO Q a new address after a reconnect. Try, in order:
// the last known and configured hosts, mDNS "uno-q.local", then every address on our /24, re-checking
// the known hosts as the sweep goes so a GridQ that comes up mid-sweep is picked up at once.
bool knownHostsUp() {
  if (isGridq(gridqHost)) return true;
  if (gridqHost != GRIDQ_HOST && isGridq(GRIDQ_HOST)) { gridqHost = GRIDQ_HOST; return true; }
  return false;
}

bool discover() {
  if (knownHostsUp()) return true;
  static bool mdnsUp = false;
  if (!mdnsUp) mdnsUp = MDNS.begin("gridq-clinic");      // needs the Wi-Fi link, so started lazily
  IPAddress m = mdnsUp ? MDNS.queryHost("uno-q", 1500) : IPAddress(0, 0, 0, 0);
  if (m != IPAddress(0, 0, 0, 0) && isGridq(m.toString())) { gridqHost = m.toString(); return true; }
  IPAddress me = WiFi.localIP();
  for (int i = 1; i < 255; i++) {
    if (i == me[3]) continue;
    if (i % 16 == 0 && knownHostsUp()) return true;
    IPAddress cand(me[0], me[1], me[2], i);
    WiFiClient c;
    if (c.connect(cand, GRIDQ_PORT, 120)) {
      c.stop();
      if (isGridq(cand.toString())) { gridqHost = cand.toString(); return true; }
    }
  }
  return false;
}

bool postJson(const char* path, const String& body) {
  HTTPClient http;
  http.setTimeout(1500);
  http.begin(url(path));
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
  return code == 200;
}

void netTask(void*) {
  unsigned long lastPoll = 0, lastSensor = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      failCount = failCount + 1;
      if (failCount >= 5) linkLost = true;
      static unsigned long lastRetry = 0;          // let a connection attempt finish before retrying
      if (millis() - lastRetry > 8000) { lastRetry = millis(); WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASS); }
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }
    unsigned long now = millis();
    if (evPending) {
      if (postJson("/api/events", "{\"type\":\"ev_fleet\",\"source\":\"esp32\"}")) evPending = false;
    }
    if (sensorReady && now - lastSensor >= 2000) {
      lastSensor = now;
      char body[64];
      portENTER_CRITICAL(&mux);
      snprintf(body, sizeof(body), "{\"kind\":\"%s\",\"value\":%.2f}", sensorKind, sensorValue);
      portEXIT_CRITICAL(&mux);
      postJson("/api/sensor", body);
    }
    if (now - lastPoll >= 1000) {
      lastPoll = now;
      HTTPClient http;
      http.setTimeout(1500);
      http.begin(url("/api/node/clinic"));
      int code = http.GET();
      lastHttp = code;
      bool ok = false;
      if (code == 200) {
        JsonDocument doc;
        if (!deserializeJson(doc, http.getString())) {
          portENTER_CRITICAL(&mux);
          strlcpy(l1, doc["l1"] | "", sizeof(l1));
          JsonArray a = doc["l2"].as<JsonArray>();
          l2n = 0;
          for (JsonVariant v : a) { if (l2n < 3) strlcpy(l2[l2n++], v | "", 17); }
          if (l2n == 0) { strlcpy(l2[0], "", 17); l2n = 1; }
          socTarget = doc["soc"] | socTarget;
          lastSeq = doc["seq"] | -1L;
          portEXIT_CRITICAL(&mux);
          ok = true;
        }
      }
      http.end();
      if (ok) { failCount = 0; linkLost = false; everLinked = true; }
      else {
        failCount = failCount + 1;
        if (failCount >= 5) linkLost = true;
        static unsigned long lastRejoin = 0, lastDisc = 0, refusedSince = 0;
        if (failCount >= 8) {
          int pr = probe(gridqHost);
          if (pr == 2) failCount = 0;                       // it just came up: the next poll links
          else if (pr == 1) {
            // the board is there and refusing: GridQ is still starting. Keep polling; don't drop Wi-Fi.
            // Only if that lasts 2 min (our address now belongs to another device?) search the network.
            if (refusedSince == 0) refusedSince = millis();
            if (millis() - refusedSince > 120000 && millis() - lastDisc > 60000) {
              lastDisc = millis();
              Serial.println("GridQ refused for 2 min: discovering...");
              if (discover()) { Serial.printf("found GridQ at %s\n", gridqHost.c_str()); failCount = 0; }
            }
          } else {
            refusedSince = 0;
            // total silence: a phone-hotspot radio link can stall while still "connected", so rejoin
            // first (cheap), then search the network
            if (lastRejoin == 0 || millis() - lastRejoin > 60000) {
              lastRejoin = millis();
              Serial.println("GridQ silent: rejoining Wi-Fi");
              WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASS);
            } else if (failCount >= 16 && millis() - lastDisc > 60000) {
              lastDisc = millis();
              Serial.println("GridQ silent: discovering...");
              if (discover()) { Serial.printf("found GridQ at %s\n", gridqHost.c_str()); failCount = 0; }
              else Serial.println("GridQ not found on this network");
            }
          }
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ---------------------------------------------------------------- UI loop (core 1)
void lcdLine(int row, const char* s) {
  char buf[17];
  snprintf(buf, sizeof(buf), "%-16s", s);
  lcd->setCursor(0, row);
  lcd->print(buf);
}

float servoPos = 90;
int evStreak = 0;
float evLo = 0, evHi = 0;
bool evArmed = true;
unsigned long lastEv = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  digitalWrite(PIN_TRIG, LOW);
  Wire.begin(PIN_SDA, PIN_SCL);
  if (!i2cPresent(LCD_ADDR) && i2cPresent(0x3F)) LCD_ADDR = 0x3F;
  lcd = new LiquidCrystal_I2C(LCD_ADDR, 16, 2);
  lcd->init();
  lcd->backlight();
  lcdLine(0, "GRIDQ CLINIC");
  lcdLine(1, "WIFI...");
  detectSensor();
  ESP32PWM::allocateTimer(0);
  servo.setPeriodHertz(50);
  servo.attach(PIN_SERVO, 500, 2400);
  servo.write((int)servoPos);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                // modem sleep makes the link stall silently on phone hotspots
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);
}

void loop() {
  unsigned long now = millis();
  static unsigned long tServo = 0, tUs = 0, tLcd = 0, tLed = 0, tSens = 0;
  static int l2i = 0;
  static unsigned long tCycle = 0;

  // servo: slew-limited towards the SoC target; holds position while the link is lost
  if (now - tServo >= 50) {
    tServo = now;
    if (!linkLost) {
      float tgt = SERVO_MIN_DEG + constrain(socTarget, 0.0f, 1.0f) * (SERVO_MAX_DEG - SERVO_MIN_DEG);
      float d = constrain(tgt - servoPos, -3.0f, 3.0f);
      if (fabs(d) > 0.01) { servoPos += d; servo.write((int)round(servoPos)); }
    }
  }

  // ultrasonic EV bay
  if (now - tUs >= 200) {
    tUs = now;
    digitalWrite(PIN_TRIG, HIGH); delayMicroseconds(10); digitalWrite(PIN_TRIG, LOW);
    unsigned long us = pulseIn(PIN_ECHO, HIGH, 30000);
    float cm = us ? us / 58.0f : 999.0f;
    bool valid = cm >= 2.0f;                       // HC-SR04 minimum range: shorter echoes are noise/wiring
    static unsigned long tDbg = 0;
    if (now - tDbg >= 1000) { tDbg = now; Serial.printf("us=%lu cm=%.1f %s wifi=%d ip=%s host=%s http=%d link=%s\n", us, cm, valid ? "" : "(invalid)",
        WiFi.status(), WiFi.localIP().toString().c_str(), gridqHost.c_str(), lastHttp, !everLinked ? "wait" : linkLost ? "LOST" : "ok"); }
    if (valid) {                                   // noise echoes are skipped, they neither count nor reset the streak
      if (cm >= EV_MIN_CM && cm < EV_THRESHOLD_CM) {
        if (evStreak == 0) { evLo = evHi = cm; }
        evLo = min(evLo, cm); evHi = max(evHi, cm);
        evStreak = (evHi - evLo <= EV_SPREAD_CM) ? evStreak + 1 : 0;   // jumpy readings restart the count
      } else if (cm >= EV_THRESHOLD_CM) { evStreak = 0; evArmed = true; }
    }
    if (evStreak >= EV_CONSECUTIVE && evArmed && now - lastEv >= EV_COOLDOWN_MS) {
      evArmed = false;      // rising edge only: re-arms when the bay clears
      lastEv = now;
      evPending = true;
      Serial.printf("EV arrival (%.1f cm)\n", cm);
    }
  }

  // sensor sample (posted by the net task)
  if (sensType != SENS_NONE && now - tSens >= 1000) {
    tSens = now;
    float v;
    if (readSensor(v)) {
      portENTER_CRITICAL(&mux);
      strlcpy(sensorKind, sensType == SENS_BH1750 ? "lux" : "temp_c", sizeof(sensorKind));
      sensorValue = v;
      portEXIT_CRITICAL(&mux);
      sensorReady = true;
    }
  }

  // LCD
  if (now - tCycle >= 3000) { tCycle = now; l2i++; }
  if (now - tLcd >= 250) {
    tLcd = now;
    if (!everLinked) {
      lcdLine(0, "GRIDQ CLINIC");
      lcdLine(1, WiFi.status() == WL_CONNECTED ? "FINDING GRIDQ..." : "CONNECTING WIFI");
    } else if (linkLost) { lcdLine(0, "LINK LOST"); lcdLine(1, "CLINIC LOCAL ON"); }
    else {
      char a[17], b[17];
      portENTER_CRITICAL(&mux);
      strlcpy(a, l1, 17);
      strlcpy(b, l2[l2i % max(1, l2n)], 17);
      portEXIT_CRITICAL(&mux);
      lcdLine(0, a); lcdLine(1, b);
    }
  }

  // blue LED (GPIO2; the red one is power): solid = linked to GridQ · slow blink = on Wi-Fi, waiting
  // for GridQ · fast blink = no Wi-Fi
  bool wifiUp = WiFi.status() == WL_CONNECTED;
  if (wifiUp && everLinked && !linkLost) digitalWrite(PIN_LED, HIGH);
  else if (now - tLed >= (wifiUp ? 500UL : 100UL)) { tLed = now; digitalWrite(PIN_LED, !digitalRead(PIN_LED)); }
}
