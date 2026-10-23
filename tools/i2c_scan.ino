// ESP32 I2C scanner (SDA 21, SCL 22). Open Serial Monitor at 115200.
// 0x27/0x3F LCD backpack · 0x23/0x5C BH1750 · 0x76/0x77 BMP/BME280 · 0x68 MPU6050
#include <Wire.h>
void setup() { Serial.begin(115200); Wire.begin(21, 22); }
void loop() {
  Serial.print("I2C:");
  for (uint8_t a = 1; a < 127; a++) { Wire.beginTransmission(a); if (Wire.endTransmission() == 0) Serial.printf(" 0x%02X", a); }
  Serial.println();
  delay(2000);
}
