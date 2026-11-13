#include <Wire.h>
#include <stdlib.h>
#include <string.h>

const uint8_t MCP4728_ADDRESS = 0x60;
const uint8_t SWITCH_SENSE_PIN = 2;
const uint8_t LAMP_PWM_PIN = 3;
const uint8_t LAMP_BRIGHTNESS = 64;  // 25% of 255
const uint16_t DAC_MAX = 4095;
const uint16_t GAUGE_A_MIN = 0;
const uint16_t GAUGE_A_MAX = 4095;
const uint16_t GAUGE_B_MIN = 0;
const uint16_t GAUGE_B_MAX = 4095;
const uint16_t STEP = 40;
const unsigned long UPDATE_MS = 10;
const unsigned long SERIAL_TIMEOUT_MS = 5000;
const unsigned long SWITCH_DEBOUNCE_MS = 35;
const unsigned long WIFI_RECOVERY_WINDOW_MS = 2500;

uint16_t currentA = 0;
uint16_t currentB = 0;
uint16_t telemetryA = 0;
uint16_t telemetryB = 0;
uint8_t lampBrightness = LAMP_BRIGHTNESS;
unsigned long lastUpdate = 0;
unsigned long lastSerial = 0;
char rxBuffer[16];
uint8_t rxLength = 0;

enum BootState {
  BOOT_OFF,
  BOOT_LAMP_ON,
  BOOT_SWEEP_UP,
  BOOT_HOLD_FULL,
  BOOT_SWEEP_DOWN,
  BOOT_RUNNING,
};

BootState bootState = BOOT_OFF;
unsigned long bootStateStarted = 0;
bool switchOn = false;
bool lastRawSwitch = false;
unsigned long switchChangedAt = 0;
uint8_t switchOnCount = 0;
unsigned long firstSwitchOnAt = 0;

uint16_t percentToDac(int percent, uint16_t minimum, uint16_t maximum) {
  percent = constrain(percent, 0, 100);
  return minimum + (uint32_t)percent * (maximum - minimum) / 100;
}

uint16_t stepToward(uint16_t current, uint16_t target) {
  if (current < target) return current + min((uint16_t)(target - current), STEP);
  if (current > target) return current - min((uint16_t)(current - target), STEP);
  return current;
}

void writeGauges(uint16_t a, uint16_t b) {
  // MCP4728 fast-write: A, B, C, D input registers; no EEPROM writes.
  Wire.beginTransmission(MCP4728_ADDRESS);
  // Physical gauge wiring is reversed: logical left is MCP4728 B.
  Wire.write((uint8_t)(b >> 8));
  Wire.write((uint8_t)b);
  Wire.write((uint8_t)(a >> 8));
  Wire.write((uint8_t)a);
  Wire.write((uint8_t)0);
  Wire.write((uint8_t)0);
  Wire.write((uint8_t)0);
  Wire.write((uint8_t)0);
  Wire.endTransmission();
}

void configureChannelForVddReference(uint8_t channel) {
  // Multi-write sets this volatile channel configuration immediately.
  // VREF=0 selects VDD rather than the 2.048 V internal reference.
  Wire.beginTransmission(MCP4728_ADDRESS);
  Wire.write((uint8_t)(0x40 | (channel << 1)));
  Wire.write((uint8_t)0);
  Wire.write((uint8_t)0);
  Wire.endTransmission();
}

void parseLine(char *line) {
  char *comma = strchr(line, ',');
  if (!comma) return;

  *comma = '\0';
  if (strcmp(line, "LAMP") == 0) {
    lampBrightness = (uint8_t)constrain(atoi(comma + 1) * 255 / 100, 0, 255);
    return;
  }
  telemetryA = percentToDac(atoi(line), GAUGE_A_MIN, GAUGE_A_MAX);
  telemetryB = percentToDac(atoi(comma + 1), GAUGE_B_MIN, GAUGE_B_MAX);
  lastSerial = millis();
}

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      rxBuffer[rxLength] = '\0';
      parseLine(rxBuffer);
      rxLength = 0;
    } else if (c != '\r') {
      if (rxLength < sizeof(rxBuffer) - 1) rxBuffer[rxLength++] = c;
      else rxLength = 0;
    }
  }
}

void setup() {
  Wire.begin();
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);
  pinMode(SWITCH_SENSE_PIN, INPUT);
  pinMode(LAMP_PWM_PIN, OUTPUT);
  analogWrite(LAMP_PWM_PIN, 0);
  configureChannelForVddReference(0);
  configureChannelForVddReference(1);
  writeGauges(0, 0);
  lastSerial = millis();
  lastRawSwitch = digitalRead(SWITCH_SENSE_PIN) == HIGH;
  switchOn = lastRawSwitch;
}

void loop() {
  readSerial();
  unsigned long now = millis();

  bool rawSwitch = digitalRead(SWITCH_SENSE_PIN) == HIGH;
  if (rawSwitch != lastRawSwitch) {
    lastRawSwitch = rawSwitch;
    switchChangedAt = now;
  }
  if (now - switchChangedAt >= SWITCH_DEBOUNCE_MS && rawSwitch != switchOn) {
    switchOn = rawSwitch;
    if (switchOn) {
      if (now - firstSwitchOnAt > WIFI_RECOVERY_WINDOW_MS) {
        switchOnCount = 0;
        firstSwitchOnAt = now;
      }
      if (++switchOnCount >= 2) {
        Serial.println("WIFI");
        switchOnCount = 0;
        firstSwitchOnAt = 0;
      }
    }
  }
  if (switchOnCount && now - firstSwitchOnAt > WIFI_RECOVERY_WINDOW_MS) {
    switchOnCount = 0;
  }

  if (now - lastSerial > SERIAL_TIMEOUT_MS) {
    telemetryA = 0;
    telemetryB = 0;
  }

  if (now - lastUpdate < UPDATE_MS) return;
  lastUpdate = now;

  if (!switchOn) {
    bootState = BOOT_OFF;
    analogWrite(LAMP_PWM_PIN, 0);
    currentA = 0;
    currentB = 0;
    writeGauges(0, 0);
    return;
  }

  if (bootState == BOOT_OFF) {
    bootState = BOOT_LAMP_ON;
    bootStateStarted = now;
    analogWrite(LAMP_PWM_PIN, lampBrightness);
  }

  analogWrite(LAMP_PWM_PIN, lampBrightness);

  uint16_t targetA = telemetryA;
  uint16_t targetB = telemetryB;

  if (bootState == BOOT_LAMP_ON) {
    targetA = 0;
    targetB = 0;
    if (now - bootStateStarted >= 350) {
      bootState = BOOT_SWEEP_UP;
    }
  } else if (bootState == BOOT_SWEEP_UP) {
    targetA = DAC_MAX;
    targetB = DAC_MAX;
    if (currentA == DAC_MAX && currentB == DAC_MAX) {
      bootState = BOOT_HOLD_FULL;
      bootStateStarted = now;
    }
  } else if (bootState == BOOT_HOLD_FULL) {
    targetA = DAC_MAX;
    targetB = DAC_MAX;
    if (now - bootStateStarted >= 400) {
      bootState = BOOT_SWEEP_DOWN;
    }
  } else if (bootState == BOOT_SWEEP_DOWN) {
    targetA = 0;
    targetB = 0;
    if (currentA == 0 && currentB == 0) {
      bootState = BOOT_RUNNING;
    }
  }

  currentA = stepToward(currentA, targetA);
  currentB = stepToward(currentB, targetB);
  writeGauges(currentA, currentB);
}
