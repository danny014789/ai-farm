#include <DFRobot_SCD4X.h>
#include <Wire.h>

DFRobot_SCD4X SCD4X(&Wire, SCD4X_I2C_ADDR);

// ---- Sensors ----
const int LIGHT_PIN = A0;
const int SOIL_PIN  = A1;
const int WATER_PIN = 9;   // HIGH = water OK, LOW = low

// ---- Relays ----
const int RELAY_LIGHT_PIN  = 8;
const int RELAY_HEATER_PIN = 5;
const int RELAY_WATER_PIN  = 7; // timed
const int RELAY_CIRC_PIN   = 6; // timed
const bool RELAY_ACTIVE_LOW = true;

// ---- Heater overtemp failsafe ----
const float HEATER_TRIP_C  = 40.0f;
const float HEATER_RESET_C = 39.0f;
bool heaterLockout = false;

// ---- SCD41 stale-data watchdog ----
const unsigned long SCD_STALE_MS = 30000;   // 30s without new data => recover
const unsigned long RECOVER_BACKOFF_MS = 15000; // don’t spam recover attempts
unsigned long lastSCDms = 0;
unsigned long nextRecoverAllowedMs = 0;

// ---- Cached data ----
DFRobot_SCD4X::sSensorMeasurement_t latestAir;
bool hasAir = false;
int latestLightRaw = 0;
int latestSoilRaw  = 0;

// ---- Relay states ----
bool lightRelayOn  = false;
bool heaterRelayOn = false;

struct TimedRelay {
  int pin;
  bool isOn;
  unsigned long offAtMs;
};
TimedRelay waterRelay { RELAY_WATER_PIN, false, 0 };
TimedRelay circRelay  { RELAY_CIRC_PIN,  false, 0 };

// ---- Serial command buffer (no String fragmentation) ----
char cmdBuf[64];
uint8_t cmdLen = 0;

// ---------- Low-level ----------
void writeRelayPin(int pin, bool on) {
  if (RELAY_ACTIVE_LOW) digitalWrite(pin, on ? LOW : HIGH);
  else                  digitalWrite(pin, on ? HIGH : LOW);
}

void setLightRelay(bool on) {
  lightRelayOn = on;
  writeRelayPin(RELAY_LIGHT_PIN, on);
}

void setHeaterRelay(bool on) {
  // Never allow ON during lockout
  if (on && heaterLockout) on = false;
  heaterRelayOn = on;
  writeRelayPin(RELAY_HEATER_PIN, on);
}

bool waterOK() {
  return digitalRead(WATER_PIN) == HIGH;
}

void updateAnalogReads() {
  latestLightRaw = analogRead(LIGHT_PIN);
  latestSoilRaw  = analogRead(SOIL_PIN);
}

// ---------- Timed relay ----------
void timedRelayOnFor(TimedRelay &r, unsigned long seconds) {
  r.isOn = true;
  writeRelayPin(r.pin, true);
  unsigned long now = millis();
  r.offAtMs = (seconds == 0) ? now : (now + seconds * 1000UL);
}

void timedRelayOff(TimedRelay &r) {
  r.isOn = false;
  r.offAtMs = 0;
  writeRelayPin(r.pin, false);
}

long secondsRemaining(const TimedRelay &r) {
  if (!r.isOn || r.offAtMs == 0) return 0;
  long diff = (long)(r.offAtMs - millis());
  if (diff <= 0) return 0;
  return (diff + 999) / 1000;
}

void serviceTimedRelays() {
  unsigned long now = millis();
  if (waterRelay.isOn && waterRelay.offAtMs && (long)(now - waterRelay.offAtMs) >= 0) timedRelayOff(waterRelay);
  if (circRelay.isOn  && circRelay.offAtMs  && (long)(now - circRelay.offAtMs)  >= 0) timedRelayOff(circRelay);
}

// ---------- Heater safety ----------
void serviceHeaterSafety() {
  if (!hasAir) return;

  float t = latestAir.temp;

  if (!heaterLockout && t > HEATER_TRIP_C) {
    heaterLockout = true;
    if (heaterRelayOn) setHeaterRelay(false);
    Serial.println("!!! HEATER FAILSAFE TRIPPED (Temp > 40C). Heater forced OFF. !!!");
  }

  if (heaterLockout && t < HEATER_RESET_C) {
    heaterLockout = false;
    Serial.println("Heater failsafe cleared (temp < 39C).");
  }

  if (heaterLockout && heaterRelayOn) setHeaterRelay(false);
}

