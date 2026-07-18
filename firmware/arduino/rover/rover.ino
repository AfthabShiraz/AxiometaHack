// Phase 1 rover firmware (plan.md §3.2, §4.3).
//
// Serial protocol, 115200 baud, newline-terminated ASCII:
//   M,<left>,<right>   left/right in -255..255           -> ack "A"
//   S                  immediate stop                     -> ack "A"
// Emits:
//   H   heartbeat every 500 ms
//   W   once, when the watchdog trips (no valid command for WATCHDOG_MS)
//
// Human-typeable via Serial Monitor (set line ending to "Newline").
// NOTE: with WATCHDOG_MS=300 the motors stop 300 ms after each hand-typed
// command — that is the watchdog working, not a bug. For bench driving by
// hand, temporarily raise WATCHDOG_MS to 2000, but restore 300 before the
// hub ever drives this (FR-15: 300 ms is non-negotiable in the real system).

const int ENA = 10;  // left speed (PWM)
const int IN1 = 8;   // left dir
const int IN2 = 9;   // left dir
const int ENB = 11;  // right speed (PWM)
const int IN3 = 12;  // right dir
const int IN4 = 13;  // right dir

const unsigned long WATCHDOG_MS = 300;
const unsigned long HEARTBEAT_MS = 500;

char lineBuf[32];
uint8_t lineLen = 0;
unsigned long lastCommandMs = 0;
unsigned long lastHeartbeatMs = 0;
bool watchdogTripped = false;
bool moving = false;

void setLeft(int speed) {
  digitalWrite(IN1, speed >= 0 ? HIGH : LOW);
  digitalWrite(IN2, speed >= 0 ? LOW : HIGH);
  analogWrite(ENA, abs(speed));
}

// Right side wired reversed relative to left: polarity flipped here.
void setRight(int speed) {
  digitalWrite(IN3, speed >= 0 ? LOW : HIGH);
  digitalWrite(IN4, speed >= 0 ? HIGH : LOW);
  analogWrite(ENB, abs(speed));
}

void allStop() {
  setLeft(0);
  setRight(0);
  moving = false;
}

int clamp255(long v) {
  if (v > 255) return 255;
  if (v < -255) return -255;
  return (int)v;
}

// Returns true if the line was a valid command.
bool handleLine(char *line) {
  if (line[0] == 'S' && line[1] == '\0') {
    allStop();
    return true;
  }
  if (line[0] == 'M' && line[1] == ',') {
    char *rest;
    long left = strtol(line + 2, &rest, 10);
    if (*rest != ',') return false;
    char *end;
    long right = strtol(rest + 1, &end, 10);
    if (*end != '\0') return false;
    setLeft(clamp255(left));
    setRight(clamp255(right));
    moving = (left != 0 || right != 0);
    return true;
  }
  return false;
}

void setup() {
  pinMode(ENA, OUTPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(ENB, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  allStop();  // FR-18: stopped before anything else happens
  Serial.begin(115200);
  lastCommandMs = millis();
}

void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        lineLen = 0;
        if (handleLine(lineBuf)) {
          lastCommandMs = millis();
          watchdogTripped = false;
          Serial.println("A");
        }
        // malformed lines are ignored and do NOT feed the watchdog
      }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;  // overlong line: discard, treat as malformed
    }
  }

  unsigned long now = millis();

  if (!watchdogTripped && now - lastCommandMs > WATCHDOG_MS) {
    if (moving) {
      allStop();
      Serial.println("W");
    } else {
      allStop();  // belt and braces even if we thought we were stopped
    }
    watchdogTripped = true;
  }

  if (now - lastHeartbeatMs >= HEARTBEAT_MS) {
    lastHeartbeatMs = now;
    Serial.println("H");
  }
}
