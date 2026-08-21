#!/usr/bin/env python3
"""Manually pulse the SSD1305 reset line on GPIO 4, then scan I2C."""
import time
import subprocess
import RPi.GPIO as GPIO

RESET_PIN = 4

GPIO.setmode(GPIO.BCM)
GPIO.setup(RESET_PIN, GPIO.OUT)

try:
    # Datasheet wants reset held low for at least 3us; we use 10ms to be safe.
    # Sequence matches the driver's reset(): high -> low -> high
    print("Pulsing reset on GPIO 4...")
    GPIO.output(RESET_PIN, GPIO.HIGH)
    time.sleep(0.05)
    GPIO.output(RESET_PIN, GPIO.LOW)
    time.sleep(0.05)
    GPIO.output(RESET_PIN, GPIO.HIGH)
    time.sleep(0.1)
    print("Reset pulse complete.\n")
finally:
    GPIO.cleanup(RESET_PIN)

# Give the controller a moment to settle before probing
time.sleep(0.2)

print("Scanning bus 1...")
subprocess.run(["i2cdetect", "-y", "1"])
