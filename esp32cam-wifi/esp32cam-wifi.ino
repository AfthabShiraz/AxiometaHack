// ESP32-CAM (AI-Thinker) WiFi MJPEG streamer.
// Joins the network in wifi_secrets.h and serves live video at
// http://<board-ip>/ (also http://esp32cam.local/). If it can't join
// within 15s it starts its own hotspot (AP_SSID) at 192.168.4.1.
// Prints STA_IP/AP_IP over serial at 115200 for discovery.

#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <Update.h>
#include <lwip/sockets.h>
#include "wifi_secrets.h"

// AI-Thinker ESP32-CAM pin map
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

static const char INDEX_HTML[] =
    "<!doctype html><title>ESP32-CAM</title>"
    "<body style='margin:0;background:#111;display:grid;"
    "place-items:center;height:100vh'>"
    "<img src='/stream' style='max-width:100%'></body>";

static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, INDEX_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t status_handler(httpd_req_t *req) {
  uint32_t t0 = millis();
  camera_fb_t *fb = esp_camera_fb_get();
  uint32_t dt = millis() - t0;
  char body[160];
  int n = snprintf(body, sizeof(body),
                   "{\"psram\":%d,\"heap\":%u,\"fb_ok\":%d,"
                   "\"fb_len\":%u,\"fb_ms\":%u,\"rssi\":%d}\n",
                   psramFound(), esp_get_free_heap_size(),
                   fb != nullptr, fb ? fb->len : 0, dt, WiFi.RSSI());
  if (fb) esp_camera_fb_return(fb);
  httpd_resp_set_type(req, "application/json");
  return httpd_resp_send(req, body, n);
}

// Chunked sender. On a clean link (phone hotspot: 0% loss at 1400B)
// full-MSS chunks with no pacing are fine. If moving back to a network
// that drops large packets (the venue WiFi lost 66% of 800B pings),
// drop CHUNK to 512 and PACE_MS to 2.
// Full-MSS chunks, no pacing: current power bank holds the rail fine
// (tested 2026-07-18). If reverting to a saggy supply, see README —
// drop CHUNK to 512 and PACE_MS to 5.
static const size_t CHUNK = 1436;
static const uint32_t PACE_MS = 0;

static esp_err_t send_paced(httpd_req_t *req, const char *data, size_t len) {
  for (size_t off = 0; off < len; off += CHUNK) {
    esp_err_t res = httpd_resp_send_chunk(req, data + off,
                                          min(CHUNK, len - off));
    if (res != ESP_OK) return res;
    if (PACE_MS) delay(PACE_MS);
  }
  return ESP_OK;
}

static esp_err_t stream_handler(httpd_req_t *req) {
  esp_err_t res = httpd_resp_set_type(
      req, "multipart/x-mixed-replace;boundary=frame");
  if (res != ESP_OK) return res;
  int fd = httpd_req_to_sockfd(req);
  int nodelay = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
  char part[96];
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      delay(100);
      continue;
    }
    int hlen = snprintf(part, sizeof(part),
                        "--frame\r\nContent-Type: image/jpeg\r\n"
                        "Content-Length: %u\r\n\r\n", fb->len);
    res = httpd_resp_send_chunk(req, part, hlen);
    if (res == ESP_OK)
      res = send_paced(req, (const char *)fb->buf, fb->len);
    if (res == ESP_OK)
      res = httpd_resp_send_chunk(req, "\r\n", 2);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) return res;  // client left
  }
}

// HTTP-push OTA: the phone hotspot blocks device->laptop connections,
// which breaks classic espota. Here the laptop pushes instead:
//   curl -X POST --data-binary @firmware.bin \
//        "http://<ip>/update?pass=esp32cam123"
static esp_err_t update_handler(httpd_req_t *req) {
  char query[64] = {0};
  httpd_req_get_url_query_str(req, query, sizeof(query));
  if (strstr(query, "pass=esp32cam123") == nullptr) {
    httpd_resp_set_status(req, "403 Forbidden");
    return httpd_resp_send(req, "bad pass\n", HTTPD_RESP_USE_STRLEN);
  }
  if (!Update.begin(req->content_len)) {
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_send(req, "begin failed\n", HTTPD_RESP_USE_STRLEN);
  }
  static uint8_t buf[4096];
  size_t remaining = req->content_len;
  while (remaining > 0) {
    int n = httpd_req_recv(req, (char *)buf,
                           min(remaining, sizeof(buf)));
    if (n <= 0 || Update.write(buf, n) != (size_t)n) {
      Update.abort();
      httpd_resp_set_status(req, "500 Internal Server Error");
      return httpd_resp_send(req, "write failed\n", HTTPD_RESP_USE_STRLEN);
    }
    remaining -= n;
  }
  if (!Update.end(true)) {
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_send(req, "end failed\n", HTTPD_RESP_USE_STRLEN);
  }
  httpd_resp_send(req, "ok, rebooting\n", HTTPD_RESP_USE_STRLEN);
  delay(500);
  ESP.restart();
  return ESP_OK;
}

