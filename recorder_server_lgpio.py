#!/usr/bin/env python3

import atexit
import array
import re
import shutil
import socket
import subprocess
import threading
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
RECORD_LED = 22    # HIGH = LED on; blinks while in record mode

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
MIN_MOTOR_SPEED = 0
DEFAULT_MOTOR_PWM_FREQ_HZ = 1000
MOTOR_DRIVE_MODE = "slow_decay"  # "slow_decay" is usually smoother on DRV8833.

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
record_led_thread = None
record_led_stop_event = threading.Event()
local_monitor = None
local_monitor_lock = threading.Lock()

state = {
    "recorder_enabled": False,
    "mode": "play",            # "play" or "record"
    "erase": False,
    "erase_freq_hz": DEFAULT_ERASE_FREQ_HZ,
    "motor_speed": 0,          # 0–255
    "motor_reverse": False,
    "motor_pwm_freq_hz": DEFAULT_MOTOR_PWM_FREQ_HZ,
    "record_led": False,
    "record_led_blinking": False,
    "local_monitor": {
        "running": False,
        "input_device": AUDIO_DEVICE,
        "output_device": "default",
        "rate": AUDIO_RATE,
        "channels": AUDIO_CHANNELS,
        "volume": 1.0,
        "muted": False,
        "level": 0.0,
        "bytes": 0,
    },
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
    state["motor_speed"] = speed
    return speed


def normalize_motor_pwm_freq(freq_hz) -> int:
    return int(clamp(int(freq_hz), 50, 20000))


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

    stop_waveform(pin)

    if freq_hz <= 0 or duty_percent <= 0:
        return

    if duty_percent >= 100:
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


def set_record_led(on: bool):
    state["record_led"] = bool(on)
    write(RECORD_LED, 1 if on else 0)


def record_led_blink_worker(stop_event):
    while not stop_event.is_set():
        set_record_led(True)

        if stop_event.wait(1.0):
            break

        set_record_led(False)
        stop_event.wait(1.0)

    set_record_led(False)


def start_record_led_blink():
    global record_led_thread, record_led_stop_event

    if record_led_thread and record_led_thread.is_alive():
        return

    record_led_stop_event = threading.Event()
    state["record_led_blinking"] = True
    record_led_thread = threading.Thread(
        target=record_led_blink_worker,
        args=(record_led_stop_event,),
        daemon=True,
    )
    record_led_thread.start()
    debug(f"Record LED blinking on GPIO {RECORD_LED}")


def stop_record_led_blink():
    global record_led_thread

    record_led_stop_event.set()

    if (
        record_led_thread
        and record_led_thread.is_alive()
        and threading.current_thread() is not record_led_thread
    ):
        record_led_thread.join(timeout=0.2)

    record_led_thread = None
    state["record_led_blinking"] = False
    set_record_led(False)


def claim_outputs():
    pins = [
        (RECORDER_EN, enable_level(False)),
        (AMP_ON, 0),
        (MIC_SW, 1),
        (RECORD_LED, 0),
        (ERASE_IN1, 0),
        (ERASE_IN2, 0),
        (MOTOR_IN3, 0),
        (MOTOR_IN4, 0),
    ]

    for pin, initial_level in pins:
        lgpio.gpio_claim_output(h, pin, initial_level)
        debug(f"Claimed GPIO {pin} as output, initial={initial_level}")


# ============================================================
# AUDIO CAPTURE / STREAMING
# ============================================================

def find_arecord():
    return shutil.which("arecord")


def find_aplay():
    return shutil.which("aplay")


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


def parse_alsa_playback_devices(aplay_output):
    card_pattern = re.compile(r"^card\s+(\d+):\s+([^\[]+).*device\s+(\d+):\s+([^\[]+)")
    devices = []

    if not aplay_output:
        return devices

    for line in aplay_output.splitlines():
        match = card_pattern.search(line)

        if not match:
            continue

        card, card_name, device, device_name = match.groups()
        device_id = f"plughw:{card},{device}"
        label = f"{card_name.strip()} / {device_name.strip()}"
        devices.append({"id": device_id, "name": label})

    return devices


def alsa_card_id(device_id):
    match = re.match(r"^plughw:(\d+),", str(device_id))
    return match.group(1) if match else None


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


def list_alsa_outputs():
    aplay = find_aplay()

    if not aplay:
        return {
            "backend": "alsa",
            "default_output": AUDIO_DEVICE,
            "outputs": [],
            "error": "aplay not found. Install alsa-utils on the Raspberry Pi.",
        }

    devices = [
        {"id": "default", "name": "ALSA default output"},
        {"id": "auto", "name": "Auto-detect first ALSA playback output"},
    ]

    list_pcms = subprocess.run(
        [aplay, "-L"],
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
        [aplay, "-l"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    for playback_device in parse_alsa_playback_devices(list_cards.stdout):
        if not any(existing["id"] == playback_device["id"] for existing in devices):
            devices.append(playback_device)

    return {
        "backend": "alsa",
        "default_output": AUDIO_DEVICE,
        "outputs": devices,
        "aplay_l": list_cards.stdout.strip(),
        "aplay_L": list_pcms.stdout.strip(),
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


def choose_alsa_playback_device(device=None):
    if device and device not in ["auto"]:
        return device

    aplay = find_aplay()

    if not aplay:
        raise RuntimeError("aplay not found. Install alsa-utils on the Raspberry Pi.")

    result = subprocess.run(
        [aplay, "-l"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    devices = parse_alsa_playback_devices(result.stdout)

    arecord = find_arecord()

    if arecord:
        capture_result = subprocess.run(
            [arecord, "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        capture_devices = parse_alsa_capture_devices(capture_result.stdout)

        if capture_devices:
            capture_card = alsa_card_id(capture_devices[0]["id"])

            for playback_device in devices:
                if alsa_card_id(playback_device["id"]) == capture_card:
                    chosen = playback_device["id"]
                    debug(
                        "ALSA auto-selected playback output matching "
                        f"capture card: {chosen} ({playback_device['name']})"
                    )
                    return chosen

    if devices:
        chosen = devices[0]["id"]
        debug(f"ALSA auto-selected playback output: {chosen} ({devices[0]['name']})")
        return chosen

    debug("No hardware ALSA playback output found; falling back to ALSA default")
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


def pcm_samples(chunk):
    if not chunk:
        return array.array("h")

    if len(chunk) % 2:
        chunk = chunk[:-1]

    samples = array.array("h")
    samples.frombytes(chunk)
    return samples


def pcm_level(chunk):
    samples = pcm_samples(chunk)

    if not samples:
        return 0.0

    square_sum = sum(sample * sample for sample in samples)
    rms = (square_sum / len(samples)) ** 0.5
    return min(1.0, rms / 12000.0)


def apply_pcm_volume(chunk, volume, muted=False):
    if muted:
        return b"\x00" * len(chunk)

    volume = float(clamp(float(volume), 0.0, 4.0))

    if abs(volume - 1.0) < 0.001:
        return chunk

    samples = pcm_samples(chunk)

    for index, sample in enumerate(samples):
        samples[index] = int(clamp(sample * volume, -32768, 32767))

    return samples.tobytes()


def choose_local_monitor_output_device(device=None):
    if device and device not in ["auto"]:
        return device

    return "default"


def set_local_monitor_volume(volume=None, muted=None):
    with local_monitor_lock:
        monitor_state = state["local_monitor"]

        if volume is not None:
            monitor_state["volume"] = float(clamp(float(volume), 0.0, 4.0))

        if muted is not None:
            monitor_state["muted"] = bool(muted)

        return dict(monitor_state)


def local_monitor_worker(capture_cmd, playback_cmd, stop_event):
    capture_proc = None
    playback_proc = None
    total_bytes = 0

    try:
        capture_proc = subprocess.Popen(
            capture_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        playback_proc = subprocess.Popen(
            playback_cmd,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        with local_monitor_lock:
            if local_monitor is not None:
                local_monitor["capture_proc"] = capture_proc
                local_monitor["playback_proc"] = playback_proc

        debug(
            "Local audio monitor started "
            f"capture_pid={capture_proc.pid} playback_pid={playback_proc.pid}"
        )

        while not stop_event.is_set():
            chunk = capture_proc.stdout.read(4096)

            if not chunk:
                break

            with local_monitor_lock:
                monitor_state = state["local_monitor"]
                volume = monitor_state["volume"]
                muted = monitor_state["muted"]

            output_chunk = apply_pcm_volume(chunk, volume, muted)
            playback_proc.stdin.write(output_chunk)
            total_bytes += len(output_chunk)

            with local_monitor_lock:
                state["local_monitor"]["level"] = 0.0 if muted else pcm_level(output_chunk)
                state["local_monitor"]["bytes"] = total_bytes

    except BrokenPipeError:
        debug("Local audio monitor playback pipe closed")

    except Exception as e:
        debug(f"Local audio monitor error: {type(e).__name__}: {e}")

    finally:
        for proc, stream_name in [
            (capture_proc, "capture"),
            (playback_proc, "playback"),
        ]:
            if proc is None:
                continue

            try:
                if stream_name == "playback" and proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

            if proc.poll() is None:
                proc.terminate()

                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()

        with local_monitor_lock:
            state["local_monitor"]["running"] = False
            state["local_monitor"]["level"] = 0.0
            state["local_monitor"]["bytes"] = total_bytes

        debug(f"Local audio monitor stopped, bytes={total_bytes}")


def start_local_monitor(input_device=None, output_device=None, rate=None, channels=None):
    global local_monitor

    if not find_arecord():
        raise RuntimeError("arecord not found. Install alsa-utils on the Raspberry Pi.")

    if not find_aplay():
        raise RuntimeError("aplay not found. Install alsa-utils on the Raspberry Pi.")

    input_device = choose_alsa_capture_device(input_device or AUDIO_DEVICE)
    output_device = choose_local_monitor_output_device(output_device or "default")
    rate = int(clamp(int(rate or AUDIO_RATE), 8000, 96000))
    channels = int(clamp(int(channels or AUDIO_CHANNELS), 1, 2))
    old_monitor = None

    with local_monitor_lock:
        existing = local_monitor

        if existing and existing["thread"].is_alive():
            monitor_state = state["local_monitor"]
            same_route = (
                monitor_state["input_device"] == input_device
                and monitor_state["output_device"] == output_device
                and monitor_state["rate"] == rate
                and monitor_state["channels"] == channels
            )

            if same_route:
                monitor_state["running"] = True
                return dict(monitor_state)

            old_monitor = existing

    if old_monitor:
        old_monitor["stop_event"].set()
        old_monitor["thread"].join(timeout=0.5)

    with local_monitor_lock:
        existing = local_monitor

        if existing and existing["thread"].is_alive():
            state["local_monitor"]["running"] = True
            return dict(state["local_monitor"])

        stop_event = threading.Event()
        capture_cmd = [
            "arecord",
            "-q",
            "-D", input_device,
            "-f", AUDIO_FORMAT,
            "-r", str(rate),
            "-c", str(channels),
            "-t", "raw",
        ]
        playback_cmd = [
            "aplay",
            "-q",
            "-D", output_device,
            "-f", AUDIO_FORMAT,
            "-r", str(rate),
            "-c", str(channels),
            "-t", "raw",
        ]
        thread = threading.Thread(
            target=local_monitor_worker,
            args=(capture_cmd, playback_cmd, stop_event),
            daemon=True,
        )
        local_monitor = {
            "thread": thread,
            "stop_event": stop_event,
            "capture_proc": None,
            "playback_proc": None,
        }
        state["local_monitor"].update({
            "running": True,
            "input_device": input_device,
            "output_device": output_device,
            "rate": rate,
            "channels": channels,
            "level": 0.0,
            "bytes": 0,
        })
        thread.start()
        return dict(state["local_monitor"])


def stop_local_monitor(wait=False):
    global local_monitor

    with local_monitor_lock:
        monitor = local_monitor
        state["local_monitor"]["running"] = False
        state["local_monitor"]["level"] = 0.0

    if monitor:
        monitor["stop_event"].set()

        for proc_name in ["capture_proc", "playback_proc"]:
            proc = monitor.get(proc_name)

            if proc and proc.poll() is None:
                proc.terminate()

        if wait and monitor["thread"].is_alive():
            monitor["thread"].join(timeout=1.0)

    return dict(state["local_monitor"])


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
        stop_record_led_blink()


def ensure_motor_for_record():
    speed = normalize_motor_speed(state["motor_speed"])
    state["motor_speed"] = speed
    debug(f"Record mode leaving motor speed unchanged at {speed}")


def set_record(mute_amp=True, connect_mic=True, record_led=True):
    debug(
        f"Set mode: RECORD mute_amp={mute_amp} "
        f"connect_mic={connect_mic} record_led={record_led}"
    )

    stop_local_monitor(wait=True)

    if not state["recorder_enabled"]:
        set_recorder_power(True)
        time.sleep(0.2)

    state["mode"] = "record"

    if record_led:
        start_record_led_blink()
    else:
        stop_record_led_blink()

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
    stop_record_led_blink()

    write(AMP_ON, 0)
    write(MIC_SW, 1)
    time.sleep(0.05)

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
        f"reverse={reverse}, drive={MOTOR_DRIVE_MODE}, "
        f"freq={state['motor_pwm_freq_hz']} Hz"
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

    if MOTOR_DRIVE_MODE == "slow_decay":
        brake_duty = 100.0 - duty

        if reverse:
            write(MOTOR_IN4, 1)
            start_pwm(MOTOR_IN3, state["motor_pwm_freq_hz"], brake_duty)
        else:
            write(MOTOR_IN3, 1)
            start_pwm(MOTOR_IN4, state["motor_pwm_freq_hz"], brake_duty)

    else:
        if reverse:
            write(MOTOR_IN3, 0)
            start_pwm(MOTOR_IN4, state["motor_pwm_freq_hz"], duty)
        else:
            write(MOTOR_IN4, 0)
            start_pwm(MOTOR_IN3, state["motor_pwm_freq_hz"], duty)

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
    stop_record_led_blink()

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
        stop_local_monitor()
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
        stop_record_led_blink()
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
    <p><a href="/record?mute=0&mic=0">Record logic only</a></p>

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
    <p><a href="/debug/record-led/on">Debug record LED ON</a></p>
    <p><a href="/debug/record-led/off">Debug record LED OFF</a></p>
    <p><a href="/audio/local-monitor/start">Audio local monitor START</a></p>
    <p><a href="/audio/local-monitor/stop">Audio local monitor STOP</a></p>
    <p><a href="/audio/local-monitor/status">Audio local monitor STATUS</a></p>
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
        data = list_alsa_inputs()
        data.update(list_alsa_outputs())
        return jsonify(data)

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


@app.route("/audio/playback", methods=["POST"])
def route_audio_playback():
    if not find_aplay():
        return jsonify({
            "ok": False,
            "error": "aplay not found. Install alsa-utils on the Raspberry Pi.",
        }), 500

    stop_local_monitor(wait=True)

    try:
        device = choose_alsa_playback_device(request.args.get("device", AUDIO_DEVICE))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    rate = int(request.args.get("rate", AUDIO_RATE))
    channels = int(request.args.get("channels", AUDIO_CHANNELS))

    rate = int(clamp(rate, 8000, 96000))
    channels = int(clamp(channels, 1, 2))

    debug(
        f"HTTP /audio/playback device={device} "
        f"rate={rate} channels={channels}"
    )

    cmd = [
        "aplay",
        "-q",
        "-D", device,
        "-f", AUDIO_FORMAT,
        "-r", str(rate),
        "-c", str(channels),
        "-t", "raw",
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    debug(f"Audio playback started with PID {proc.pid}")
    total_bytes = 0

    try:
        while True:
            chunk = request.stream.read(4096)

            if not chunk:
                break

            proc.stdin.write(chunk)
            total_bytes += len(chunk)

    except BrokenPipeError:
        debug("Audio playback aplay pipe closed")

    except Exception as e:
        debug(f"Audio playback stream error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()

            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

        stderr = ""

        try:
            if proc.stderr:
                stderr = proc.stderr.read().decode(errors="replace").strip()
        except Exception:
            stderr = ""

        if stderr:
            debug(f"Audio playback aplay stderr: {stderr}")

        debug(
            f"Audio playback stopped with exit {proc.returncode}, "
            f"bytes={total_bytes}"
        )

    if proc.returncode not in [0, None]:
        return jsonify({
            "ok": False,
            "exit": proc.returncode,
            "stderr": stderr,
            "bytes": total_bytes,
        }), 500

    return jsonify({
        "ok": True,
        "device": device,
        "rate": rate,
        "channels": channels,
        "bytes": total_bytes,
    })


@app.route("/audio/local-monitor/start", methods=["GET", "POST"])
def route_audio_local_monitor_start():
    debug("HTTP /audio/local-monitor/start")

    try:
        volume = request.values.get("volume")
        muted = request.values.get("mute")

        set_local_monitor_volume(
            volume=volume if volume is not None else None,
            muted=parse_bool(muted, False) if muted is not None else None,
        )
        monitor_state = start_local_monitor(
            input_device=request.values.get("input", request.values.get("device", AUDIO_DEVICE)),
            output_device=request.values.get("output", "default"),
            rate=request.values.get("rate", AUDIO_RATE),
            channels=request.values.get("channels", AUDIO_CHANNELS),
        )
        return jsonify({
            "ok": True,
            "local_monitor": monitor_state,
            **state,
        })

    except Exception as e:
        debug(f"Local monitor start error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": str(e), **state}), 500


@app.route("/audio/local-monitor/stop", methods=["GET", "POST"])
def route_audio_local_monitor_stop():
    debug("HTTP /audio/local-monitor/stop")
    monitor_state = stop_local_monitor()
    return jsonify({
        "ok": True,
        "local_monitor": monitor_state,
        **state,
    })


@app.route("/audio/local-monitor/volume", methods=["GET", "POST"])
def route_audio_local_monitor_volume():
    debug("HTTP /audio/local-monitor/volume")

    volume = request.values.get("volume")
    muted = request.values.get("mute")
    monitor_state = set_local_monitor_volume(
        volume=volume if volume is not None else None,
        muted=parse_bool(muted, False) if muted is not None else None,
    )

    return jsonify({
        "ok": True,
        "local_monitor": monitor_state,
        **state,
    })


@app.route("/audio/local-monitor/status", methods=["GET"])
def route_audio_local_monitor_status():
    debug("HTTP /audio/local-monitor/status")

    with local_monitor_lock:
        monitor_state = dict(state["local_monitor"])

    return jsonify({
        "ok": True,
        "local_monitor": monitor_state,
        **state,
    })


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


@app.route("/debug/record-led/on", methods=["GET", "POST"])
def route_debug_record_led_on():
    debug("HTTP /debug/record-led/on")
    stop_record_led_blink()
    set_record_led(True)
    apply_motor()
    return jsonify(state)


@app.route("/debug/record-led/off", methods=["GET", "POST"])
def route_debug_record_led_off():
    debug("HTTP /debug/record-led/off")
    stop_record_led_blink()
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
    freq = request.values.get("freq", request.values.get("hz"))

    if speed is not None:
        state["motor_speed"] = normalize_motor_speed(speed)

    if reverse is not None:
        state["motor_reverse"] = reverse.lower() in ["1", "true", "yes", "on"]

    if freq is not None:
        state["motor_pwm_freq_hz"] = normalize_motor_pwm_freq(freq)

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
