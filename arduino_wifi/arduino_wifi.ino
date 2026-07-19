#include <SPI.h>
#include <WiFiNINA.h>

// Fill these in before uploading to the Nano 33 IoT.
const char WIFI_SSID[] = "gigacube-5039";
const char WIFI_PASS[] = "7eARDTBA65L35d68";

const int HTTP_PORT = 5000;
WiFiServer server(HTTP_PORT);

const int AMP_ON     = 20;  // HIGH = amp on, LOW = muted
const int MIC_SW     = 21;  // inverted: LOW = mic connected, HIGH = mic disconnected
const int RECORD_LED = 2;   // active-low LED

const int ERASE_IN1  = 5;   // DRV8833 erase channel IN1
const int ERASE_IN2  = 6;   // DRV8833 erase channel IN2

const int MOTOR_IN3  = 9;   // DRV8833 motor channel IN3
const int MOTOR_IN4  = 10;  // DRV8833 motor channel IN4

const int MIN_MOTOR_SPEED = 180;
const int DEFAULT_MOTOR_SPEED = MIN_MOTOR_SPEED;
const int DEFAULT_ERASE_FREQ_HZ = 20000;
const unsigned long SERIAL_WAIT_MS = 3000;
const unsigned long WIFI_CONNECT_TIMEOUT_MS = 15000;
const unsigned long WIFI_RETRY_DELAY_MS = 3000;
const unsigned long HTTP_REQUEST_TIMEOUT_MS = 1500;
const unsigned long HEARTBEAT_INTERVAL_MS = 5000;

bool recorderEnabled = true;
bool currentRecordMode = false;
bool eraseActive = false;
int eraseFreqHz = DEFAULT_ERASE_FREQ_HZ;
unsigned long eraseHalfPeriodUs = 25;
unsigned long lastEraseToggle = 0;
bool erasePhase = false;

int currentMotorPWM = 0;
bool currentMotorReverse = false;
bool motorOutputKnown = false;
bool motorOutputReverse = false;
unsigned long lastHeartbeat = 0;