// ---------- SCD41 recovery ----------
void recoverSCD41() {
  unsigned long now = millis();
  if (now < nextRecoverAllowedMs) return;
  nextRecoverAllowedMs = now + RECOVER_BACKOFF_MS;

  Serial.println("!!! SCD41 stale -> attempting recovery (stop/reinit/start + Wire restart) !!!");

  // Safety: if you rely on temp for heater safety, shut heater off during sensor outage
  setHeaterRelay(false);

  // Restart I2C peripheral (often helps after bus glitches)
  Wire.end();
  delay(50);
  Wire.begin();
  delay(50);

  // Try to restart sensor periodic measurement
  SCD4X.enablePeriodMeasure(SCD4X_STOP_PERIODIC_MEASURE);
  delay(600); // DFRobot notes need >=500ms after stop

  // Reinit from EEPROM settings (DFRobot provides this in their examples)
  // If your library doesn’t have moduleReinit(), comment this out.
  SCD4X.moduleReinit();
  delay(50);

  SCD4X.enablePeriodMeasure(SCD4X_START_PERIODIC_MEASURE);
  delay(50);

  // Don’t immediately declare success; wait for next dataReady->readMeasurement to refresh lastSCDms
}

// ---------- Output ----------
void printHelp() {
  Serial.println("Commands:");
  Serial.println("  r / read             -> CSV: co2,tempC,rh,lightRaw,soilRaw,waterOK,lightOn,heaterOn,heaterLockout,waterOn,circOn,waterRem,circRem");
  Serial.println("  p                    -> pretty print latest data");
  Serial.println("  lon / loff           -> light relay ON/OFF");
  Serial.println("  hon / hoff           -> heater relay ON/OFF (blocked if lockout or sensor stale)");
  Serial.println("  w_on,SEC / w_off     -> water pump ON for SEC / OFF");
  Serial.println("  c_on,SEC / c_off     -> circulation ON for SEC / OFF");
  Serial.println("  t?                   -> remaining time for timed relays");
  Serial.println("  help                 -> this help");
}

void printCSV() {
  updateAnalogReads();

  Serial.print(latestAir.CO2ppm); Serial.print(",");
  Serial.print(latestAir.temp, 2); Serial.print(",");
  Serial.print(latestAir.humidity, 2); Serial.print(",");
  Serial.print(latestLightRaw); Serial.print(",");
  Serial.print(latestSoilRaw); Serial.print(",");
  Serial.print(waterOK() ? 1 : 0); Serial.print(",");
  Serial.print(lightRelayOn ? 1 : 0); Serial.print(",");
  Serial.print(heaterRelayOn ? 1 : 0); Serial.print(",");
  Serial.print(heaterLockout ? 1 : 0); Serial.print(",");
  Serial.print(waterRelay.isOn ? 1 : 0); Serial.print(",");
  Serial.print(circRelay.isOn ? 1 : 0); Serial.print(",");
  Serial.print(secondsRemaining(waterRelay)); Serial.print(",");
  Serial.println(secondsRemaining(circRelay));
}

void printPretty() {
  updateAnalogReads();

  Serial.print("CO2:    "); Serial.print(latestAir.CO2ppm); Serial.println(" ppm");
  Serial.print("Temp:   "); Serial.print(latestAir.temp, 2); Serial.println(" C");
  Serial.print("RH:     "); Serial.print(latestAir.humidity, 2); Serial.println(" %");
  Serial.print("Light:  "); Serial.println(latestLightRaw);
  Serial.print("Soil:   "); Serial.println(latestSoilRaw);
  Serial.print("Water:  "); Serial.println(waterOK() ? "OK (1)" : "LOW (0)");

  Serial.print("Relay Light:  "); Serial.println(lightRelayOn ? "ON" : "OFF");
  Serial.print("Relay Heater: "); Serial.print(heaterRelayOn ? "ON" : "OFF");
  Serial.print("  Lockout: "); Serial.println(heaterLockout ? "YES" : "no");

  Serial.print("Relay Water:  "); Serial.print(waterRelay.isOn ? "ON" : "OFF");
  Serial.print("  remaining(s): "); Serial.println(secondsRemaining(waterRelay));

  Serial.print("Relay Circ:   "); Serial.print(circRelay.isOn ? "ON" : "OFF");
  Serial.print("  remaining(s): "); Serial.println(secondsRemaining(circRelay));

  Serial.print("SCD last update(ms ago): ");
  Serial.println(hasAir ? (long)(millis() - lastSCDms) : -1);
}

// ---------- Command handling ----------
unsigned long parseSeconds(const char* s) {
  // accepts "w_on,60" or "w_on 60"
  const char* p = strchr(s, ',');
  if (!p) p = strchr(s, ' ');
  if (!p) return 0;
  return (unsigned long)atoi(p + 1);
}

