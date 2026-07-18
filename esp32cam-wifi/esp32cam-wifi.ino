// ESP32-CAM (AI-Thinker) WiFi MJPEG streamer.
// Joins the network in wifi_secrets.h and serves live video at
// http://<board-ip>/ (also http://esp32cam.local/). If it can't join
// within 15s it starts its own hotspot (AP_SSID) at 192.168.4.1.
// Prints STA_IP/AP_IP over serial at 115200 for discovery.

#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <ESPmDNS.h>
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

// The local network drops packets over ~600 bytes (measured: 800B pings
// lose 66%, 1200B lose 100%). Stream in small paced chunks so every TCP
// segment stays under that threshold.
static const size_t CHUNK = 512;

static esp_err_t send_paced(httpd_req_t *req, const char *data, size_t len) {
  for (size_t off = 0; off < len; off += CHUNK) {
    esp_err_t res = httpd_resp_send_chunk(req, data + off,
                                          min(CHUNK, len - off));
    if (res != ESP_OK) return res;
    delay(2);  // let each chunk leave as its own small segment
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
  httpd_register_uri_handler(server, &index_uri);
  httpd_register_uri_handler(server, &status_uri);
  httpd_register_uri_handler(server, &stream_uri);
}

void setup() {
  Serial.begin(115200);

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
  config.frame_size = FRAMESIZE_QVGA;  // 320x240 while the WiFi link is lossy
  config.jpeg_quality = 12;
  config.fb_count = psramFound() ? 2 : 1;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    while (true) {
      Serial.printf("CAMERA_INIT_FAILED err=0x%x\n", err);
      delay(2000);
    }
  }

  // Hotspot always available; STA join keeps retrying in loop() forever.
  WiFi.mode(WIFI_AP_STA);
  WiFi.setSleep(false);  // power-save off: cuts latency from ~300ms to ~5ms
  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("AP_IP %s ssid=%s pass=%s\n",
                WiFi.softAPIP().toString().c_str(), AP_SSID, AP_PASS);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  MDNS.begin("esp32cam");
  start_server();
}

void loop() {
  static bool wasConnected = false;
  static uint32_t lastAttempt = millis();
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected && !wasConnected) {
    Serial.printf("STA_IP %s\n", WiFi.localIP().toString().c_str());
  }
  if (!connected && millis() - lastAttempt > 20000) {
    Serial.printf("STA_RETRY status=%d\n", WiFi.status());
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    lastAttempt = millis();
  }
  wasConnected = connected;
  delay(1000);
}
