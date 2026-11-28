// Batsy camera mount: an ESP8266 (NodeMCU v3) with two servos, controlled by Batsy over Wi-Fi.
//
//   pan  = MG995, 360° continuous rotation, on D5 (GPIO14): it can't hold an angle, only a speed. A turn of N degrees
//          is a timed spin at a calibrated degrees-per-second, and the mount keeps its own estimate of where it points.
//   tilt = MG90S, 180° positional, on D6 (GPIO12): a real angle.
//
// Power the servos from their own 5-6 V supply (2 A or more), not the NodeMCU: the MG995 alone pulls over 1 A when it
// starts. Join the grounds (supply GND to a NodeMCU GND pin). See README.md.
//
// Over the USB cable (115200 baud), the same one-line commands as the Uno version, each answered with the status JSON:
//   ?  status · T <deg> tilt · R <deg> turn · S <speed> spin · X stop · C center · Z zero · K <dps> <stop µs> <dir>
//
// Wi-Fi as well, optional: with no Wi-Fi saved it makes a network "Batsy-Cam-Setup"; join it from the phone and choose
// your Wi-Fi (the password is typed there, never in this code). Then it's batsy-cam.local. USB works meanwhile.
//
// Every request needs the key from secret.h (made by Batsy when it builds this): header "X-Key: ...".
//   GET  /status                      {"pan": est. degrees, "tilt": degrees, "moving": bool, ...}  (no key needed)
//   POST /tilt?deg=0..180             tilt to an angle (gently)
//   POST /turn?deg=-720..720          pan by that many degrees (+ = clockwise seen from above)
//   POST /spin?speed=-100..100        pan continuously at that speed until /stop
//   POST /stop                        stop panning
//   POST /center                      pan back to where it started (by the estimate) and tilt to 90
//   POST /zero                        "it's pointing straight ahead now": the pan estimate becomes 0
//   POST /cal?dps=..&stop=..&dir=..   degrees per second at full speed, the pulse (µs) that means "stop", 1/-1
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <EEPROM.h>
#include <Servo.h>
#include <WiFiManager.h>
#include "secret.h"   // #define BATSY_KEY "..." (written by Batsy's build; not in git)

const int PAN_PIN = 14;   // D5
const int TILT_PIN = 12;  // D6
const int TILT_MIN = 10, TILT_MAX = 170;

struct Cal { uint32_t magic; float dps; int stopUs; int dir; int tilt; };
Cal cal = {0xBA75CA11, 120.0, 1500, 1, 90};

Servo pan, tilt;
ESP8266WebServer server(80);
WiFiManager wm;
bool online = false;
String line;

float panEst = 0;            // where it points, in degrees from "zero" (an estimate: the MG995 has no sensor)
int tiltNow = 90;
int speedNow = 0;            // -100..100 while spinning
unsigned long spinFrom = 0;  // when the current spin started
unsigned long turnUntil = 0; // a timed turn ends here (0: spinning until /stop)

void saveCal() { EEPROM.put(0, cal); EEPROM.commit(); }

void panPulse(int speed) {   // speed -100..100 -> the pulse width around the stop point
  if (speed == 0) { pan.detach(); return; }  // no pulses at all: no creeping
  if (!pan.attached()) pan.attach(PAN_PIN, 500, 2500);
  pan.writeMicroseconds(cal.stopUs + cal.dir * speed * 5);  // ±500 µs at full speed
}

void account() {             // add the spin so far to the estimate
  if (speedNow != 0) {
    unsigned long now = millis();
    panEst += cal.dps * (speedNow / 100.0) * (now - spinFrom) / 1000.0;
    spinFrom = now;
  }
}

// Powered from the board's USB 5 V: the MG995's surge when it starts at full power resets the board. So it ramps
// up over ~0.4 s, tops out at 60%, and the tilt never moves while it's turning.
const int MAX_SPEED = 60;

void startSpin(int speed, unsigned long ms) {
  account();
  int target = constrain(speed, -MAX_SPEED, MAX_SPEED);
  for (int k = 1; k <= 8; k++) {  // the ramp counts toward the turn (as half its time at full speed)
    panPulse(target * k / 8);
    delay(50);
  }
  speedNow = target;
  spinFrom = millis() - 200;  // the ramp moved it about as far as 0.2 s at full speed
  turnUntil = ms ? millis() + (ms > 200 ? ms - 200 : 0) : 0;
  panPulse(speedNow);
}

void stopPan() { account(); speedNow = 0; turnUntil = 0; panPulse(0); }

void tiltTo(int deg) {       // a gentle move, so the camera doesn't jerk
  if (speedNow) stopPan();   // one servo at a time (the board's USB power can't feed both at once)
  deg = constrain(deg, TILT_MIN, TILT_MAX);
  if (!tilt.attached()) tilt.attach(TILT_PIN, 500, 2400);
  int step = deg > tiltNow ? 1 : -1;
  while (tiltNow != deg) { tiltNow += step; tilt.write(tiltNow); delay(8); server.handleClient(); }
  cal.tilt = tiltNow;
}

