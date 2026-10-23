// GridQ MCU safety supervisor (STM32U585 on the Arduino UNO Q). Spec §9.1-9.3, §10.1.
//
// The MCU is the authority for outputs. It trips the grid relay locally from the
// grid-cable input (no Linux involved), and falls back to the safe load mask when
// no NEW heartbeat value has arrived from the brain for HB_TIMEOUT_MS.
//
// Bridge RPC (string payloads):
//   hb("1532")                                   -> "ok"
//   set_outputs("mask,safe_mask,grid_open_cmd,cols13,led_state") -> "ok"
//   get_status()  -> "mode,applied_mask,cable_present,relay_energised,uptime_ms,last_hb"
//
// No blocking delay() in loop(): buzzer, matrix blink and debounce run on millis().

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>

// ---------------------------------------------------------------- config (mirrors config/hardware.yaml)
const int RELAY_PIN = 2;
const int CABLE_PIN = 3;
const int BUZZER_PIN = 4;
const bool RELAY_ACTIVE_LOW = true;     // set false if the BC547 inverter stage is used (§2.1)
// 5 V active-LOW relay modules can't be released by a 3.3 V "HIGH" (1.7 V stays across the opto LED,
// so the relay never drops out). Open-drain drive fixes it: OFF = pin floats (the module pulls IN to its
// own 5 V), ON = pin sinks to GND. D2 is PB3, which is 5 V tolerant. Fail-safe: a reset/unpowered MCU
// floats the pin, so the relay releases and the site stays on grid (COM-NC).
const bool RELAY_OPEN_DRAIN = true;
const bool BUZZER_PASSIVE = false;      // true -> drive with tone()
const int BUZZER_HZ = 2700;

const unsigned long HB_TIMEOUT_MS = 3000;
const unsigned long RECLOSE_DELAY_MS = 5000;
const unsigned long DEBOUNCE_MS = 50;
const unsigned long MATRIX_BOOT_HOLDOFF_MS = 30000;  // let the Linux boot logo finish (AGENTS.md §6)

const int RELAY_ON = RELAY_ACTIVE_LOW ? LOW : HIGH;
const int RELAY_OFF = RELAY_ACTIVE_LOW ? HIGH : LOW;


// ---------------------------------------------------------------- state
enum Mode { MODE_NORMAL, MODE_SAFE };
volatile Mode mode = MODE_SAFE;          // start in SAFE until the first heartbeat
String lastHb = "";
unsigned long lastHbMs = 0;
bool hbPulse = false;
bool everHb = false;

int planMask = 127;
int safeMask = 3;                        // clinic + pump
int gridOpenCmd = 0;
uint8_t cols[13] = {0};
char ledState = 'N';
bool everOutputs = false;

bool cablePresent = true;
int cableRaw = LOW;
unsigned long cableChangeMs = 0;
bool relayEnergised = false;
unsigned long recloseOkSince = 0;

bool pendingSafeEntryTone = false;
bool pendingSafeExitChirp = false;
bool pendingRelayChirp = false;

Arduino_LED_Matrix matrix;
uint8_t frame[8 * 13];
uint8_t lastFrame[8 * 13];

// ---------------------------------------------------------------- buzzer (non-blocking pattern player)
struct Seg { unsigned int on, off; };
Seg pattern[4];
int patLen = 0, patIdx = 0;
bool patOn = false;
unsigned long patT = 0;

void buzz(bool on) {
  if (BUZZER_PASSIVE) { if (on) tone(BUZZER_PIN, BUZZER_HZ); else noTone(BUZZER_PIN); }
  else digitalWrite(BUZZER_PIN, on ? HIGH : LOW);
}

void play(const Seg* segs, int n) {   // a newer, longer alarm replaces a chirp in progress
  for (int i = 0; i < n && i < 4; i++) pattern[i] = segs[i];
  patLen = n; patIdx = 0; patOn = true; patT = millis();
  buzz(true);
}

void buzzerTick(unsigned long now) {
  if (patLen == 0) return;
  Seg s = pattern[patIdx];
  if (patOn && now - patT >= s.on) { buzz(false); patOn = false; patT = now; }
  else if (!patOn && now - patT >= s.off) {
    patIdx++;
    if (patIdx >= patLen) { patLen = 0; return; }
    patOn = true; patT = now; buzz(true);
  }
}

const Seg TONE_SAFE_ENTRY[] = {{800, 0}};
const Seg TONE_SAFE_EXIT[] = {{80, 80}, {80, 0}};
const Seg TONE_RELAY[] = {{150, 0}};

