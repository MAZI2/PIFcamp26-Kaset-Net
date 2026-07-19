#!/usr/bin/env python3

import atexit
import re
import shutil
import socket
import subprocess
import time

from flask import Flask, Response, jsonify, request, stream_with_context
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
MIN_MOTOR_SPEED = 180
DEFAULT_MOTOR_SPEED = MIN_MOTOR_SPEED
MOTOR_PWM_FREQ_HZ = 20000

# Web server.
HTTP_PORT = 5000

# mDNS/Bonjour service type.
SERVICE_TYPE = "_recorder._tcp.local."

# USB sound-card monitor stream.
AUDIO_DEVICE = "auto"
AUDIO_RATE = 44100
AUDIO_CHANNELS = 1
AUDIO_FORMAT = "S16_LE"
DEFAULT_AUDIO_SECONDS = 5.0
MAX_AUDIO_SECONDS = 60.0
AUDIO_CAPTURE_TIMEOUT_EXTRA = 10.0


# ============================================================
# GLOBAL STATE
# ============================================================

app = Flask(__name__)

h = None
zeroconf = None
service_info = None
motor_output_reverse = None

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


def parse_bool(value, default=True):
    if value is None:
        return default

    return str(value).lower() not in ["0", "false", "no", "off"]


def normalize_motor_speed(speed) -> int:
    speed = int(clamp(int(speed), 0, 255))

    if speed == 0:
        return 0

    return int(clamp(speed, MIN_MOTOR_SPEED, 255))


def effective_motor_speed() -> int:
    speed = normalize_motor_speed(state["motor_speed"])

    if (
        state["recorder_enabled"]
        and state["mode"] == "record"
        and speed == 0
    ):
        debug(f"Record mode requires motor; forcing speed {DEFAULT_MOTOR_SPEED}")
        speed = DEFAULT_MOTOR_SPEED

    state["motor_speed"] = speed
    return speed


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
# AUDIO CAPTURE / STREAMING
# ============================================================

def find_arecord():
    return shutil.which("arecord")


def parse_alsa_capture_devices(arecord_output):
    card_pattern = re.compile(r"^card\s+(\d+):\s+([^\[]+).*device\s+(\d+):\s+([^\[]+)")
    devices = []

    if not arecord_output:
        return devices

    for line in arecord_output.splitlines():
        match = card_pattern.search(line)

        if not match:
            continue

        card, card_name, device, device_name = match.groups()
        device_id = f"plughw:{card},{device}"
        label = f"{card_name.strip()} / {device_name.strip()}"
        devices.append({"id": device_id, "name": label})

    return devices


