#!/usr/bin/env python3

import atexit
import socket
import time

from flask import Flask, jsonify, request
from zeroconf import ServiceInfo, Zeroconf
import lgpio


# ============================================================
# GPIO CHIP
# ============================================================
# Raspberry Pi 5 commonly exposes the 40-pin header as gpiochip4.
# Some systems may use gpiochip0, so we try both.
GPIOCHIP_CANDIDATES = [4, 0]


# ============================================================
# BCM GPIO PIN CONFIGURATION
# ============================================================
# Use BCM GPIO numbers, not physical header pin numbers.

RECORDER_EN = 23   # whole-recorder enable pin; HIGH = enabled by default

AMP_ON = 17        # HIGH = amp on, LOW = muted
MIC_SW = 27        # LOW = mic connected, HIGH = mic disconnected
RECORD_LED = 22    # active-low LED

ERASE_IN1 = 5      # DRV8833 erase channel IN1
ERASE_IN2 = 6      # DRV8833 erase channel IN2

MOTOR_IN3 = 12     # DRV8833 motor channel IN3
MOTOR_IN4 = 13     # DRV8833 motor channel IN4


# ============================================================
# SETTINGS
# ============================================================

RECORDER_ENABLE_ACTIVE_HIGH = True

# Erase drive.
# Try /erase/on?freq=20000, 30000, 40000, 50000
DEFAULT_ERASE_FREQ_HZ = 20000

# Less than 50% gives slight dead-time between H-bridge input phases.
ERASE_DUTY_PERCENT = 45

# Motor PWM.
MOTOR_PWM_FREQ_HZ = 20000

# Web server.
HTTP_PORT = 5000

# mDNS/Bonjour service type.
SERVICE_TYPE = "_recorder._tcp.local."


# ============================================================
# GLOBAL STATE
# ============================================================

app = Flask(__name__)

h = None
zeroconf = None
service_info = None

state = {
    "recorder_enabled": False,
    "mode": "play",            # "play" or "record"
    "erase": False,
    "erase_freq_hz": DEFAULT_ERASE_FREQ_HZ,
    "motor_speed": 0,          # 0–255
    "motor_reverse": False,
}


# ============================================================
# BASIC HELPERS
# ============================================================

def debug(msg: str):
    print(f"[DEBUG] {msg}", flush=True)


def clamp(value, low, high):
    return max(low, min(high, value))


def enable_level(on: bool) -> int:
    if RECORDER_ENABLE_ACTIVE_HIGH:
        return 1 if on else 0
    return 0 if on else 1


def open_gpiochip():
    last_error = None

    for chip in GPIOCHIP_CANDIDATES:
        try:
            handle = lgpio.gpiochip_open(chip)
            debug(f"Opened gpiochip{chip}")
            return handle
        except Exception as e:
            last_error = e
            debug(f"Could not open gpiochip{chip}: {e}")

    raise RuntimeError(f"Could not open any GPIO chip. Last error: {last_error}")


def write(pin: int, level: int):
    lgpio.gpio_write(h, pin, 1 if level else 0)


def stop_waveform(pin: int):
    try:
        lgpio.tx_pulse(h, pin, 0, 0)
    except Exception as e:
        debug(f"PWM stop on GPIO {pin} ignored: {e}")

    write(pin, 0)


def start_pwm(pin: int, freq_hz, duty_percent, offset_us=0):
    freq_hz = float(freq_hz)
    duty_percent = float(clamp(duty_percent, 0, 100))

    if freq_hz <= 0 or duty_percent <= 0:
        stop_waveform(pin)
        return

    if duty_percent >= 100:
        stop_waveform(pin)
        write(pin, 1)
        return

    period_us = max(2, round(1_000_000.0 / freq_hz))
    pulse_on_us = round(period_us * (duty_percent / 100.0))
    pulse_on_us = int(clamp(pulse_on_us, 1, period_us - 1))
    pulse_off_us = period_us - pulse_on_us

    lgpio.tx_pulse(
        h,
        pin,
        pulse_on_us,
        pulse_off_us,
        int(max(0, offset_us)),
        0,
    )


def claim_outputs():
    pins = [
        RECORDER_EN,
        AMP_ON,
        MIC_SW,
        RECORD_LED,
        ERASE_IN1,
        ERASE_IN2,
        MOTOR_IN3,
        MOTOR_IN4,
    ]

    for pin in pins:
        lgpio.gpio_claim_output(h, pin, 0)
        debug(f"Claimed GPIO {pin} as output")


# ============================================================
# mDNS / ZEROCONF ADVERTISEMENT
# ============================================================