// ---------------------------------------------------------------- LEDs (active-LOW RGB)
void rgb(int r, int g, int b, int pr, int pg, int pb) {
  digitalWrite(pr, r ? LOW : HIGH);
  digitalWrite(pg, g ? LOW : HIGH);
  digitalWrite(pb, b ? LOW : HIGH);
}

// ---------------------------------------------------------------- Bridge handlers (run via provide_safe in the loop thread)
String hb(String v) {
  if (v.length() > 0 && v != lastHb) {   // only a NEW value feeds the watchdog
    lastHb = v;
    lastHbMs = millis();
    hbPulse = !hbPulse;
    everHb = true;
    if (mode == MODE_SAFE) { mode = MODE_NORMAL; pendingSafeExitChirp = true; }
  }
  return String("ok");
}

int nextField(const String& s, int start, String& out) {
  int c = s.indexOf(',', start);
  if (c < 0) { out = s.substring(start); return s.length(); }
  out = s.substring(start, c);
  return c + 1;
}

String set_outputs(String csv) {
  String f;
  int i = 0;
  i = nextField(csv, i, f); int m = f.toInt();
  i = nextField(csv, i, f); int sm = f.toInt();
  i = nextField(csv, i, f); int g = f.toInt();
  i = nextField(csv, i, f); String c = f;
  i = nextField(csv, i, f); String l = f;
  if (c.length() != 13) return String("err:cols");
  planMask = m & 127;
  safeMask = sm & 127;
  gridOpenCmd = g ? 1 : 0;
  for (int k = 0; k < 13; k++) { char ch = c[k]; cols[k] = (ch >= '0' && ch <= '8') ? ch - '0' : 0; }
  ledState = l.length() ? l[0] : 'N';
  everOutputs = true;
  return String("ok");
}

String get_status() {
  int applied = (mode == MODE_NORMAL) ? planMask : safeMask;
  String s = String(mode == MODE_NORMAL ? "N" : "S");
  s += ","; s += String(applied);
  s += ","; s += String(cablePresent ? 1 : 0);
  s += ","; s += String(relayEnergised ? 1 : 0);
  s += ","; s += String(millis());
  s += ","; s += (lastHb.length() ? lastHb : String("0"));
  return s;
}

// ---------------------------------------------------------------- matrix
// "SAFE" in a 3x5 font, 12 columns, letters alternate brightness so they read apart.
const char* GLYPH[5] = {
  "SSSAAAFFFEEE",
  "S..A.AF..E..",
  "SSSAAAFF.EE.",
  "..SA.AF..E..",
  "SSSA.AF..EEE",
};

void setPx(int r, int c, uint8_t v) { if (r >= 0 && r < 8 && c >= 0 && c < 13) frame[r * 13 + c] = v; }

void bar(int c, int h, uint8_t v) {   // bar grows from the bottom row
  for (int r = 0; r < 8; r++) setPx(r, c, (7 - r) < h ? v : 0);
}

void drawFrame(unsigned long now) {
  memset(frame, 0, sizeof(frame));
  bool showGlyph = (mode == MODE_SAFE) && ((now / 1000) % 2 == 0);
  if (showGlyph) {
    for (int r = 0; r < 5; r++)
      for (int c = 0; c < 12; c++)
        if (GLYPH[r][c] != '.') setPx(r + 1, c, (c / 3) % 2 == 0 ? 7 : 3);
  } else if (mode == MODE_SAFE) {
    for (int b = 0; b < 7; b++) bar(b, (safeMask >> b & 1) ? 8 : 0, 7);
  } else {
    // loads (columns 0-6, column n = mask bit n) bright; sources (8-11) dimmer so the two groups read apart.
    // A load the plan holds back (mask bit off) flashes a dim ghost bar; a trimmed feeder's bar pulses.
    bool blinkOn = (now / 300) % 2 == 0;
    for (int c = 0; c < 7; c++) {
      bool cut = !(planMask >> c & 1);
      bool feeder = c == 0 || c == 2 || c == 3 || c == 4;
      if (cut) bar(c, 8, blinkOn ? 2 : 0);
      else if (feeder && cols[c] > 0 && cols[c] < 8) bar(c, cols[c], blinkOn ? 7 : 2);
      else bar(c, cols[c], 7);
    }
    for (int c = 8; c < 12; c++) bar(c, cols[c], 3);
    bar(12, hbPulse ? 8 : 0, 4);
  }
  if (memcmp(frame, lastFrame, sizeof(frame)) != 0) {
    matrix.draw(frame);
    memcpy(lastFrame, frame, sizeof(frame));
  }
}

