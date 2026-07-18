// Servo pin identification for the AX22 servo module on Genesis Mini
// Port 3 (candidate signal pins: IO9, IO16, IO15).
//
// Drives standard 50 Hz servo PWM on one pin at a time, forever:
//   ~10 s sweeping on IO9  -> then IO16 -> then IO15 -> repeat
// Watch which servo moves during which phase and report the mapping.
// Serial (115200/CDC) prints the active pin.

const int CANDIDATES[] = {9, 16, 15};
const int N = 3;
const uint32_t SERVO_FREQ = 50;
const uint8_t SERVO_RES = 16;

uint32_t usToDuty(uint32_t us) {
  return (uint32_t)((uint64_t)us * ((1 << SERVO_RES) - 1) / 20000);
}

void setup() {
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  Serial.setTxTimeoutMs(0);
#endif
}

void sweep(int pin) {
  Serial.printf("=== sweeping IO%d ===\n", pin);
  ledcAttach(pin, SERVO_FREQ, SERVO_RES);
  for (int cycle = 0; cycle < 3; cycle++) {
    for (uint32_t us = 1000; us <= 2000; us += 20) {
      ledcWrite(pin, usToDuty(us));
      delay(15);
    }
    for (uint32_t us = 2000; us >= 1000; us -= 20) {
      ledcWrite(pin, usToDuty(us));
      delay(15);
    }
  }
  ledcWrite(pin, usToDuty(1500));  // park at center
  delay(500);
  ledcDetach(pin);
}

void loop() {
  for (int i = 0; i < N; i++) sweep(CANDIDATES[i]);
}
