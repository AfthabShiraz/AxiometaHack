// Spin test: left pair forward, right pair reverse, continuously.
// On the ground this would spin the robot in place. Reset board to restart.

const int ENA = 10;  // left speed (PWM)
const int IN1 = 8;   // left dir
const int IN2 = 9;   // left dir
const int ENB = 11;  // right speed (PWM)
const int IN3 = 12;  // right dir
const int IN4 = 13;  // right dir

const int SPEED = 150;  // 0..255

void setup() {
  pinMode(ENA, OUTPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(ENB, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  analogWrite(ENA, 0);
  analogWrite(ENB, 0);

  Serial.begin(115200);
  Serial.println("Spin test starting in 3s...");
  delay(3000);

  // left forward
  digitalWrite(IN1, HIGH);
  digitalWrite(IN2, LOW);
  // right reverse
  digitalWrite(IN3, LOW);
  digitalWrite(IN4, HIGH);
  analogWrite(ENA, SPEED);
  analogWrite(ENB, SPEED);
  Serial.println("Left pair FORWARD, right pair REVERSE.");
}

void loop() {}
