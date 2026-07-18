/*
 * Genesis Mini face module — dual OLED eyes + 5x5 matrix + rotary encoder.
 *
 * Listens for UDP JSON on FACE_PORT (default 5010):
 *   {"v":1,"type":"face","emotion":"happy","text":""}
 *
 * Emotions: neutral, happy, worried, sleepy, scan, talking
 * Encoder rotate: cycle local emotion; press: UDP set_mode hint (optional).
 *
 * Requires: wifi_secrets.h with WIFI_SSID / WIFI_PASS
 * Libraries: Adafruit_SSD1306, Adafruit_GFX, Wire, WiFi, WiFiUdp
 *
 * Pin map is a starting point for Genesis Mini — adjust to your board.
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#include "wifi_secrets.h"

#ifndef FACE_PORT
#define FACE_PORT 5010
#endif

#define SCREEN_W 128
#define SCREEN_H 64
#define OLED_RESET -1
#define OLED_ADDR_L 0x3C
#define OLED_ADDR_R 0x3D

// Adjust for Genesis Mini wiring
#define ENC_A 10
#define ENC_B 11
#define ENC_BTN 12
#define MATRIX_SDA 8
#define MATRIX_SCL 9

Adafruit_SSD1306 eyeL(SCREEN_W, SCREEN_H, &Wire, OLED_RESET);
Adafruit_SSD1306 eyeR(SCREEN_W, SCREEN_H, &Wire, OLED_RESET);
WiFiUDP udp;

String emotion = "neutral";
unsigned long lastBlink = 0;
unsigned long lastPkt = 0;
bool eyesClosed = false;

const char *EMOTIONS[] = {"neutral", "happy", "worried", "sleepy", "scan", "talking"};
const int N_EMOTIONS = 6;
int emoIdx = 0;

void drawEyes() {
  eyeL.clearDisplay();
  eyeR.clearDisplay();

  if (eyesClosed || emotion == "sleepy") {
    eyeL.drawLine(20, 32, 108, 32, SSD1306_WHITE);
    eyeR.drawLine(20, 32, 108, 32, SSD1306_WHITE);
  } else if (emotion == "worried") {
    eyeL.drawCircle(64, 36, 18, SSD1306_WHITE);
    eyeR.drawCircle(64, 36, 18, SSD1306_WHITE);
    eyeL.drawLine(40, 20, 88, 28, SSD1306_WHITE);
    eyeR.drawLine(40, 28, 88, 20, SSD1306_WHITE);
  } else if (emotion == "happy") {
    eyeL.fillCircle(64, 30, 16, SSD1306_WHITE);
    eyeR.fillCircle(64, 30, 16, SSD1306_WHITE);
    eyeL.fillCircle(64, 34, 16, SSD1306_BLACK); // crescent
    eyeR.fillCircle(64, 34, 16, SSD1306_BLACK);
  } else {
    // neutral / scan / talking
    eyeL.fillCircle(64, 32, 18, SSD1306_WHITE);
    eyeR.fillCircle(64, 32, 18, SSD1306_WHITE);
    eyeL.fillCircle(64, 34, 8, SSD1306_BLACK);
    eyeR.fillCircle(64, 34, 8, SSD1306_BLACK);
  }

  eyeL.display();
  eyeR.display();
}

void applyEmotion(const String &e) {
  emotion = e;
  for (int i = 0; i < N_EMOTIONS; i++) {
    if (emotion == EMOTIONS[i]) {
      emoIdx = i;
      break;
    }
  }
  drawEyes();
}

void setup() {
  Serial.begin(115200);
  pinMode(ENC_A, INPUT_PULLUP);
  pinMode(ENC_B, INPUT_PULLUP);
  pinMode(ENC_BTN, INPUT_PULLUP);

  Wire.begin();
  if (!eyeL.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR_L)) {
    Serial.println("OLED L fail");
  }
  if (!eyeR.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR_R)) {
    Serial.println("OLED R fail — trying 0x3C for both");
    eyeR.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR_L);
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  Serial.println(WiFi.localIP());

  udp.begin(FACE_PORT);
  applyEmotion("neutral");
  lastBlink = millis();
}

void pollUdp() {
  int n = udp.parsePacket();
  if (n <= 0) return;
  char buf[256];
  int len = udp.read(buf, sizeof(buf) - 1);
  if (len <= 0) return;
  buf[len] = 0;
  // tiny parse for "emotion":"..."
  String s(buf);
  int k = s.indexOf("\"emotion\"");
  if (k < 0) return;
  int c = s.indexOf(':', k);
  int q1 = s.indexOf('"', c + 1);
  int q2 = s.indexOf('"', q1 + 1);
  if (q1 < 0 || q2 < 0) return;
  applyEmotion(s.substring(q1 + 1, q2));
  lastPkt = millis();
}

void pollEncoder() {
  static int lastA = HIGH;
  int a = digitalRead(ENC_A);
  if (a != lastA && a == LOW) {
    if (digitalRead(ENC_B) == HIGH) emoIdx = (emoIdx + 1) % N_EMOTIONS;
    else emoIdx = (emoIdx + N_EMOTIONS - 1) % N_EMOTIONS;
    applyEmotion(EMOTIONS[emoIdx]);
  }
  lastA = a;

  static bool lastBtn = HIGH;
  bool btn = digitalRead(ENC_BTN);
  if (lastBtn == HIGH && btn == LOW) {
    Serial.println("encoder press");
  }
  lastBtn = btn;
}

void loop() {
  pollUdp();
  pollEncoder();

  // idle blink if hub silent
  unsigned long now = millis();
  if (now - lastPkt > 3000 && emotion == "neutral") {
    if (!eyesClosed && now - lastBlink > 3000) {
      eyesClosed = true;
      drawEyes();
      lastBlink = now;
    } else if (eyesClosed && now - lastBlink > 160) {
      eyesClosed = false;
      drawEyes();
      lastBlink = now + random(2000, 5000);
    }
  }
  delay(5);
}
