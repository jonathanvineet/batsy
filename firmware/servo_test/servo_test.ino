// Servo check: sweeps the servo on pin 9 from 0 to 180 and back, forever, and blinks the Uno's own LED (pin 13) at
// each end so you can see the board is running. Nothing else: no Batsy, no commands. (Flash camera_mount_uno again
// afterwards.)
#include <Servo.h>
Servo s;
void setup() {
  pinMode(13, OUTPUT);
  s.attach(9);
}
void loop() {
  for (int a = 0; a <= 180; a += 2) { s.write(a); delay(15); }
  digitalWrite(13, HIGH); delay(600); digitalWrite(13, LOW);
  for (int a = 180; a >= 0; a -= 2) { s.write(a); delay(15); }
  digitalWrite(13, HIGH); delay(600); digitalWrite(13, LOW);
}
