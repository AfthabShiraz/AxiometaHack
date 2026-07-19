// Wireless rover controller — ESP32-S3 mini (Axiometa) replacing the
// Arduino + bridge (plan.md §4.3 protocol, §5.4 safety, open question 2).
//
// The venue AP blocks fresh laptop->client UDP flows (ICMP passes!), so
// the rover ANNOUNCES ITSELF first: every 2 s without a live commander it
// resolves the hub's mDNS hostname and sends "HELLO" to hub port 5011.
// That outbound packet opens the flow; the hub then sends commands back
// to the announcement's source address. Same trick as the glove's SUB.
//
// Joins WiFi (mDNS "rover.local") and listens on UDP :5010 for the same
// newline-terminated ASCII protocol the Arduino spoke:
//   M,<left>,<right>   -255..255 per side  -> ack "A"
//   S                  immediate stop      -> ack "A"
// Replies go to the most recent commander:
//   A per valid command, H heartbeat every 500 ms, W once on watchdog trip.
//
// Safety identical to the Arduino firmware: motors zeroed at boot
// (FR-18); if no valid command arrives for 300 ms, motors stop (FR-15).
// WiFi drop, hub death, or router loss all look like silence -> stop.
//
// Board is an Axiometa Genesis Mini (ESP32-S3-Mini-1-N4R2). GPIOs are
// exposed via AX22 ports; the port-specific pins are: Port1 = IO4, IO3,
// IO2; Port2 = IO7, IO6, IO5 (positions 3,4,5 of each port; position
// 1 = GND, 2 = 3V3). GPIO 8 is NOT broken out. User button = IO45,
// activity LED = IO37, NeoPixel = IO21.
//
// Wiring to L298N (3.3 V logic is enough for its ~2.3 V HIGH threshold):
//   Port 1: IO4 = ENA, IO3 = IN1, IO2 = IN2, position-1 hole = GND
//   Port 2: IO7 = IN3, IO6 = IN4, IO5 = ENB

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <esp_task_wdt.h>
#include "wifi_secrets.h"

const int ENA = 4;   // left speed (PWM)   — Port 1, IO4
const int IN1 = 3;   // left dir           — Port 1, IO3
const int IN2 = 2;   // left dir           — Port 1, IO2
const int IN3 = 7;   // right dir          — Port 2, IO7
const int IN4 = 6;   // right dir          — Port 2, IO6
const int ENB = 5;   // right speed (PWM)  — Port 2, IO5

const uint16_t CMD_PORT = 5006;  // 5010 blocked by venue AP; 5006 proven (glove)
const char *HUB_HOSTNAME = "MacBooks-MacBook-Air";  // hub's mDNS name
const uint16_t HUB_PORT = 5011;
const unsigned long HELLO_INTERVAL_MS = 2000;
const unsigned long COMMANDER_FRESH_MS = 3000;
const unsigned long WATCHDOG_MS = 300;
const unsigned long HEARTBEAT_MS = 500;
const uint32_t PWM_FREQ = 500;  // L298N is slow; 5 kHz starved it (hum, no spin)
const uint8_t PWM_RES = 8;  // 0..255, same scale as the Arduino

const int SERVO_PAN = 15;   // Port 3, IO15 — left/right
const int SERVO_TILT = 16;  // Port 3, IO16 — up/down
const uint8_t SERVO_RES = 14;      // S3 LEDC max is 14-bit; 16 fails silently
const int PAN_MIN = 10, PAN_MAX = 170;    // mechanical clamps (FR-7)
const int TILT_MIN = 30, TILT_MAX = 150;

WiFiUDP rxUdp;  // receive-only: commands in (glove firmware pattern —
WiFiUDP txUdp;  // transmit-only: HELLO/acks/heartbeats out
IPAddress commanderIp;
uint16_t commanderPort = 0;
unsigned long lastCommandMs = 0;
unsigned long lastPacketMs = 0;   // any inbound traffic, valid or not
unsigned long lastHeartbeatMs = 0;
unsigned long lastHelloMs = 0;
IPAddress hubIp;
bool watchdogTripped = false;
bool moving = false;

// Left pair runs mirrored on this chassis too: polarity flipped.
void setLeft(int speed) {
  digitalWrite(IN1, speed >= 0 ? LOW : HIGH);
  digitalWrite(IN2, speed >= 0 ? HIGH : LOW);
  ledcWrite(ENA, abs(speed));
}

// Right side wired reversed relative to left (mirrored pairs): flipped.
void setRight(int speed) {
  digitalWrite(IN3, speed >= 0 ? LOW : HIGH);
  digitalWrite(IN4, speed >= 0 ? HIGH : LOW);
  ledcWrite(ENB, abs(speed));
}

void allStop() {
  setLeft(0);
  setRight(0);
  moving = false;
}

void setServo(int pin, int deg) {
  // 0..180 deg -> 500..2500 us pulse at 50 Hz
  uint32_t us = 500 + (uint32_t)deg * 2000 / 180;
  ledcWrite(pin, (uint32_t)((uint64_t)us * ((1UL << SERVO_RES) - 1) / 20000));
}

int clamp255(long v) {
  if (v > 255) return 255;
  if (v < -255) return -255;
  return (int)v;
}

void reply(const char *s) {
  if (commanderPort == 0) return;
  txUdp.beginPacket(commanderIp, commanderPort);
  txUdp.write((const uint8_t *)s, strlen(s));
  txUdp.write((const uint8_t *)"\n", 1);
  txUdp.endPacket();
}

