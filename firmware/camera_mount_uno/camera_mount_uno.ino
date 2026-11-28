// Batsy camera mount on an Arduino Uno R3: two servos, driven by Batsy over the USB cable (115200 baud, one command
// per line, every command answers with a status line in JSON).
//
//   pan  = MG995, 360° continuous rotation, on D9: it can't hold an angle, only a speed. A turn of N degrees is a timed
//          spin at a calibrated degrees-per-second; the Uno keeps an estimate of where it points.
//   tilt = MG90S, 180° positional, on D10: a real angle.
//
// Power the servos from their own 5-6 V supply (2 A or more), not the Uno's 5V pin: the MG995 pulls over 1 A when it
// starts and would reset the Uno. Join the grounds (supply GND to an Uno GND pin). See README.md.
//
//   ?            status
//   T <deg>      tilt to 10..170
//   R <deg>      pan by that many degrees (+ = clockwise seen from above), -720..720
//   S <speed>    pan continuously at -100..100 until X
//   X            stop panning
//   C            back to where it started (the estimate) and tilt 90
//   Z            "it's pointing straight ahead now": the pan estimate becomes 0
//   K <dps> <stop µs> <dir>   calibration: degrees per second at full speed, the pulse that means stop, 1 / -1
#include <Servo.h>
#include <EEPROM.h>

const int PAN_PIN = 9, TILT_PIN = 10;
const int TILT_MIN = 10, TILT_MAX = 170;

struct Cal { uint32_t magic; float dps; int stopUs; int dir; int tilt; };
Cal cal = {0xBA75CA12, 290.0, 1500, 1, 90};  // 290°/s at full speed: measured on this MG995

Servo pan, tilt;
float panEst = 0;
int tiltNow = 90, speedNow = 0;
unsigned long spinFrom = 0, turnUntil = 0;
String buf;

void saveCal() { EEPROM.put(0, cal); }

void panPulse(int speed) {
  if (speed == 0) { pan.detach(); return; }  // no pulses at all: no creeping
  if (!pan.attached()) pan.attach(PAN_PIN, 500, 2500);
  pan.writeMicroseconds(cal.stopUs + cal.dir * speed * 5);  // ±500 µs at full speed
}

void account() {
  if (speedNow != 0) {
    unsigned long now = millis();
    panEst += cal.dps * (speedNow / 100.0) * (now - spinFrom) / 1000.0;
    spinFrom = now;
  }
}

// Powered from the Uno's USB 5 V: the MG995's surge when it starts at full power resets the board. So it ramps up
// over ~0.4 s, tops out at 60%, and the tilt never moves while it's turning.
const int MAX_SPEED = 60;

void startSpin(int speed, unsigned long ms) {
  account();
  int target = constrain(speed, -MAX_SPEED, MAX_SPEED);
  for (int k = 1; k <= 8; k++) {  // the ramp counts toward the turn (as half its time at full speed)
    panPulse(target * k / 8);
    delay(50);
  }
  speedNow = target;
  spinFrom = millis() - 200;
  turnUntil = ms ? millis() + (ms > 200 ? ms - 200 : 0) : 0;
  panPulse(speedNow);
}

void stopPan() { account(); speedNow = 0; turnUntil = 0; panPulse(0); }

void tiltTo(int deg) {  // gently, so the camera doesn't jerk
  if (speedNow) stopPan();  // one servo at a time (the board's USB power can't feed both at once)
  deg = constrain(deg, TILT_MIN, TILT_MAX);
  if (!tilt.attached()) tilt.attach(TILT_PIN, 500, 2400);
  int step = deg > tiltNow ? 1 : -1;
  while (tiltNow != deg) {
    tiltNow += step;
    tilt.write(tiltNow);
    delay(8);
    if (turnUntil && millis() >= turnUntil) stopPan();
  }
  cal.tilt = tiltNow;
  saveCal();
}

void turnBy(float deg) {  // slower for small turns: less overshoot
  if (fabs(deg) < 1) return;
  int speed = fabs(deg) < 30 ? 35 : MAX_SPEED;
  unsigned long ms = (unsigned long)(fabs(deg) / (cal.dps * speed / 100.0) * 1000.0);
  startSpin(deg > 0 ? speed : -speed, ms);
}

void status() {
  account();
  Serial.print(F("{\"name\":\"batsy-cam\",\"pan\":")); Serial.print(panEst, 1);
  Serial.print(F(",\"tilt\":")); Serial.print(tiltNow);
  Serial.print(F(",\"moving\":")); Serial.print(speedNow ? F("true") : F("false"));
  Serial.print(F(",\"speed\":")); Serial.print(speedNow);
  Serial.print(F(",\"dps\":")); Serial.print(cal.dps, 1);
  Serial.print(F(",\"stop\":")); Serial.print(cal.stopUs);
  Serial.print(F(",\"dir\":")); Serial.print(cal.dir);
  Serial.println(F("}"));
}

void run(String c) {
  c.trim();
  if (!c.length()) return;
  char op = toupper(c[0]);
  String rest = c.substring(1);
  rest.trim();
  switch (op) {
    case 'T': tiltTo(rest.toInt()); break;
    case 'R': turnBy(constrain(rest.toFloat(), -720, 720)); break;
    case 'S': startSpin(rest.toInt(), 0); break;
    case 'X': stopPan(); break;
    case 'Z': stopPan(); panEst = 0; break;
    case 'C': {
      stopPan();
      float back = fmod(panEst, 360.0);  // the short way round
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
  status();
}

void setup() {
  Serial.begin(115200);
  Cal c;
  EEPROM.get(0, c);
  if (c.magic == cal.magic && c.dps > 5 && c.dps < 2000) cal = c;
  tiltNow = constrain(cal.tilt, TILT_MIN, TILT_MAX);
  tilt.attach(TILT_PIN, 500, 2400);
  tilt.write(tiltNow);
  status();
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { run(buf); buf = ""; }
    else if (ch != '\r' && buf.length() < 40) buf += ch;
  }
  if (turnUntil && millis() >= turnUntil) stopPan();
}