def list_alsa_inputs():
    arecord = find_arecord()

    if not arecord:
        raise RuntimeError("arecord not found. Install alsa-utils on the Raspberry Pi.")

    devices = [
        {"id": "default", "name": "ALSA default input"},
        {"id": "auto", "name": "Auto-detect first ALSA capture input"},
    ]

    list_pcms = subprocess.run(
        [arecord, "-L"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    if list_pcms.stdout:
        for line in list_pcms.stdout.splitlines():
            if not line or line[0].isspace():
                continue

            pcm_name = line.strip()

            if pcm_name and pcm_name != "null":
                devices.append({"id": pcm_name, "name": pcm_name})

    list_cards = subprocess.run(
        [arecord, "-l"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    for capture_device in parse_alsa_capture_devices(list_cards.stdout):
        if not any(existing["id"] == capture_device["id"] for existing in devices):
            devices.append(capture_device)

    return {
        "backend": "alsa",
        "default_input": AUDIO_DEVICE,
        "inputs": devices,
        "arecord_l": list_cards.stdout.strip(),
        "arecord_L": list_pcms.stdout.strip(),
    }


def choose_alsa_capture_device(device=None):
    if device and device not in ["auto"]:
        return device

    arecord = find_arecord()

    if not arecord:
        raise RuntimeError("arecord not found. Install alsa-utils on the Raspberry Pi.")

    result = subprocess.run(
        [arecord, "-l"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    devices = parse_alsa_capture_devices(result.stdout)

    if devices:
        chosen = devices[0]["id"]
        debug(f"ALSA auto-selected capture input: {chosen} ({devices[0]['name']})")
        return chosen

    debug("No hardware ALSA capture input found; falling back to ALSA default")
    return "default"


def record_audio_wav_with_arecord(seconds, samplerate, channels, device=None):
    arecord = find_arecord()

    if not arecord:
        raise RuntimeError("arecord not found. Install alsa-utils on the Raspberry Pi.")

    seconds = float(clamp(float(seconds), 0.1, MAX_AUDIO_SECONDS))
    samplerate = int(clamp(int(samplerate), 8000, 96000))
    channels = int(clamp(int(channels), 1, 2))
    total_frames = int(seconds * samplerate)
    device = choose_alsa_capture_device(device or AUDIO_DEVICE)

    command = [
        arecord,
        "-q",
        "-D",
        device,
        "-f",
        AUDIO_FORMAT,
        "-r",
        str(samplerate),
        "-c",
        str(channels),
        "--samples",
        str(total_frames),
        "-t",
        "wav",
    ]

    debug(f"ALSA capture start: {' '.join(command)}")

    result = subprocess.run(
        command,
        capture_output=True,
        timeout=seconds + AUDIO_CAPTURE_TIMEOUT_EXTRA,
        check=False,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"arecord failed with exit {result.returncode}: {stderr}")

    debug(f"ALSA capture complete: bytes={len(result.stdout)}")
    return result.stdout


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


def ensure_motor_for_record():
    speed = normalize_motor_speed(state["motor_speed"])

    if speed == 0:
        debug(f"Record mode starting motor at {DEFAULT_MOTOR_SPEED}")
        state["motor_speed"] = DEFAULT_MOTOR_SPEED
        apply_motor()
        return

    state["motor_speed"] = speed

    if motor_output_reverse is None:
        debug("Record mode motor state unknown; applying motor")
        apply_motor()
        return

    if motor_output_reverse != bool(state["motor_reverse"]):
        debug("Record mode motor direction changed; applying motor")
        apply_motor()
        return

    debug(f"Record mode preserving running motor at speed {speed}")


def set_record(mute_amp=True, connect_mic=True, record_led=True):
    debug(
        f"Set mode: RECORD mute_amp={mute_amp} "
        f"connect_mic={connect_mic} record_led={record_led}"
    )

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    state["mode"] = "record"

    if record_led:
        debug("Record step: LED ON")
        write(RECORD_LED, 0)
    else:
        debug("Record step: LED left unchanged")

    if mute_amp:
        debug("Record step: amp muted")
        write(AMP_ON, 0)
        time.sleep(0.1)
    else:
        debug("Record step: amp left unchanged")

    if connect_mic:
        debug("Record step: mic/record path connected")
        write(MIC_SW, 0)
        time.sleep(0.05)
    else:
        debug("Record step: mic/record path left unchanged")

    if mute_amp:
        update_amp_mute()
    else:
        debug("Record step: automatic amp mute skipped")

    ensure_motor_for_record()


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
    global motor_output_reverse

    if not state["recorder_enabled"]:
        debug("Motor OFF: recorder disabled")

        stop_waveform(MOTOR_IN3)
        stop_waveform(MOTOR_IN4)
        motor_output_reverse = None
        return

    speed = effective_motor_speed()
    reverse = bool(state["motor_reverse"])

    duty = (speed / 255.0) * 100.0

    debug(
        f"Apply motor: speed={speed}/255, duty={duty:.1f}%, "
        f"reverse={reverse}"
    )

    if speed == 0:
        stop_waveform(MOTOR_IN3)
        stop_waveform(MOTOR_IN4)
        motor_output_reverse = None
        return

    if motor_output_reverse is not None and motor_output_reverse != reverse:
        debug("Motor direction changed; stopping both sides before reversing")
        stop_waveform(MOTOR_IN3)
        stop_waveform(MOTOR_IN4)
        time.sleep(0.02)

    if reverse:
        write(MOTOR_IN3, 0)
        start_pwm(MOTOR_IN4, MOTOR_PWM_FREQ_HZ, duty)
    else:
        write(MOTOR_IN4, 0)
        start_pwm(MOTOR_IN3, MOTOR_PWM_FREQ_HZ, duty)

    motor_output_reverse = reverse


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
    <p><a href="/record?mute=0">Record without amp mute</a></p>
    <p><a href="/record?mic=0">Record without mic switch</a></p>
    <p><a href="/record?mute=0&mic=0&led=0">Record logic only</a></p>

    <h3>Erase</h3>
    <p><a href="/erase/on">Erase ON default</a></p>
    <p><a href="/erase/on?freq=20000">Erase ON 20 kHz</a></p>
    <p><a href="/erase/on?freq=30000">Erase ON 30 kHz</a></p>
    <p><a href="/erase/on?freq=40000">Erase ON 40 kHz</a></p>
    <p><a href="/erase/on?freq=50000">Erase ON 50 kHz</a></p>
    <p><a href="/erase/off">Erase OFF</a></p>

    <h3>Motor</h3>
    <p><a href="/motor?speed=0">Motor stop</a></p>
    <p><a href="/motor?speed=180">Motor speed 180</a></p>
    <p><a href="/motor?speed=255">Motor max</a></p>
    <p><a href="/reverse/on">Reverse ON</a></p>
    <p><a href="/reverse/off">Reverse OFF</a></p>

    <h3>Debug</h3>
    <p><a href="/status">Status</a></p>
    <p><a href="/debug/motor/reapply">Debug motor reapply</a></p>
    <p><a href="/debug/amp/on">Debug amp ON</a></p>
    <p><a href="/debug/amp/off">Debug amp OFF</a></p>
    <p><a href="/debug/mic/play">Debug mic PLAY path</a></p>
    <p><a href="/debug/mic/record">Debug mic RECORD path</a></p>
    <p><a href="/debug/led/on">Debug record LED ON</a></p>
    <p><a href="/debug/led/off">Debug record LED OFF</a></p>
    """


@app.get("/status")
def route_status():
    debug("HTTP /status")
    return jsonify(state)


@app.get("/ping")
def route_ping():
    return "pong\n", 200, {"Content-Type": "text/plain"}


@app.route("/audio/devices", methods=["GET"])
def route_audio_devices():
    debug("HTTP /audio/devices")

    try:
        return jsonify(list_alsa_inputs())

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "default_device": AUDIO_DEVICE,
        }), 500


@app.route("/audio/stream", methods=["GET"])
def route_audio_stream():
    if not find_arecord():
        return jsonify({
            "ok": False,
            "error": "arecord not found. Install alsa-utils on the Raspberry Pi.",
        }), 500

    try:
        device = choose_alsa_capture_device(request.values.get("device", AUDIO_DEVICE))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    rate = int(request.values.get("rate", AUDIO_RATE))
    channels = int(request.values.get("channels", AUDIO_CHANNELS))

    rate = int(clamp(rate, 8000, 96000))
    channels = int(clamp(channels, 1, 2))

    debug(
        f"HTTP /audio/stream device={device} "
        f"rate={rate} channels={channels}"
    )

    cmd = [
        "arecord",
        "-q",
        "-D", device,
        "-f", AUDIO_FORMAT,
        "-r", str(rate),
        "-c", str(channels),
        "-t", "raw",
    ]

    def generate():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        debug(f"Audio stream started with PID {proc.pid}")

        try:
            while True:
                chunk = proc.stdout.read(4096)

                if not chunk:
                    break

                yield chunk

        except GeneratorExit:
            debug("Audio stream client disconnected")

        finally:
            if proc.poll() is None:
                proc.terminate()

            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

            stderr = proc.stderr.read().decode(errors="replace").strip()

            if stderr:
                debug(f"Audio stream arecord stderr: {stderr}")

            debug(f"Audio stream stopped with exit {proc.returncode}")

    return Response(
        stream_with_context(generate()),
        mimetype="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/audio/record", methods=["GET"])
def route_audio_record():
    debug("HTTP /audio/record")

    try:
        seconds = request.values.get("seconds", DEFAULT_AUDIO_SECONDS)
        samplerate = request.values.get("samplerate", AUDIO_RATE)
        channels = request.values.get("channels", AUDIO_CHANNELS)
        device = request.values.get("device", AUDIO_DEVICE)

        wav_bytes = record_audio_wav_with_arecord(seconds, samplerate, channels, device)

        return Response(
            wav_bytes,
            mimetype="audio/wav",
            headers={
                "Content-Disposition": "attachment; filename=recorder_capture.wav",
            },
        )

    except Exception as e:
        debug(f"Audio capture error: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/debug/motor/reapply", methods=["GET", "POST"])
def route_debug_motor_reapply():
    debug("HTTP /debug/motor/reapply")

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    if state["motor_speed"] == 0:
        state["motor_speed"] = DEFAULT_MOTOR_SPEED

    apply_motor()
    return jsonify(state)


@app.route("/debug/amp/on", methods=["GET", "POST"])
def route_debug_amp_on():
    debug("HTTP /debug/amp/on")
    write(AMP_ON, 1)
    apply_motor()
    return jsonify(state)


@app.route("/debug/amp/off", methods=["GET", "POST"])
def route_debug_amp_off():
    debug("HTTP /debug/amp/off")
    write(AMP_ON, 0)
    apply_motor()
    return jsonify(state)


@app.route("/debug/mic/play", methods=["GET", "POST"])
def route_debug_mic_play():
    debug("HTTP /debug/mic/play")
    write(MIC_SW, 1)
    apply_motor()
    return jsonify(state)


@app.route("/debug/mic/record", methods=["GET", "POST"])
def route_debug_mic_record():
    debug("HTTP /debug/mic/record")
    write(MIC_SW, 0)
    apply_motor()
    return jsonify(state)


@app.route("/debug/led/on", methods=["GET", "POST"])
def route_debug_led_on():
    debug("HTTP /debug/led/on")
    write(RECORD_LED, 0)
    apply_motor()
    return jsonify(state)


@app.route("/debug/led/off", methods=["GET", "POST"])
def route_debug_led_off():
    debug("HTTP /debug/led/off")
    write(RECORD_LED, 1)
    apply_motor()
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
    mute = parse_bool(request.values.get("mute"), True)
    mic = parse_bool(request.values.get("mic"), True)
    led = parse_bool(request.values.get("led"), True)
    set_record(mute_amp=mute, connect_mic=mic, record_led=led)
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
        state["motor_speed"] = normalize_motor_speed(speed)

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
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