bool handleLine(char *line) {
  if (line[0] == 'S' && (line[1] == '\0')) {
    allStop();
    return true;
  }
  if (line[0] == 'P' && line[1] == ',') {
    char *rest;
    long pan = strtol(line + 2, &rest, 10);
    if (*rest != ',') return false;
    char *end;
    long tilt = strtol(rest + 1, &end, 10);
    if (*end != '\0' && *end != '\r' && *end != '\n') return false;
    setServo(SERVO_PAN, constrain((int)pan, PAN_MIN, PAN_MAX));
    setServo(SERVO_TILT, constrain((int)tilt, TILT_MIN, TILT_MAX));
    return true;
  }
  if (line[0] == 'M' && line[1] == ',') {
    char *rest;
    long left = strtol(line + 2, &rest, 10);
    if (*rest != ',') return false;
    char *end;
    long right = strtol(rest + 1, &end, 10);
    if (*end != '\0' && *end != '\r' && *end != '\n') return false;
    setLeft(clamp255(left));
    setRight(clamp255(right));
    moving = (left != 0 || right != 0);
    return true;
  }
  return false;
}

void setup() {
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  ledcAttach(ENA, PWM_FREQ, PWM_RES);
  ledcAttach(ENB, PWM_FREQ, PWM_RES);
  allStop();  // FR-18
  ledcAttach(SERVO_PAN, 50, SERVO_RES);
  ledcAttach(SERVO_TILT, 50, SERVO_RES);
  setServo(SERVO_PAN, 90);   // camera centered until head tracking exists
  setServo(SERVO_TILT, 90);

  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  // Native-USB serial: never block on writes when no monitor is reading
  // (a blocked print would freeze the control loop — motors stay safe via
  // the command watchdog, but the robot goes dead until reset).
  Serial.setTxTimeoutMs(0);
#endif

  // Hang protection: if loop() ever stalls for 5 s, reboot. The hub sees
  // silence, the command watchdog has already stopped the motors, and the
  // board rejoins WiFi by itself — self-healing without a button press.
  esp_task_wdt_config_t wdt_cfg = {
      .timeout_ms = 5000, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt_cfg);
  esp_task_wdt_add(NULL);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // control latency matters
  WiFi.setAutoReconnect(true);
  // DHCP on the phone hotspot (the venue static-IP workaround is gone);
  // the HELLO announcement tells the hub our address either way.
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("Joining %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
    esp_task_wdt_reset();  // joining may take longer than the WDT window
  }
  Serial.printf("\nROVER_IP %s\n", WiFi.localIP().toString().c_str());
  MDNS.begin("rover");
  rxUdp.begin(CMD_PORT);
  lastCommandMs = millis();
}

// Announce to the hub so the AP opens the return path for its commands.
void sendHello() {
  if ((uint32_t)hubIp == 0) {
    hubIp = MDNS.queryHost(HUB_HOSTNAME, 1500);
    if ((uint32_t)hubIp == 0) return;  // hub offline; retry next interval
    Serial.printf("hub %s -> %s\n", HUB_HOSTNAME, hubIp.toString().c_str());
  }
  char hello[32];
  int n = snprintf(hello, sizeof(hello), "HELLO rover %u\n", CMD_PORT);
  txUdp.beginPacket(hubIp, HUB_PORT);
  txUdp.write((const uint8_t *)hello, n);
  txUdp.endPacket();
}

void loop() {
  esp_task_wdt_reset();
  while (true) {
    int len = rxUdp.parsePacket();
    if (len <= 0) break;
    char buf[48];
    int n = rxUdp.read(buf, sizeof(buf) - 1);
    if (n <= 0) continue;
    buf[n] = '\0';
    // strip trailing newline(s)
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) buf[--n] = '\0';
    commanderIp = rxUdp.remoteIP();
    commanderPort = rxUdp.remotePort();
    lastPacketMs = millis();
    bool isMotor = (buf[0] == 'M' || buf[0] == 'S');
    if (handleLine(buf)) {
      if (isMotor) {
        lastCommandMs = millis();
        watchdogTripped = false;
      }
      reply("A");
    }
    // malformed lines are ignored and do NOT feed the watchdog
  }

  unsigned long now = millis();

  // No live commander? Keep announcing so the hub can reach us the
  // moment it starts (and re-resolve in case the hub's IP changed).
  if (now - lastPacketMs > COMMANDER_FRESH_MS &&
      now - lastHelloMs > HELLO_INTERVAL_MS) {
    lastHelloMs = now;
    if (now - lastPacketMs > 30000) hubIp = IPAddress();  // stale, re-resolve
    sendHello();
  }

  if (!watchdogTripped && now - lastCommandMs > WATCHDOG_MS) {
    bool wasMoving = moving;
    allStop();
    watchdogTripped = true;
    if (wasMoving) {
      reply("W");
      Serial.println("W (watchdog stop)");
    }
  }

  if (now - lastHeartbeatMs >= HEARTBEAT_MS) {
    lastHeartbeatMs = now;
    reply("H");
  }

  static unsigned long lastStatusMs = 0;
  if (now - lastStatusMs >= 2000) {
    lastStatusMs = now;
    Serial.printf("status wifi=%d rssi=%d wdstop=%d cmd_age_ms=%lu\n",
                  WiFi.status() == WL_CONNECTED, WiFi.RSSI(),
                  watchdogTripped, (unsigned long)(now - lastCommandMs));
  }
}
