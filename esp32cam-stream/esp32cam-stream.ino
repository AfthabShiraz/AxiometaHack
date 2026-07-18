// ESP32-CAM (AI-Thinker) -> JPEG frames over USB serial.
// Each frame: magic 0xAA 0x55 0xAA 0x55, uint32 LE length, uint32 LE
// byte-sum checksum, JPEG bytes. Pair with viewer.py on the laptop to
// watch the stream in a browser.

#include "esp_camera.h"

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

static const uint8_t FRAME_MAGIC[4] = {0xAA, 0x55, 0xAA, 0x55};

void setup() {
  Serial.begin(460800);
  Serial.setTxBufferSize(4096);

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
  config.frame_size = FRAMESIZE_QVGA;  // 320x240 keeps framerate usable at 230400 baud
  config.jpeg_quality = 15;            // lower = better quality, bigger frames
  config.fb_count = psramFound() ? 2 : 1;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    // Plain-text error so viewer.py can surface it; no magic prefix.
    while (true) {
      Serial.printf("CAMERA_INIT_FAILED err=0x%x\n", err);
      delay(2000);
    }
  }
}

void loop() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    delay(100);
    return;
  }
  uint32_t len = fb->len;
  uint32_t sum = 0;
  for (uint32_t i = 0; i < len; i++) sum += fb->buf[i];
  Serial.write(FRAME_MAGIC, sizeof(FRAME_MAGIC));
  Serial.write((uint8_t *)&len, 4);
  Serial.write((uint8_t *)&sum, 4);
  Serial.write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}
