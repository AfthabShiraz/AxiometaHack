// Wiring verification test for L298N + 4 DC motors (2 per side, parallel).
// No serial input needed: cycles each side forward/reverse, then both,
// printing what it's doing. Prop the chassis up on a box so wheels spin free.
//
// PASS = "LEFT FORWARD" makes both left wheels spin in the direction that
// would drive the robot forward, same for right. If one wheel on a side is
// reversed, swap that single motor's two leads at the L298N terminal.

const int ENA = 10;  // left speed (PWM)
const int IN1 = 8;   // left dir
const int IN2 = 9;   // left dir
const int ENB = 11;  // right speed (PWM)
const int IN3 = 12;  // right dir
const int IN4 = 13;  // right dir

const int TEST_SPEED = 180;  // 0..255

void setLeft(int speed) {  // -255..255
  digitalWrite(IN1, speed >= 0 ? HIGH : LOW);
  digitalWrite(IN2, speed >= 0 ? LOW : HIGH);
  analogWrite(ENA, abs(speed));
}

void setRight(int speed) {
  digitalWrite(IN3, speed >= 0 ? HIGH : LOW);
  digitalWrite(IN4, speed >= 0 ? LOW : HIGH);
  analogWrite(ENB, abs(speed));
}

void allStop() {
  setLeft(0);
  setRight(0);
}

void step(const char *label, int left, int right) {
  Serial.println(label);
  setLeft(left);
  setRight(right);
  delay(2000);
  allStop();
  delay(1000);
}

void setup() {
  pinMode(ENA, OUTPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(ENB, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  allStop();
  Serial.begin(115200);
  Serial.println("L298N wiring test starting in 3s...");
  delay(3000);
}

void loop() {
  step("LEFT FORWARD", TEST_SPEED, 0);
  step("LEFT REVERSE", -TEST_SPEED, 0);
  step("RIGHT FORWARD", 0, TEST_SPEED);
  step("RIGHT REVERSE", 0, -TEST_SPEED);
  step("BOTH FORWARD", TEST_SPEED, TEST_SPEED);
  step("BOTH REVERSE", -TEST_SPEED, -TEST_SPEED);
  step("SPIN (left fwd, right rev)", TEST_SPEED, -TEST_SPEED);

  Serial.println("--- ramp test: both sides 0 -> 255 ---");
  for (int s = 0; s <= 255; s += 5) {
    setLeft(s);
    setRight(s);
    delay(60);
  }
  allStop();
  Serial.println("--- cycle complete, repeating ---\n");
  delay(2000);
}