int clampInt(int value, int low, int high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

int normalizeMotorSpeed(int speed) {
  speed = clampInt(speed, 0, 255);

  if (speed == 0) {
    return 0;
  }

  return clampInt(speed, MIN_MOTOR_SPEED, 255);
}

void updateEraseTiming() {
  eraseFreqHz = clampInt(eraseFreqHz, 1000, 50000);
  eraseHalfPeriodUs = 500000UL / (unsigned long)eraseFreqHz;

  if (eraseHalfPeriodUs < 1) {
    eraseHalfPeriodUs = 1;
  }
}

void updateAmpMute() {
  if (!recorderEnabled) {
    digitalWrite(AMP_ON, LOW);
  } else if (eraseActive) {
    digitalWrite(AMP_ON, LOW);
  } else if (currentRecordMode) {
    digitalWrite(AMP_ON, LOW);
  } else {
    digitalWrite(AMP_ON, HIGH);
  }
}

void stopEraseOutputs() {
  eraseActive = false;
  digitalWrite(ERASE_IN1, LOW);
  digitalWrite(ERASE_IN2, LOW);
}

void updateEraseHead() {
  if (!eraseActive) {
    digitalWrite(ERASE_IN1, LOW);
    digitalWrite(ERASE_IN2, LOW);
    return;
  }

  unsigned long now = micros();

  if (now - lastEraseToggle >= eraseHalfPeriodUs) {
    lastEraseToggle += eraseHalfPeriodUs;
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

void applyMotor() {
  if (!recorderEnabled) {
    analogWrite(MOTOR_IN3, 0);
    analogWrite(MOTOR_IN4, 0);
    digitalWrite(MOTOR_IN3, LOW);
    digitalWrite(MOTOR_IN4, LOW);
    motorOutputKnown = false;
    return;
  }

  currentMotorPWM = normalizeMotorSpeed(currentMotorPWM);

  if (currentRecordMode && currentMotorPWM == 0) {
    currentMotorPWM = DEFAULT_MOTOR_SPEED;
  }

  if (currentMotorPWM == 0) {
    analogWrite(MOTOR_IN3, 0);
    analogWrite(MOTOR_IN4, 0);
    digitalWrite(MOTOR_IN3, LOW);
    digitalWrite(MOTOR_IN4, LOW);
    motorOutputKnown = false;
    return;
  }

  if (motorOutputKnown && motorOutputReverse != currentMotorReverse) {
    analogWrite(MOTOR_IN3, 0);
    analogWrite(MOTOR_IN4, 0);
    digitalWrite(MOTOR_IN3, LOW);
    digitalWrite(MOTOR_IN4, LOW);
    delay(20);
  }

  if (currentMotorReverse) {
    analogWrite(MOTOR_IN3, 0);
    digitalWrite(MOTOR_IN3, LOW);
    analogWrite(MOTOR_IN4, currentMotorPWM);
  } else {
    analogWrite(MOTOR_IN4, 0);
    digitalWrite(MOTOR_IN4, LOW);
    analogWrite(MOTOR_IN3, currentMotorPWM);
  }

  motorOutputKnown = true;
  motorOutputReverse = currentMotorReverse;
}

void ensureMotorForRecord() {
  int speed = normalizeMotorSpeed(currentMotorPWM);

  if (speed == 0) {
    currentMotorPWM = DEFAULT_MOTOR_SPEED;
    applyMotor();
    return;
  }

  currentMotorPWM = speed;

  if (!motorOutputKnown || motorOutputReverse != currentMotorReverse) {
    applyMotor();
  }
}

void delayWithUpdates(unsigned long ms) {
  unsigned long start = millis();

  while (millis() - start < ms) {
    updateEraseHead();
    yield();
  }
}

void setRecordMode(bool muteAmp, bool connectMic, bool recordLed) {
  recorderEnabled = true;
  currentRecordMode = true;

  if (recordLed) {
    digitalWrite(RECORD_LED, LOW);
  }

  if (muteAmp) {
    digitalWrite(AMP_ON, LOW);
    delayWithUpdates(100);
  }

  if (connectMic) {
    digitalWrite(MIC_SW, LOW);
    delayWithUpdates(50);
  }

  if (muteAmp) {
    updateAmpMute();
  }

  ensureMotorForRecord();
}

void setPlayMode() {
  recorderEnabled = true;
  currentRecordMode = false;

  digitalWrite(MIC_SW, HIGH);
  delayWithUpdates(100);
  digitalWrite(RECORD_LED, HIGH);

  updateAmpMute();
}

String getPath(String requestLine) {
  int firstSpace = requestLine.indexOf(' ');
  int secondSpace = requestLine.indexOf(' ', firstSpace + 1);

  if (firstSpace < 0 || secondSpace < 0) {
    return "/";
  }

  return requestLine.substring(firstSpace + 1, secondSpace);
}

String getPathOnly(String url) {
  int queryStart = url.indexOf('?');

  if (queryStart < 0) {
    return url;
  }

  return url.substring(0, queryStart);
}

String getParam(String url, String name, String fallback) {
  int queryStart = url.indexOf('?');

  if (queryStart < 0) {
    return fallback;
  }

  String query = url.substring(queryStart + 1);
  int start = 0;

  while (start < query.length()) {
    int end = query.indexOf('&', start);

    if (end < 0) {
      end = query.length();
    }

    String pair = query.substring(start, end);
    int equals = pair.indexOf('=');
    String key = equals >= 0 ? pair.substring(0, equals) : pair;
    String value = equals >= 0 ? pair.substring(equals + 1) : "1";

    if (key == name) {
      return value;
    }

    start = end + 1;
  }

  return fallback;
}

bool paramEnabled(String url, String name, bool fallback) {
  String value = getParam(url, name, fallback ? "1" : "0");
  value.toLowerCase();

  return !(value == "0" || value == "false" || value == "no" || value == "off");
}

void sendText(WiFiClient &client, int code, const char *status, const char *type, String body) {
  client.print("HTTP/1.1 ");
  client.print(code);
  client.print(" ");
  client.println(status);
  client.print("Content-Type: ");
  client.println(type);
  client.print("Content-Length: ");
  client.println(body.length());
  client.println("Connection: close");
  client.println();
  client.print(body);
}

String statusJson() {
  String body = "{\"recorder_enabled\":";
  body += recorderEnabled ? "true" : "false";
  body += ",\"mode\":\"";
  body += currentRecordMode ? "record" : "play";
  body += "\",\"erase\":";
  body += eraseActive ? "true" : "false";
  body += ",\"erase_freq_hz\":";
  body += eraseFreqHz;
  body += ",\"motor_speed\":";
  body += currentMotorPWM;
  body += ",\"motor_reverse\":";
  body += currentMotorReverse ? "true" : "false";
  body += ",\"ip\":\"";
  IPAddress ip = WiFi.localIP();
  body += String(ip[0]);
  body += ".";
  body += String(ip[1]);
  body += ".";
  body += String(ip[2]);
  body += ".";
  body += String(ip[3]);
  body += "\"}";
  return body;
}

void sendJson(WiFiClient &client) {
  sendText(client, 200, "OK", "application/json", statusJson());
}

void sendHome(WiFiClient &client) {
  String body = "<h2>Cassette Recorder Control - Arduino WiFi</h2>";
  body += "<p><a href=\"/ping\">Ping</a></p>";
  body += "<p><a href=\"/status\">Status</a></p>";
  body += "<p><a href=\"/play\">Play</a></p>";
  body += "<p><a href=\"/record?led=0\">Record</a></p>";
  body += "<p><a href=\"/erase/on?freq=20000\">Erase ON 20 kHz</a></p>";
  body += "<p><a href=\"/erase/off\">Erase OFF</a></p>";
  body += "<p><a href=\"/motor?speed=180\">Motor 180</a></p>";
  body += "<p><a href=\"/motor?speed=255\">Motor max</a></p>";
  body += "<p><a href=\"/stop\">Stop</a></p>";
  sendText(client, 200, "OK", "text/html", body);
}

void handleRequest(WiFiClient &client, String url) {
  String path = getPathOnly(url);

  Serial.print("HTTP request path: ");
  Serial.println(url);

  if (path == "/") {
    sendHome(client);
    return;
  }

  if (path == "/ping") {
    sendText(client, 200, "OK", "text/plain", "pong\n");
    return;
  }

  if (path == "/power/on") {
    recorderEnabled = true;
    updateAmpMute();
    sendJson(client);
    return;
  }

  if (path == "/power/off") {
    recorderEnabled = false;
    stopEraseOutputs();
    currentMotorPWM = 0;
    applyMotor();
    digitalWrite(AMP_ON, LOW);
    digitalWrite(MIC_SW, HIGH);
    digitalWrite(RECORD_LED, HIGH);
    sendJson(client);
    return;
  }

  if (path == "/play") {
    setPlayMode();
    sendJson(client);
    return;
  }

  if (path == "/record") {
    bool muteAmp = paramEnabled(url, "mute", true);
    bool connectMic = paramEnabled(url, "mic", true);
    bool recordLed = paramEnabled(url, "led", true);
    setRecordMode(muteAmp, connectMic, recordLed);
    sendJson(client);
    return;
  }

  if (path == "/erase/on") {
    eraseFreqHz = getParam(url, "freq", String(DEFAULT_ERASE_FREQ_HZ)).toInt();
    updateEraseTiming();
    eraseActive = true;
    lastEraseToggle = micros();
    updateAmpMute();
    sendJson(client);
    return;
  }

  if (path == "/erase/off") {
    stopEraseOutputs();
    updateAmpMute();
    sendJson(client);
    return;
  }

  if (path == "/motor") {
    String speedValue = getParam(url, "speed", "");
    String reverseValue = getParam(url, "reverse", "");

    recorderEnabled = true;

    if (speedValue.length() > 0) {
      currentMotorPWM = normalizeMotorSpeed(speedValue.toInt());
    }

    if (reverseValue.length() > 0) {
      reverseValue.toLowerCase();
      currentMotorReverse = (
        reverseValue == "1" ||
        reverseValue == "true" ||
        reverseValue == "yes" ||
        reverseValue == "on"
      );
    }

    applyMotor();
    sendJson(client);
    return;
  }

  if (path == "/reverse/on") {
    currentMotorReverse = true;
    applyMotor();
    sendJson(client);
    return;
  }

  if (path == "/reverse/off") {
    currentMotorReverse = false;
    applyMotor();
    sendJson(client);
    return;
  }

  if (path == "/stop") {
    currentMotorPWM = 0;
    applyMotor();
    sendJson(client);
    return;
  }

  if (path == "/status") {
    sendJson(client);
    return;
  }

  sendText(client, 404, "Not Found", "application/json", "{\"ok\":false,\"error\":\"not found\"}");
}

void handleClient() {
  WiFiClient client = server.available();

  if (!client) {
    return;
  }

  client.setTimeout(HTTP_REQUEST_TIMEOUT_MS);

  String requestLine = "";
  unsigned long started = millis();

  while (client.connected() && millis() - started < HTTP_REQUEST_TIMEOUT_MS) {
    updateEraseHead();

    if (!client.available()) {
      delay(1);
      continue;
    }

    requestLine = client.readStringUntil('\n');
    requestLine.trim();
    Serial.print("HTTP request line: ");
    Serial.println(requestLine);
    break;
  }

  while (client.connected() && client.available()) {
    String header = client.readStringUntil('\n');

    if (header == "\r" || header.length() == 0) {
      break;
    }
  }

  if (requestLine.length() > 0) {
    handleRequest(client, getPath(requestLine));
  } else {
    Serial.println("HTTP request timed out before request line");
  }

  client.flush();
  delay(10);
  client.stop();
  Serial.println("HTTP client closed");
}

const char *wifiStatusName(int status) {
  switch (status) {
    case WL_IDLE_STATUS:
      return "WL_IDLE_STATUS";
    case WL_NO_SSID_AVAIL:
      return "WL_NO_SSID_AVAIL";
    case WL_SCAN_COMPLETED:
      return "WL_SCAN_COMPLETED";
    case WL_CONNECTED:
      return "WL_CONNECTED";
    case WL_CONNECT_FAILED:
      return "WL_CONNECT_FAILED";
    case WL_CONNECTION_LOST:
      return "WL_CONNECTION_LOST";
    case WL_DISCONNECTED:
      return "WL_DISCONNECTED";
    case WL_NO_MODULE:
      return "WL_NO_MODULE";
    default:
      return "UNKNOWN";
  }
}

void printWiFiStatus() {
  int status = WiFi.status();

  Serial.print("WiFi status: ");
  Serial.print(status);
  Serial.print(" ");
  Serial.println(wifiStatusName(status));
}

void printHeartbeat() {
  if (millis() - lastHeartbeat < HEARTBEAT_INTERVAL_MS) {
    return;
  }

  lastHeartbeat = millis();

  Serial.print("Heartbeat WiFi=");
  Serial.print(wifiStatusName(WiFi.status()));
  Serial.print(" IP=");
  Serial.print(WiFi.localIP());
  Serial.print(" RSSI=");
  Serial.print(WiFi.RSSI());
  Serial.println(" dBm");
}

void connectWiFi() {
  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("WiFi module not detected. Check board selection and Nano 33 IoT hardware.");

    while (true) {
      delay(1000);
    }
  }

  Serial.print("WiFiNINA firmware: ");
  Serial.println(WiFi.firmwareVersion());
  Serial.print("Connecting to SSID: ");
  Serial.println(WIFI_SSID);

  int attempt = 1;

  while (WiFi.status() != WL_CONNECTED) {
    Serial.print("WiFi attempt ");
    Serial.println(attempt);

    WiFi.begin(WIFI_SSID, WIFI_PASS);

    unsigned long started = millis();

    while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_CONNECT_TIMEOUT_MS) {
      Serial.print(".");
      delay(500);
    }

    Serial.println();
    printWiFiStatus();

    if (WiFi.status() != WL_CONNECTED) {
      Serial.print("Retrying in ");
      Serial.print(WIFI_RETRY_DELAY_MS / 1000);
      Serial.println(" seconds");
      delay(WIFI_RETRY_DELAY_MS);
    }

    attempt++;
  }

  Serial.println("WiFi connected");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());
  Serial.print("Signal RSSI: ");
  Serial.print(WiFi.RSSI());
  Serial.println(" dBm");
}

void setup() {
  Serial.begin(115200);

  unsigned long serialStarted = millis();

  while (!Serial && millis() - serialStarted < SERIAL_WAIT_MS) {
    delay(10);
  }

  Serial.println();
  Serial.println("Arduino WiFi cassette controller booting");

  pinMode(AMP_ON, OUTPUT);
  pinMode(MIC_SW, OUTPUT);
  pinMode(RECORD_LED, OUTPUT);

  pinMode(ERASE_IN1, OUTPUT);
  pinMode(ERASE_IN2, OUTPUT);

  pinMode(MOTOR_IN3, OUTPUT);
  pinMode(MOTOR_IN4, OUTPUT);

  digitalWrite(AMP_ON, LOW);
  digitalWrite(MIC_SW, HIGH);
  digitalWrite(RECORD_LED, HIGH);

  stopEraseOutputs();
  currentMotorPWM = 0;
  applyMotor();
  setPlayMode();

  connectWiFi();
  server.begin();

  Serial.print("Arduino recorder online at http://");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(HTTP_PORT);
}

void loop() {
  printHeartbeat();
  updateEraseHead();
  handleClient();
}