static void start_server() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  httpd_handle_t server = nullptr;
  if (httpd_start(&server, &config) != ESP_OK) {
    Serial.println("HTTPD_START_FAILED");
    return;
  }
  httpd_uri_t index_uri = {"/", HTTP_GET, index_handler, nullptr};
  httpd_uri_t status_uri = {"/status", HTTP_GET, status_handler, nullptr};
  httpd_uri_t stream_uri = {"/stream", HTTP_GET, stream_handler, nullptr};
  httpd_uri_t update_uri = {"/update", HTTP_POST, update_handler, nullptr};
  httpd_register_uri_handler(server, &index_uri);
  httpd_register_uri_handler(server, &status_uri);
  httpd_register_uri_handler(server, &stream_uri);
  httpd_register_uri_handler(server, &update_uri);
}

// Onboard flash LED (bright white, front) as a no-network heartbeat:
// 3 quick blinks at boot, then a short blip every 2 s from loop().
// On a power bank: no blinks ever = chip not booting; boot blinks that
// stop = crash/brownout later; steady blipping = alive, check WiFi.
#define FLASH_LED_GPIO 4

static void blink(int times, int ms) {
  for (int i = 0; i < times; i++) {
    digitalWrite(FLASH_LED_GPIO, HIGH);
    delay(ms);
    digitalWrite(FLASH_LED_GPIO, LOW);
    delay(ms);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(FLASH_LED_GPIO, OUTPUT);
  blink(3, 120);

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;  // 640x480; new bank sustains it
  config.jpeg_quality = 12;
  config.fb_count = psramFound() ? 2 : 1;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  // Do not halt on camera failure: WiFi must come up regardless so the
  // board stays reachable (/status reports fb_ok=0 for diagnosis).
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("CAMERA_INIT_FAILED err=0x%x\n", err);
  }

  // Station-only: AP_STA mode forced the hotspot and the join onto one
  // radio channel and broke association at the venue. The hotspot now
  // only starts as a fallback (see loop) if the join fails for 60 s.
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // power-save off: cuts latency from ~300ms to ~5ms
  // Reduced TX power: the power bank browns the chip out on full-power
  // transmit bursts. 11dBm halves the current spike; plenty of link
  // budget at rover-to-phone range. Set before the scan below so probe
  // requests are quieter too.
  WiFi.setTxPower(WIFI_POWER_11dBm);

  // Diagnostics: print why associations fail, and whether we can even
  // see the target SSID (weak/absent antenna shows up here as no hit).
  WiFi.onEvent(
      [](WiFiEvent_t e, WiFiEventInfo_t info) {
        Serial.printf("STA_DISCONNECT reason=%d\n",
                      info.wifi_sta_disconnected.reason);
      },
      WiFiEvent_t::ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  int n = WiFi.scanNetworks();
  Serial.printf("scan: %d networks\n", n);
  for (int i = 0; i < n; i++) {
    if (WiFi.SSID(i) == WIFI_SSID || i < 5) {
      Serial.printf("  %s rssi=%d ch=%d\n", WiFi.SSID(i).c_str(),
                    WiFi.RSSI(i), WiFi.channel(i));
    }
  }
  // DHCP: the phone hotspot assigns its own subnet. (The venue-network
  // static IP .223 config lived here; restore it if switching back.)
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  MDNS.begin("esp32cam");
  start_server();
  // OTA: reflash over WiFi, no USB needed once mounted on the rover.
  //   arduino-cli upload --fqbn esp32:esp32:esp32cam -p <board-ip> \
  //     --upload-field password=esp32cam123 .
  ArduinoOTA.setHostname("esp32cam");
  ArduinoOTA.setPassword("esp32cam123");
  ArduinoOTA.begin();
}

void loop() {
  static bool wasConnected = false;
  static bool apStarted = false;
  static uint32_t lastAttempt = millis();
  static uint32_t bootMs = millis();
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected && !wasConnected) {
    Serial.printf("STA_IP %s\n", WiFi.localIP().toString().c_str());
  }
  if (!connected && millis() - lastAttempt > 20000) {
    Serial.printf("STA_RETRY status=%d\n", WiFi.status());
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    lastAttempt = millis();
  }
  // Start the hotspot once the join outcome settles: right after a
  // successful join (AP then shares the STA channel, so association
  // isn't disrupted) or after 60 s of failing. Stream stays reachable
  // at 192.168.4.1 either way.
  if (!apStarted && (connected || millis() - bootMs > 60000)) {
    apStarted = true;
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASS);
    Serial.printf("AP_IP %s ssid=%s pass=%s\n",
                  WiFi.softAPIP().toString().c_str(), AP_SSID, AP_PASS);
  }
  wasConnected = connected;
  // No periodic heartbeat blink: the flash LED is a current spike the
  // power bank doesn't need. Boot blinks remain as a power self-test.
  ArduinoOTA.handle();
  delay(20);
}
