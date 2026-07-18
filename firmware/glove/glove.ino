// Glove firmware — WiFi + MPU-9250/6500/9255 IMU (plan.md §3.2 H5, §4.4).
//
// Reads the IMU over I2C (SDA=21, SCL=22, addr 0x68), runs a complementary
// filter, and broadcasts JSON over UDP to port 5005 at 50 Hz:
//   {"v":1,"seq":123,"t":45678,"roll":1.2,"pitch":-3.4,"yaw":10.5}
// roll/pitch in degrees relative to the calibrated neutral pose; yaw is
// gyro-integrated and RELATIVE ONLY (it drifts — FR-4). Gyro bias is
// calibrated during the first ~2 s after power-up: KEEP THE GLOVE STILL
// AT BOOT.
//
// Re-zero (FR-4): listens on UDP :5006 for a "CAL" packet (press 'c' in
// hub/glove_listen.py). Counts down 5 s — get the hand into neutral and
// hold still — then re-measures gyro bias and adopts the current pose as
// the new zero. Streaming pauses ~2 s during the bias measurement.
//
// Serial (115200): prints WHO_AM_I, calibration, IP, and 2 s status lines.

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include "wifi_secrets.h"

const uint16_t TELEMETRY_PORT = 5005;
const uint16_t COMMAND_PORT = 5006;
const uint32_t SEND_INTERVAL_MS = 20;   // 50 Hz
const uint32_t CAL_DELAY_MS = 5000;     // grace period after 'c' is pressed

// MPU-9250/6500 registers (shared register map)
const uint8_t MPU_ADDR = 0x68;      // 0x69 if AD0 is tied high
const uint8_t REG_PWR_MGMT_1 = 0x6B;
const uint8_t REG_SMPLRT_DIV = 0x19;
const uint8_t REG_CONFIG = 0x1A;
const uint8_t REG_GYRO_CONFIG = 0x1B;
const uint8_t REG_ACCEL_CONFIG = 0x1C;
const uint8_t REG_ACCEL_XOUT_H = 0x3B;
const uint8_t REG_WHO_AM_I = 0x75;

const float ACCEL_SCALE = 4.0f / 32768.0f;    // ±4 g
const float GYRO_SCALE = 500.0f / 32768.0f;   // ±500 dps
const float FILTER_ALPHA = 0.98f;             // gyro weight in complementary

WiFiUDP udp;
WiFiUDP cmdUdp;
uint32_t seq = 0;
uint32_t lastSend = 0;
uint32_t lastStatus = 0;
uint32_t lastSampleUs = 0;
float roll = 0, pitch = 0, yaw = 0;
float rollNeutral = 0, pitchNeutral = 0;
float gyroBias[3] = {0, 0, 0};
bool imuOk = false;
bool calPending = false;
uint32_t calDueAt = 0;

void writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

uint8_t readReg(uint8_t reg) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0xFF;
}

// Reads accel xyz [g] and gyro xyz [dps]; returns false on I2C failure.
bool readImu(float accel[3], float gyro[3]) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT_H);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)14) != 14) return false;
  int16_t raw[7];  // ax ay az temp gx gy gz
  for (int i = 0; i < 7; i++) {
    raw[i] = (Wire.read() << 8) | Wire.read();
  }
  for (int i = 0; i < 3; i++) {
    accel[i] = raw[i] * ACCEL_SCALE;
    gyro[i] = raw[i + 4] * GYRO_SCALE - gyroBias[i];
  }
  return true;
}

void calibrateGyro() {
  Serial.print("Calibrating gyro, keep glove still");
  const int N = 400;
  double sum[3] = {0, 0, 0};
  float accel[3], gyro[3];
  float saved[3] = {gyroBias[0], gyroBias[1], gyroBias[2]};
  gyroBias[0] = gyroBias[1] = gyroBias[2] = 0;
  for (int i = 0; i < N; i++) {
    if (readImu(accel, gyro)) {
      for (int j = 0; j < 3; j++) sum[j] += gyro[j];
    }
    if (i % 100 == 0) Serial.print(".");
    delay(4);
  }
  for (int j = 0; j < 3; j++) gyroBias[j] = sum[j] / N;
  Serial.printf(" done (bias %.2f %.2f %.2f dps)\n",
                gyroBias[0], gyroBias[1], gyroBias[2]);
  (void)saved;
}