void handleCommand(const char* in) {
  // lower-case copy for comparisons
  char tmp[64];
  strncpy(tmp, in, sizeof(tmp));
  tmp[sizeof(tmp)-1] = 0;
  for (char* p = tmp; *p; ++p) *p = tolower(*p);

  if (strcmp(tmp, "r") == 0 || strcmp(tmp, "read") == 0) {
    if (!hasAir) { Serial.println("No SCD41 data yet (wait ~5s after start)."); return; }
    printCSV();
    return;
  }

  if (strcmp(tmp, "p") == 0) {
    if (!hasAir) { Serial.println("No SCD41 data yet (wait ~5s after start)."); return; }
    printPretty();
    return;
  }

  if (strcmp(tmp, "lon") == 0 || strcmp(tmp, "light on") == 0) {
    setLightRelay(true); Serial.println("Light relay ON"); return;
  }
  if (strcmp(tmp, "loff") == 0 || strcmp(tmp, "light off") == 0) {
    setLightRelay(false); Serial.println("Light relay OFF"); return;
  }

  if (strcmp(tmp, "hon") == 0 || strcmp(tmp, "heater on") == 0) {
    // block heater if stale sensor
    if (!hasAir || (millis() - lastSCDms) > SCD_STALE_MS) {
      Serial.println("Heater ON blocked: SCD41 data stale/unavailable.");
      setHeaterRelay(false);
      return;
    }
    if (heaterLockout) {
      Serial.println("Heater ON blocked: overtemp lockout active.");
      return;
    }
    setHeaterRelay(true); Serial.println("Heater relay ON"); return;
  }
  if (strcmp(tmp, "hoff") == 0 || strcmp(tmp, "heater off") == 0) {
    setHeaterRelay(false); Serial.println("Heater relay OFF"); return;
  }

  if (strncmp(tmp, "w_on", 4) == 0) {
    unsigned long sec = parseSeconds(in);
    timedRelayOnFor(waterRelay, sec);
    Serial.print("Water pump ON for "); Serial.print(sec); Serial.println(" s");
    return;
  }
  if (strcmp(tmp, "w_off") == 0) { timedRelayOff(waterRelay); Serial.println("Water pump OFF"); return; }

  if (strncmp(tmp, "c_on", 4) == 0) {
    unsigned long sec = parseSeconds(in);
    timedRelayOnFor(circRelay, sec);
    Serial.print("Circulation ON for "); Serial.print(sec); Serial.println(" s");
    return;
  }
  if (strcmp(tmp, "c_off") == 0) { timedRelayOff(circRelay); Serial.println("Circulation OFF"); return; }

  if (strcmp(tmp, "t?") == 0) {
    Serial.print("Water remaining(s): "); Serial.println(secondsRemaining(waterRelay));
    Serial.print("Circ  remaining(s): "); Serial.println(secondsRemaining(circRelay));
    return;
  }

  if (strcmp(tmp, "help") == 0 || strcmp(tmp, "h") == 0) { printHelp(); return; }

  Serial.println("Unknown command. Type 'help'.");
}

void pollSerialCommands() {
  while (Serial.available() > 0) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (cmdLen > 0) {
        cmdBuf[cmdLen] = 0;
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    } else {
      if (cmdLen < sizeof(cmdBuf) - 1) {
        cmdBuf[cmdLen++] = ch;
      } else {
        // overflow -> reset buffer
        cmdLen = 0;
      }
    }
  }
}

// ---------- Setup / Loop ----------
void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(10); }

  pinMode(LIGHT_PIN, INPUT);
  pinMode(SOIL_PIN, INPUT);
  pinMode(WATER_PIN, INPUT_PULLUP); // change to INPUT if your module drives high/low strongly

  pinMode(RELAY_LIGHT_PIN, OUTPUT);
  pinMode(RELAY_HEATER_PIN, OUTPUT);
  pinMode(RELAY_WATER_PIN, OUTPUT);
  pinMode(RELAY_CIRC_PIN, OUTPUT);

  setLightRelay(false);
  setHeaterRelay(false);
  timedRelayOff(waterRelay);
  timedRelayOff(circRelay);

  while (!SCD4X.begin()) {
    Serial.println("Communication with SCD41 failed, please check connection");
    delay(3000);
  }
  Serial.println("SCD41 begin ok!");

  SCD4X.enablePeriodMeasure(SCD4X_STOP_PERIODIC_MEASURE);
  delay(600);
  SCD4X.enablePeriodMeasure(SCD4X_START_PERIODIC_MEASURE);

  printHelp();
}

void loop() {
  serviceTimedRelays();

  // Try to read new SCD41 data when ready
  if (SCD4X.getDataReadyStatus()) {
    SCD4X.readMeasurement(&latestAir);
    hasAir = true;
    lastSCDms = millis();
  }

  // If sensor stale, attempt recovery
  if (hasAir && (millis() - lastSCDms) > SCD_STALE_MS) {
    recoverSCD41();
  }

  // Heater safety always enforced (includes overtemp lockout)
  serviceHeaterSafety();

  pollSerialCommands();
  delay(5);
}