void turnBy(float deg) {     // full-ish speed for long turns, slower for small ones (less overshoot)
  if (fabs(deg) < 1) return;
  int speed = fabs(deg) < 30 ? 35 : MAX_SPEED;
  unsigned long ms = (unsigned long)(fabs(deg) / (cal.dps * speed / 100.0) * 1000.0);
  startSpin(deg > 0 ? speed : -speed, ms);
}

bool authed() {
  if (server.header("X-Key") == BATSY_KEY) return true;
  server.send(401, "application/json", "{\"error\":\"wrong key\"}");
  return false;
}

String status() {
  account();
  return String("{\"pan\":") + String(panEst, 1) + ",\"tilt\":" + tiltNow + ",\"moving\":" + (speedNow ? "true" : "false") +
         ",\"speed\":" + speedNow + ",\"dps\":" + String(cal.dps, 1) + ",\"stop\":" + cal.stopUs + ",\"dir\":" + cal.dir +
         ",\"name\":\"batsy-cam\",\"ip\":\"" + WiFi.localIP().toString() + "\",\"rssi\":" + WiFi.RSSI() + "}";
}

void reply() { server.send(200, "application/json", status()); }

void setup() {
  Serial.begin(115200);
  EEPROM.begin(64);
  Cal c; EEPROM.get(0, c);
  if (c.magic == cal.magic && c.dps > 5 && c.dps < 2000) cal = c;
  tiltNow = cal.tilt;
  tilt.attach(TILT_PIN, 500, 2400);
  tilt.write(tiltNow);
  pan.detach();

  WiFi.mode(WIFI_STA);
  wm.setConfigPortalBlocking(false);  // USB commands work while the setup network waits
  wm.setConfigPortalTimeout(0);
  wm.autoConnect("Batsy-Cam-Setup");
  WiFi.setSleepMode(WIFI_NONE_SLEEP);  // answers at once

  server.collectHeaders("X-Key");
  server.on("/status", [] { reply(); });
  server.on("/tilt", [] { if (!authed()) return; tiltTo(server.arg("deg").toInt()); saveCal(); reply(); });
  server.on("/turn", [] { if (!authed()) return; turnBy(constrain(server.arg("deg").toFloat(), -720, 720)); reply(); });
  server.on("/spin", [] { if (!authed()) return; startSpin(server.arg("speed").toInt(), 0); reply(); });
  server.on("/stop", [] { if (!authed()) return; stopPan(); reply(); });
  server.on("/zero", [] { if (!authed()) return; stopPan(); panEst = 0; reply(); });
  server.on("/center", [] {
    if (!authed()) return;
    stopPan();
    float back = fmod(panEst, 360.0);           // the short way round
    if (back > 180) back -= 360;
    if (back < -180) back += 360;
    turnBy(-back);
    tiltTo(90);
    reply();
  });
  server.on("/cal", [] {
    if (!authed()) return;
    if (server.hasArg("dps")) cal.dps = constrain(server.arg("dps").toFloat(), 5, 2000);
    if (server.hasArg("stop")) cal.stopUs = constrain(server.arg("stop").toInt(), 1300, 1700);
    if (server.hasArg("dir")) cal.dir = server.arg("dir").toInt() < 0 ? -1 : 1;
    saveCal();
    reply();
  });
  Serial.println(status());
}

void usbCommand(String c) {  // the Uno's protocol, over the cable
  c.trim();
  if (!c.length()) return;
  char op = toupper(c[0]);
  String rest = c.substring(1);
  rest.trim();
  switch (op) {
    case 'T': tiltTo(rest.toInt()); saveCal(); break;
    case 'R': turnBy(constrain(rest.toFloat(), -720, 720)); break;
    case 'S': startSpin(rest.toInt(), 0); break;
    case 'X': stopPan(); break;
    case 'Z': stopPan(); panEst = 0; break;
    case 'C': {
      stopPan();
      float back = fmod(panEst, 360.0);
      if (back > 180) back -= 360;
      if (back < -180) back += 360;
      turnBy(-back);
      tiltTo(90);
      break;
    }
    case 'K': {
      float dps = rest.toFloat();
      int sp = rest.indexOf(' ');
      int stopUs = sp > 0 ? rest.substring(sp + 1).toInt() : cal.stopUs;
      int sp2 = sp > 0 ? rest.indexOf(' ', sp + 1) : -1;
      int dir = sp2 > 0 ? rest.substring(sp2 + 1).toInt() : cal.dir;
      if (dps > 5 && dps < 2000) cal.dps = dps;
      if (stopUs >= 1300 && stopUs <= 1700) cal.stopUs = stopUs;
      cal.dir = dir < 0 ? -1 : 1;
      saveCal();
      break;
    }
  }
  Serial.println(status());
}

void loop() {
  wm.process();
  if (!online && WiFi.status() == WL_CONNECTED) {  // on the Wi-Fi: the name and the web commands
    online = true;
    MDNS.begin("batsy-cam");
    MDNS.addService("http", "tcp", 80);
    server.begin();
  }
  if (online) { server.handleClient(); MDNS.update(); }
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { usbCommand(line); line = ""; }
    else if (ch != '\r' && line.length() < 40) line += ch;
  }
  if (turnUntil && millis() >= turnUntil) stopPan();
}
