const int AMP_ON     = 20;  // HIGH = amp on, LOW = muted
const int MIC_SW     = 21;  // inverted: LOW = mic connected, HIGH = mic disconnected
const int MODE_SW    = 19;  // toggle switch to GND
const int RECORD_LED = 2;   // active-low LED

const int ERASE_IN1  = 5;   // DRV8833 erase channel IN1
const int ERASE_IN2  = 6;   // DRV8833 erase channel IN2
const int ERASE_BTN  = 11;  // button to GND, INPUT_PULLUP

const int MOTOR_IN3  = 9;   // DRV8833 motor channel IN3
const int MOTOR_IN4  = 10;  // DRV8833 motor channel IN4
const int MOTOR_REV_BTN = 12; // button to GND, INPUT_PULLUP; pressed = reverse

const int POT_PIN    = A0;  // potentiometer wiper

bool currentRecordMode = false;

// Try higher erase frequency to reduce audible residue.
// 20 us + 20 us = 25 kHz.
// If this is too weak, try 25 for 20 kHz or 50 for 10 kHz.
const unsigned long ERASE_HALF_PERIOD_US = 25;

unsigned long lastEraseToggle = 0;
bool erasePhase = false;

// Motor update timing
unsigned long lastMotorUpdate = 0;
const unsigned long MOTOR_UPDATE_INTERVAL_MS = 20;

// Store latest motor values
int currentMotorPWM = 0;
bool currentMotorReverse = false;

bool isErasePressed() {
  return digitalRead(ERASE_BTN) == LOW;
}

void updateAmpMute() {
  bool eraseActive = isErasePressed();

  if (eraseActive) {
    // Always mute speaker while erasing
    digitalWrite(AMP_ON, LOW);
  } else {
    // Normal behavior when not erasing
    if (currentRecordMode) {
      digitalWrite(AMP_ON, LOW);   // record mode: muted
    } else {
      digitalWrite(AMP_ON, HIGH);  // play mode: amp on
    }
  }
}

void updateEraseHead() {
  bool erasePressed = isErasePressed();

  if (!erasePressed) {
    // erase OFF: coast/brake disabled
    digitalWrite(ERASE_IN1, LOW);
    digitalWrite(ERASE_IN2, LOW);
    return;
  }

  // erase ON: alternating drive
  unsigned long now = micros();

  if (now - lastEraseToggle >= ERASE_HALF_PERIOD_US) {
    // More stable than lastEraseToggle = now;
    lastEraseToggle += ERASE_HALF_PERIOD_US;
    erasePhase = !erasePhase;

    if (erasePhase) {
      digitalWrite(ERASE_IN1, HIGH);
      digitalWrite(ERASE_IN2, LOW);
    } else {
      digitalWrite(ERASE_IN1, LOW);
      digitalWrite(ERASE_IN2, HIGH);
    }
  }
}

void updateMotorSpeed() {
  unsigned long now = millis();

  if (now - lastMotorUpdate < MOTOR_UPDATE_INTERVAL_MS) {
    return;
  }

  lastMotorUpdate = now;

  int potValue = analogRead(POT_PIN);  // 0–1023 because analogReadResolution(10)
  currentMotorPWM = map(potValue, 0, 1023, 0, 255);
  currentMotorPWM = constrain(currentMotorPWM, 0, 255);

  currentMotorReverse = (digitalRead(MOTOR_REV_BTN) == LOW);

  if (currentMotorReverse) {
    // reverse at same speed set by potentiometer
    digitalWrite(MOTOR_IN3, LOW);
    analogWrite(MOTOR_IN4, currentMotorPWM);
  } else {
    // forward at same speed set by potentiometer
    analogWrite(MOTOR_IN3, currentMotorPWM);
    digitalWrite(MOTOR_IN4, LOW);
  }
}

void updateAll() {
  updateAmpMute();
  updateEraseHead();
  updateMotorSpeed();
}

// Delay function that keeps erase/motor/amp control active
void delayWithUpdates(unsigned long ms) {
  unsigned long start = millis();

  while (millis() - start < ms) {
    updateAll();
  }
}

void setup() {
  pinMode(AMP_ON, OUTPUT);
  pinMode(MIC_SW, OUTPUT);
  pinMode(RECORD_LED, OUTPUT);
  pinMode(MODE_SW, INPUT_PULLUP);

  pinMode(ERASE_IN1, OUTPUT);
  pinMode(ERASE_IN2, OUTPUT);
  pinMode(ERASE_BTN, INPUT_PULLUP);

  pinMode(MOTOR_IN3, OUTPUT);
  pinMode(MOTOR_IN4, OUTPUT);
  pinMode(MOTOR_REV_BTN, INPUT_PULLUP);

  pinMode(POT_PIN, INPUT);

  // Force 10-bit analogRead so map(..., 0, 1023, ...) is correct
  analogReadResolution(10);

  // erase OFF at startup
  digitalWrite(ERASE_IN1, LOW);
  digitalWrite(ERASE_IN2, LOW);

  // motor initially stopped
  analogWrite(MOTOR_IN3, 0);
  digitalWrite(MOTOR_IN4, LOW);

  setPlayMode();
}

void setRecordMode() {
  // LED ON
  digitalWrite(RECORD_LED, LOW);

  // mute amplifier first
  digitalWrite(AMP_ON, LOW);
  delayWithUpdates(100);

  // inverted CD4053 logic: LOW connects the record/mic path
  digitalWrite(MIC_SW, LOW);

  currentRecordMode = true;
  updateAmpMute();
}

void setPlayMode() {
  // inverted CD4053 logic: HIGH disconnects the record/mic path
  digitalWrite(MIC_SW, HIGH);
  delayWithUpdates(100);

  currentRecordMode = false;

  // LED OFF
  digitalWrite(RECORD_LED, HIGH);

  updateAmpMute();
}

void loop() {
  updateAll();

  // switch closed to GND = RECORD
  bool requestedRecordMode = (digitalRead(MODE_SW) == LOW);

  if (requestedRecordMode != currentRecordMode) {
    delayWithUpdates(30); // debounce while keeping motor/erase updated

    requestedRecordMode = (digitalRead(MODE_SW) == LOW);

    if (requestedRecordMode != currentRecordMode) {
      if (requestedRecordMode) {
        setRecordMode();
      } else {
        setPlayMode();
      }
    }
  }
}