// ---------------------------------------------------------------- relay drive
void relayWrite(bool energise) {
  if (RELAY_OPEN_DRAIN && RELAY_ACTIVE_LOW) {
    if (energise) { digitalWrite(RELAY_PIN, LOW); pinMode(RELAY_PIN, OUTPUT); digitalWrite(RELAY_PIN, LOW); }
    else { pinMode(RELAY_PIN, INPUT); }        // high-Z: module pull-up releases the relay
  } else {
    digitalWrite(RELAY_PIN, energise ? RELAY_ON : RELAY_OFF);
  }
}

// ---------------------------------------------------------------- setup / loop
void setup() {
  // Relay OFF level BEFORE pinMode(OUTPUT): no glitch that islands the site at boot.
  if (RELAY_OPEN_DRAIN && RELAY_ACTIVE_LOW) {
    pinMode(RELAY_PIN, INPUT);                 // released from the first instruction
  } else {
    digitalWrite(RELAY_PIN, RELAY_OFF);
    pinMode(RELAY_PIN, OUTPUT);
    digitalWrite(RELAY_PIN, RELAY_OFF);
  }
  pinMode(CABLE_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  buzz(false);
  pinMode(LED3_R, OUTPUT); pinMode(LED3_G, OUTPUT); pinMode(LED3_B, OUTPUT);
  pinMode(LED4_R, OUTPUT); pinMode(LED4_G, OUTPUT); pinMode(LED4_B, OUTPUT);
  rgb(1, 0, 0, LED3_R, LED3_G, LED3_B);    // SAFE at boot
  rgb(0, 1, 0, LED4_R, LED4_G, LED4_B);
  cableRaw = digitalRead(CABLE_PIN);
  cablePresent = (cableRaw == LOW);
  cableChangeMs = millis();
  memset(lastFrame, 255, sizeof(lastFrame));

  matrix.begin();
  matrix.setGrayscaleBits(3);
  matrix.clear();

  Bridge.begin();
  Bridge.provide_safe("hb", hb);
  Bridge.provide_safe("set_outputs", set_outputs);
  Bridge.provide_safe("get_status", get_status);
}

void loop() {
  unsigned long now = millis();

  // 1. debounce the grid cable (LOW = present)
  int raw = digitalRead(CABLE_PIN);
  if (raw != cableRaw) { cableRaw = raw; cableChangeMs = now; }
  if (now - cableChangeMs >= DEBOUNCE_MS) cablePresent = (cableRaw == LOW);

  // 2. heartbeat watchdog
  if (mode == MODE_NORMAL && now - lastHbMs > HB_TIMEOUT_MS) { mode = MODE_SAFE; pendingSafeEntryTone = true; }

  // 3. local grid trip: island immediately, reclose only after RECLOSE_DELAY_MS of good conditions
  bool wantOpen = !cablePresent || gridOpenCmd == 1;
  if (wantOpen) {
    recloseOkSince = 0;
    if (!relayEnergised) { relayEnergised = true; relayWrite(true); pendingRelayChirp = true; }
  } else if (relayEnergised) {
    if (recloseOkSince == 0) recloseOkSince = now ? now : 1;
    if (now - recloseOkSince >= RECLOSE_DELAY_MS) {
      relayEnergised = false; relayWrite(false); pendingRelayChirp = true; recloseOkSince = 0;
    }
  }

  // 4. buzzer: SAFE entry beats a chirp
  if (pendingSafeEntryTone) { play(TONE_SAFE_ENTRY, 1); pendingSafeEntryTone = false; pendingSafeExitChirp = false; }
  else if (pendingSafeExitChirp) { play(TONE_SAFE_EXIT, 2); pendingSafeExitChirp = false; }
  if (pendingRelayChirp) { if (patLen == 0) play(TONE_RELAY, 1); pendingRelayChirp = false; }
  buzzerTick(now);

  // 5. status LEDs: LED3 mode, LED4 grid
  if (mode == MODE_SAFE) rgb(1, 0, 0, LED3_R, LED3_G, LED3_B);
  else if (ledState == 'C' || relayEnergised) rgb(1, 1, 0, LED3_R, LED3_G, LED3_B);
  else rgb(0, 1, 0, LED3_R, LED3_G, LED3_B);
  if (relayEnergised) rgb(0, 0, 1, LED4_R, LED4_G, LED4_B);
  else rgb(0, 1, 0, LED4_R, LED4_G, LED4_B);

  // 6. matrix (after the boot logo, or once Linux has talked to us)
  static unsigned long lastDraw = 0;
  if ((everOutputs || everHb || now > MATRIX_BOOT_HOLDOFF_MS) && now - lastDraw >= 50) {
    lastDraw = now;
    drawFrame(now);
  }
}