void setup() {
  Serial.begin(115200);

  Wire.begin(21, 22, 400000);
  uint8_t who = readReg(REG_WHO_AM_I);
  // 0x71=MPU9250, 0x73=MPU9255, 0x70=MPU6500
  Serial.printf("WHO_AM_I=0x%02X\n", who);
  imuOk = (who == 0x70 || who == 0x71 || who == 0x73 || who == 0x68);
  if (imuOk) {
    writeReg(REG_PWR_MGMT_1, 0x01);   // wake, clock from gyro PLL
    delay(50);
    writeReg(REG_CONFIG, 0x03);       // DLPF ~41 Hz
    writeReg(REG_SMPLRT_DIV, 0x04);   // 200 Hz internal rate
    writeReg(REG_GYRO_CONFIG, 0x08);  // ±500 dps
    writeReg(REG_ACCEL_CONFIG, 0x08); // ±4 g
    delay(50);
    calibrateGyro();
  } else {
    Serial.println("IMU NOT FOUND — check SDA=21 SCL=22 VCC GND wiring");
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("Joining %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.printf("\nGLOVE_IP %s\n", WiFi.localIP().toString().c_str());
  MDNS.begin("glove");
  cmdUdp.begin(COMMAND_PORT);
  lastSampleUs = micros();
}

void checkCommands() {
  int len = cmdUdp.parsePacket();
  if (len <= 0) return;
  char buf[16] = {0};
  cmdUdp.read(buf, sizeof(buf) - 1);
  if (strncmp(buf, "CAL", 3) == 0 && !calPending) {
    calPending = true;
    calDueAt = millis() + CAL_DELAY_MS;
    Serial.printf("CAL received: hold neutral pose, zeroing in %lu s\n",
                  (unsigned long)(CAL_DELAY_MS / 1000));
  }
}

void runCalibration() {
  calPending = false;
  if (imuOk) calibrateGyro();  // ~2 s, keep still; streaming pauses
  rollNeutral = roll;
  pitchNeutral = pitch;
  yaw = 0;
  lastSampleUs = micros();  // don't integrate across the pause
  Serial.printf("re-zeroed: neutral roll=%.1f pitch=%.1f\n",
                rollNeutral, pitchNeutral);
}

void updateFilter() {
  float accel[3], gyro[3];
  if (!readImu(accel, gyro)) return;
  uint32_t nowUs = micros();
  float dt = (nowUs - lastSampleUs) * 1e-6f;
  lastSampleUs = nowUs;
  if (dt <= 0 || dt > 0.5f) return;  // clock hiccup, skip

  // Accel-derived attitude (valid when not accelerating hard)
  float accRoll = atan2f(accel[1], accel[2]) * RAD_TO_DEG;
  float accPitch = atan2f(-accel[0],
                          sqrtf(accel[1] * accel[1] + accel[2] * accel[2])) *
                   RAD_TO_DEG;

  // Complementary: integrate gyro, bleed toward accel reference
  roll = FILTER_ALPHA * (roll + gyro[0] * dt) + (1 - FILTER_ALPHA) * accRoll;
  pitch = FILTER_ALPHA * (pitch + gyro[1] * dt) + (1 - FILTER_ALPHA) * accPitch;
  yaw += gyro[2] * dt;  // relative only, drifts
}

void loop() {
  uint32_t now = millis();

  if (imuOk) updateFilter();
  checkCommands();
  if (calPending && (int32_t)(now - calDueAt) >= 0) runCalibration();

  if (WiFi.status() == WL_CONNECTED && now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    char buf[160];
    int n = snprintf(buf, sizeof(buf),
                     "{\"v\":1,\"seq\":%lu,\"t\":%lu,"
                     "\"roll\":%.1f,\"pitch\":%.1f,\"yaw\":%.1f,\"cal\":%d}",
                     (unsigned long)seq++, (unsigned long)now,
                     roll - rollNeutral, pitch - pitchNeutral, yaw,
                     calPending ? 1 : 0);
    udp.beginPacket(IPAddress(255, 255, 255, 255), TELEMETRY_PORT);
    udp.write((const uint8_t *)buf, n);
    udp.endPacket();
  }

  if (now - lastStatus >= 2000) {
    lastStatus = now;
    Serial.printf("status wifi=%d rssi=%d imu=%d rpy=%.1f,%.1f,%.1f\n",
                  WiFi.status() == WL_CONNECTED, WiFi.RSSI(), imuOk,
                  roll, pitch, yaw);
  }
}