def get_lan_ip():
    """
    Get the LAN-facing IP address.
    This normally works even if 8.8.8.8 is not actually reachable,
    because UDP connect only chooses a route locally.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname() + ".local")
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def register_mdns_service():
    global zeroconf, service_info

    ip = get_lan_ip()
    hostname = socket.gethostname()
    service_name = f"{hostname} Recorder.{SERVICE_TYPE}"

    zeroconf = Zeroconf()

    service_info = ServiceInfo(
        type_=SERVICE_TYPE,
        name=service_name,
        addresses=[socket.inet_aton(ip)],
        port=HTTP_PORT,
        properties={
            "path": "/",
            "device": "cassette-recorder",
            "host": hostname,
        },
        server=f"{hostname}.local.",
    )

    zeroconf.register_service(service_info)
    debug(f"mDNS advertised: {service_name} at http://{ip}:{HTTP_PORT}")


def unregister_mdns_service():
    global zeroconf, service_info

    if zeroconf and service_info:
        try:
            debug("Unregistering mDNS service")
            zeroconf.unregister_service(service_info)
            zeroconf.close()
        except Exception as e:
            debug(f"mDNS unregister error: {e}")


# ============================================================
# OUTPUT CONTROL
# ============================================================

def stop_erase_outputs():
    debug("Erase outputs OFF")

    stop_waveform(ERASE_IN1)
    stop_waveform(ERASE_IN2)


def start_erase_outputs(freq_hz=None):
    if freq_hz is None:
        freq_hz = state["erase_freq_hz"]

    freq_hz = int(clamp(int(freq_hz), 1000, 100000))
    state["erase_freq_hz"] = freq_hz

    period_us = 1_000_000.0 / freq_hz
    half_period_us = int(period_us / 2)

    debug(
        f"Erase ON: freq={freq_hz} Hz, "
        f"duty={ERASE_DUTY_PERCENT}%, offset={half_period_us} us"
    )

    stop_erase_outputs()

    # Opposite-phase PWM:
    # IN1 starts at phase 0.
    # IN2 starts half a period later.
    start_pwm(ERASE_IN1, freq_hz, ERASE_DUTY_PERCENT, 0)
    start_pwm(ERASE_IN2, freq_hz, ERASE_DUTY_PERCENT, half_period_us)


def update_amp_mute():
    if not state["recorder_enabled"]:
        debug("Amp mute: recorder disabled")
        write(AMP_ON, 0)

    elif state["erase"]:
        debug("Amp mute: erase active")
        write(AMP_ON, 0)

    elif state["mode"] == "record":
        debug("Amp mute: record mode")
        write(AMP_ON, 0)

    else:
        debug("Amp ON: play mode")
        write(AMP_ON, 1)


def set_recorder_power(on: bool):
    debug(f"Recorder power {'ON' if on else 'OFF'}")

    state["recorder_enabled"] = bool(on)
    write(RECORDER_EN, enable_level(on))

    if not on:
        erase_off()
        state["motor_speed"] = 0
        apply_motor()
        write(AMP_ON, 0)
        write(MIC_SW, 1)
        write(RECORD_LED, 1)


def set_record():
    debug("Set mode: RECORD")

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    state["mode"] = "record"

    # LED ON
    write(RECORD_LED, 0)

    # Mute amp first
    write(AMP_ON, 0)
    time.sleep(0.1)

    # Inverted CD4053 logic: LOW connects mic/record path
    write(MIC_SW, 0)

    update_amp_mute()


def set_play():
    debug("Set mode: PLAY")

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    state["mode"] = "play"

    # Inverted CD4053 logic: HIGH disconnects mic/record path
    write(MIC_SW, 1)
    time.sleep(0.1)

    # LED OFF
    write(RECORD_LED, 1)

    update_amp_mute()


def erase_on(freq_hz=None):
    debug(f"Erase requested ON, freq={freq_hz}")

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    state["erase"] = True
    update_amp_mute()
    start_erase_outputs(freq_hz)


def erase_off():
    debug("Erase requested OFF")

    state["erase"] = False
    stop_erase_outputs()
    update_amp_mute()


def apply_motor():
    if not state["recorder_enabled"]:
        debug("Motor OFF: recorder disabled")

        stop_waveform(MOTOR_IN3)
        stop_waveform(MOTOR_IN4)
        return

    speed = int(clamp(state["motor_speed"], 0, 255))
    reverse = bool(state["motor_reverse"])

    duty = (speed / 255.0) * 100.0

    debug(
        f"Apply motor: speed={speed}/255, duty={duty:.1f}%, "
        f"reverse={reverse}"
    )

    # Stop both before changing direction.
    stop_waveform(MOTOR_IN3)
    stop_waveform(MOTOR_IN4)

    if speed == 0:
        return

    if reverse:
        write(MOTOR_IN3, 0)
        start_pwm(MOTOR_IN4, MOTOR_PWM_FREQ_HZ, duty)
    else:
        write(MOTOR_IN4, 0)
        start_pwm(MOTOR_IN3, MOTOR_PWM_FREQ_HZ, duty)


# ============================================================
# SETUP / CLEANUP
# ============================================================

def setup():
    global h

    debug("Starting GPIO setup")
    h = open_gpiochip()
    claim_outputs()

    # Safe startup.
    stop_erase_outputs()

    write(AMP_ON, 0)
    write(MIC_SW, 1)
    write(RECORD_LED, 1)

    stop_waveform(MOTOR_IN3)
    stop_waveform(MOTOR_IN4)

    set_recorder_power(True)
    time.sleep(0.2)
    set_play()

    debug("GPIO setup complete")


def cleanup():
    debug("Cleanup started")

    try:
        erase_off()
    except Exception:
        pass

    try:
        state["motor_speed"] = 0
        apply_motor()
    except Exception:
        pass

    try:
        write(AMP_ON, 0)
        write(MIC_SW, 1)
        write(RECORD_LED, 1)
        write(RECORDER_EN, enable_level(False))
    except Exception:
        pass

    try:
        unregister_mdns_service()
    except Exception:
        pass

    try:
        if h is not None:
            lgpio.gpiochip_close(h)
    except Exception:
        pass

    debug("Cleanup complete")


atexit.register(cleanup)


# ============================================================
# HTTP ROUTES
# ============================================================

@app.get("/")
def index():
    return """
    <h2>Cassette Recorder Control</h2>

    <h3>Power</h3>
    <p><a href="/power/on">Power ON</a></p>
    <p><a href="/power/off">Power OFF</a></p>

    <h3>Mode</h3>
    <p><a href="/play">Play</a></p>
    <p><a href="/record">Record</a></p>

    <h3>Erase</h3>
    <p><a href="/erase/on">Erase ON default</a></p>
    <p><a href="/erase/on?freq=20000">Erase ON 20 kHz</a></p>
    <p><a href="/erase/on?freq=30000">Erase ON 30 kHz</a></p>
    <p><a href="/erase/on?freq=40000">Erase ON 40 kHz</a></p>
    <p><a href="/erase/on?freq=50000">Erase ON 50 kHz</a></p>
    <p><a href="/erase/off">Erase OFF</a></p>

    <h3>Motor</h3>
    <p><a href="/motor?speed=0">Motor stop</a></p>
    <p><a href="/motor?speed=100">Motor speed 100</a></p>
    <p><a href="/motor?speed=180">Motor speed 180</a></p>
    <p><a href="/motor?speed=255">Motor max</a></p>
    <p><a href="/reverse/on">Reverse ON</a></p>
    <p><a href="/reverse/off">Reverse OFF</a></p>

    <h3>Debug</h3>
    <p><a href="/status">Status</a></p>
    """


@app.get("/status")
def route_status():
    debug("HTTP /status")
    return jsonify(state)


@app.route("/power/on", methods=["GET", "POST"])
def route_power_on():
    debug("HTTP /power/on")
    set_recorder_power(True)
    time.sleep(0.2)
    update_amp_mute()
    return jsonify(state)


@app.route("/power/off", methods=["GET", "POST"])
def route_power_off():
    debug("HTTP /power/off")
    set_recorder_power(False)
    return jsonify(state)


@app.route("/play", methods=["GET", "POST"])
def route_play():
    debug("HTTP /play")
    set_play()
    return jsonify(state)


@app.route("/record", methods=["GET", "POST"])
def route_record():
    debug("HTTP /record")
    set_record()
    return jsonify(state)


@app.route("/erase/on", methods=["GET", "POST"])
def route_erase_on():
    debug("HTTP /erase/on")

    freq = request.values.get("freq")
    freq_hz = int(freq) if freq is not None else None

    erase_on(freq_hz)
    return jsonify(state)


@app.route("/erase/off", methods=["GET", "POST"])
def route_erase_off():
    debug("HTTP /erase/off")
    erase_off()
    return jsonify(state)


@app.route("/motor", methods=["GET", "POST"])
def route_motor():
    debug("HTTP /motor")

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    speed = request.values.get("speed")
    reverse = request.values.get("reverse")

    if speed is not None:
        state["motor_speed"] = int(clamp(int(speed), 0, 255))

    if reverse is not None:
        state["motor_reverse"] = reverse.lower() in ["1", "true", "yes", "on"]

    apply_motor()
    return jsonify(state)


@app.route("/reverse/on", methods=["GET", "POST"])
def route_reverse_on():
    debug("HTTP /reverse/on")
    state["motor_reverse"] = True
    apply_motor()
    return jsonify(state)


@app.route("/reverse/off", methods=["GET", "POST"])
def route_reverse_off():
    debug("HTTP /reverse/off")
    state["motor_reverse"] = False
    apply_motor()
    return jsonify(state)


@app.route("/stop", methods=["GET", "POST"])
def route_stop():
    debug("HTTP /stop")
    state["motor_speed"] = 0
    apply_motor()
    return jsonify(state)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    setup()
    register_mdns_service()
    debug(f"Starting Flask server on 0.0.0.0:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT)
