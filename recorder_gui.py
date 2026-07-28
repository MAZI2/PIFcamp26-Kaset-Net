#!/usr/bin/env python3

import array
import json
import math
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf


SERVICE_TYPE = "_recorder._tcp.local."
MIN_MOTOR_SPEED = 0
ERASE_FREQ_OPTIONS = ("20000", "30000", "40000", "50000")
RECORD_PATH = "/record"
REQUEST_TIMEOUT = 3.0
AUDIO_RATE = 44100
AUDIO_CHANNELS = 1
AUDIO_CHUNK_BYTES = 4096
AUDIO_SOURCE_BUFFER_MAX_BYTES = AUDIO_RATE * AUDIO_CHANNELS * 2
AUDIO_LEVEL_DOTS = 12


def clamp(value, low, high):
    return max(low, min(high, value))


# ============================================================
# DISCOVERY
# ============================================================

class RecorderDiscovery(ServiceListener):
    def __init__(self, event_queue):
        self.event_queue = event_queue

    def add_service(self, zeroconf, service_type, name):
        self.event_queue.put(("debug", f"[DISCOVERY] Service added: {name}"))

        info = zeroconf.get_service_info(service_type, name)
        if not info:
            self.event_queue.put(("debug", f"[DISCOVERY] No info for {name}"))
            return

        addresses = info.parsed_scoped_addresses()
        if not addresses:
            self.event_queue.put(("debug", f"[DISCOVERY] No IP address for {name}"))
            return

        ip = addresses[0]
        port = info.port

        props = {}
        for key, value in info.properties.items():
            try:
                key_decoded = key.decode() if isinstance(key, bytes) else str(key)
                value_decoded = value.decode() if isinstance(value, bytes) else str(value)
                props[key_decoded] = value_decoded
            except Exception:
                props[str(key)] = str(value)

        device = {
            "name": name,
            "ip": ip,
            "port": port,
            "url": f"http://{ip}:{port}",
            "properties": props,
        }

        self.event_queue.put(("device_added", device))

    def update_service(self, zeroconf, service_type, name):
        self.event_queue.put(("debug", f"[DISCOVERY] Service updated: {name}"))
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        self.event_queue.put(("debug", f"[DISCOVERY] Service removed: {name}"))
        self.event_queue.put(("device_removed", name))


# ============================================================
# GUI
# ============================================================

class RecorderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Cassette Recorder Controller")
        self.root.geometry("980x720")

        self.event_queue = queue.Queue()
        self.devices = {}
        self.mixer_running = False
        self.mixer_process = None
        self.mixer_stop_event = threading.Event()
        self.mixer_thread = None
        self.mixer_sources = {}
        self.mixer_rows = {}
        self.mixer_lock = threading.Lock()

        self.manual_host = tk.StringVar(value="192.168.0.9")
        self.audio_device = tk.StringVar(value="auto")
        self.erase_freq = tk.StringVar(value=ERASE_FREQ_OPTIONS[0])

        self.motor_speed = tk.IntVar(value=MIN_MOTOR_SPEED)
        self.zeroconf = None
        self.listener = None
        self.browser = None

        self.configure_style()
        self.build_ui()

        self.log("[INIT] GUI started")
        self.log("[INIT] Add recorders manually, or press Find to browse with mDNS.")

        self.root.after(100, self.process_events)
        self.root.after(100, self.update_mixer_meters)

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def configure_style(self):
        self.root.configure(bg="white")

        style = ttk.Style(self.root)

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background="white", foreground="#111111")
        style.configure("TFrame", background="white")
        style.configure("TLabelframe", background="white")
        style.configure("TLabelframe.Label", background="white", foreground="#111111")
        style.configure("TLabel", background="white", foreground="#111111")
        style.configure("TCheckbutton", background="white", foreground="#111111")
        style.configure("TButton", padding=(8, 4))
        style.configure("Horizontal.TScale", background="white")

    def build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Discovery section
        discovery_frame = ttk.LabelFrame(main, text="Discovered recorders", padding=10)
        discovery_frame.pack(fill=tk.X)

        list_frame = ttk.Frame(discovery_frame)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        self.device_list = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            height=5,
            exportselection=False,
            bg="white",
            fg="#111111",
            selectbackground="#dcecff",
            selectforeground="#111111",
            highlightthickness=1,
            highlightbackground="#d7d7d7",
            relief=tk.FLAT,
        )
        self.device_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        device_scroll = ttk.Scrollbar(
            list_frame,
            orient=tk.VERTICAL,
            command=self.device_list.yview,
        )
        device_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.device_list.configure(yscrollcommand=device_scroll.set)

        ttk.Button(
            discovery_frame,
            text="Find",
            command=self.start_discovery,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            discovery_frame,
            text="Select All",
            command=self.select_all_devices,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            discovery_frame,
            text="Status Selected",
            command=self.status,
        ).pack(side=tk.LEFT, padx=3)

        # Manual host
        manual_frame = ttk.LabelFrame(main, text="Add recorder by IP or URL", padding=10)
        manual_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Entry(
            manual_frame,
            textvariable=self.manual_host,
            width=95,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(
            manual_frame,
            text="Add IP",
            command=self.use_manual_host,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            manual_frame,
            text="Add + Test",
            command=self.test_manual_host,
        ).pack(side=tk.LEFT, padx=3)

        # Power / mode commands
        power_frame = ttk.LabelFrame(main, text="Power and mode", padding=10)
        power_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(power_frame, text="Power ON", command=lambda: self.command("/power/on")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Power OFF", command=lambda: self.command("/power/off")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Play", command=lambda: self.command("/play")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Record", command=lambda: self.command(RECORD_PATH)).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Status", command=lambda: self.command("/status")).pack(side=tk.LEFT, padx=3)

        # Erase commands
        erase_frame = ttk.LabelFrame(main, text="Erase", padding=10)
        erase_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(erase_frame, text="Freq:").pack(side=tk.LEFT, padx=3)

        ttk.Combobox(
            erase_frame,
            textvariable=self.erase_freq,
            values=ERASE_FREQ_OPTIONS,
            width=8,
            state="readonly",
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(erase_frame, text="Erase ON", command=self.erase_on).pack(side=tk.LEFT, padx=3)
        ttk.Button(erase_frame, text="Erase OFF", command=lambda: self.command("/erase/off")).pack(side=tk.LEFT, padx=3)

        # Motor commands
        motor_frame = ttk.LabelFrame(main, text="Motor", padding=10)
        motor_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(motor_frame, text="Speed:").pack(side=tk.LEFT, padx=3)

        self.speed_slider = ttk.Scale(
            motor_frame,
            from_=MIN_MOTOR_SPEED,
            to=255,
            orient=tk.HORIZONTAL,
            command=self.on_speed_slider,
        )
        self.speed_label = ttk.Label(motor_frame, text=str(self.motor_speed.get()), width=4)

        self.speed_slider.set(self.motor_speed.get())
        self.speed_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)
        self.speed_label.pack(side=tk.LEFT, padx=3)

        ttk.Button(motor_frame, text="Apply Speed", command=self.apply_motor_speed).pack(side=tk.LEFT, padx=3)
        ttk.Button(motor_frame, text="Stop", command=lambda: self.command("/stop")).pack(side=tk.LEFT, padx=3)

        # Direction commands
        direction_frame = ttk.LabelFrame(main, text="Direction", padding=10)
        direction_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(direction_frame, text="Forward", command=lambda: self.command("/reverse/off")).pack(side=tk.LEFT, padx=3)
        ttk.Button(direction_frame, text="Reverse", command=lambda: self.command("/reverse/on")).pack(side=tk.LEFT, padx=3)
        ttk.Button(direction_frame, text="Forward + Speed", command=self.motor_forward).pack(side=tk.LEFT, padx=3)
        ttk.Button(direction_frame, text="Reverse + Speed", command=self.motor_reverse).pack(side=tk.LEFT, padx=3)

        # Audio mixer
        audio_frame = ttk.LabelFrame(main, text="Audio mixer", padding=10)
        audio_frame.pack(fill=tk.X, pady=(10, 0))

        audio_controls = ttk.Frame(audio_frame)
        audio_controls.pack(fill=tk.X)

        ttk.Label(audio_controls, text="ALSA device:").pack(side=tk.LEFT, padx=3)

        ttk.Entry(
            audio_controls,
            textvariable=self.audio_device,
            width=16,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(audio_controls, text="Start Mixer", command=self.start_audio_monitor).pack(side=tk.LEFT, padx=3)
        ttk.Button(audio_controls, text="Stop Mixer", command=self.stop_audio_monitor).pack(side=tk.LEFT, padx=3)
        ttk.Button(audio_controls, text="List Devices", command=lambda: self.command("/audio/devices")).pack(side=tk.LEFT, padx=3)

        self.mixer_rows_frame = ttk.Frame(audio_frame)
        self.mixer_rows_frame.pack(fill=tk.X, pady=(8, 0))

        # Quick command entry
        custom_frame = ttk.LabelFrame(main, text="Custom endpoint", padding=10)
        custom_frame.pack(fill=tk.X, pady=(10, 0))

        self.custom_path = tk.StringVar(value="/status")

        ttk.Entry(
            custom_frame,
            textvariable=self.custom_path,
            width=80,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(
            custom_frame,
            text="Send",
            command=self.send_custom,
        ).pack(side=tk.LEFT, padx=3)

        # Debug log
        log_frame = ttk.LabelFrame(main, text="Debug log", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.log_box = scrolledtext.ScrolledText(
            log_frame,
            height=14,
            wrap=tk.WORD,
            bg="white",
            fg="#111111",
            insertbackground="#111111",
        )
        self.log_box.pack(fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(bottom, text="Clear Log", command=self.clear_log).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Quit", command=self.quit).pack(side=tk.RIGHT)

    # --------------------------------------------------------
    # LOGGING / EVENTS
    # --------------------------------------------------------

    def log(self, message):
        self.log_box.insert(tk.END, message + "\n")
        self.log_box.see(tk.END)
        print(message)

    def clear_log(self):
        self.log_box.delete("1.0", tk.END)

    def start_discovery(self):
        if self.browser is not None:
            self.log("[DISCOVERY] Already browsing")
            return

        self.log(f"[DISCOVERY] Browsing for mDNS service: {SERVICE_TYPE}")
        self.zeroconf = Zeroconf()
        self.listener = RecorderDiscovery(self.event_queue)
        self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, self.listener)

    def process_events(self):
        try:
            while True:
                event, payload = self.event_queue.get_nowait()

                if event == "debug":
                    self.log(payload)

                elif event == "device_added":
                    existing = self.devices.get(payload["url"], {})
                    payload["transport_mode"] = existing.get("transport_mode", "play")
                    payload["erase_active"] = existing.get("erase_active", False)
                    self.devices[payload["url"]] = payload
                    self.log(
                        f"[DISCOVERY] Found recorder: {payload['name']} "
                        f"at {payload['url']} props={payload['properties']}"
                    )
                    self.refresh_device_list()
                    self.sync_mixer_sources()

                elif event == "device_removed":
                    removed_urls = [
                        url for url, dev in self.devices.items()
                        if dev["name"] == payload
                    ]

                    for url in removed_urls:
                        removed = self.devices.pop(url)
                        self.log(f"[DISCOVERY] Removed recorder: {payload} at {removed['url']}")

                    if removed_urls:
                        self.refresh_device_list()
                        self.sync_mixer_sources()

                elif event == "status_update":
                    base_url, data = payload
                    self.update_device_from_status(base_url, data)

                elif event == "mixer_error":
                    self.log(payload)

                elif event == "mixer_stopped":
                    self.mixer_running = False
                    self.log(payload)

        except queue.Empty:
            pass

        self.root.after(100, self.process_events)

    def displayed_device_state(self, dev):
        if dev.get("erase_active"):
            return "erase"

        return dev.get("transport_mode", "play")

    def device_label(self, dev):
        return f"{dev['url']}  |  {dev['name']}  |  state: {self.displayed_device_state(dev)}"

    def refresh_device_list(self):
        selected_urls = set(self.get_selected_base_urls(allow_empty=True))

        self.device_list.delete(0, tk.END)

        sorted_devices = sorted(self.devices.values(), key=lambda dev: dev["url"])

        for dev in sorted_devices:
            self.device_list.insert(tk.END, self.device_label(dev))

        for index, dev in enumerate(sorted_devices):
            if dev["url"] in selected_urls or not selected_urls:
                self.device_list.selection_set(index)

        if sorted_devices:
            self.log(f"[TARGETS] {len(sorted_devices)} recorder(s) available; selected {len(self.device_list.curselection())}")

    def select_all_devices(self):
        self.device_list.selection_set(0, tk.END)
        self.log(f"[TARGETS] Selected {len(self.device_list.curselection())} recorder(s)")

    def update_device_state(self, base_url, transport_mode=None, erase_active=None):
        dev = self.devices.get(base_url)

        if not dev:
            return

        if transport_mode in ["play", "record"]:
            dev["transport_mode"] = transport_mode

        if erase_active is not None:
            dev["erase_active"] = bool(erase_active)

    def update_device_from_status(self, base_url, data):
        if not isinstance(data, dict):
            return

        mode = data.get("mode")
        erase = data.get("erase")

        self.update_device_state(
            base_url,
            transport_mode=mode if mode in ["play", "record"] else None,
            erase_active=erase if erase is not None else None,
        )
        self.refresh_device_list()
        self.sync_mixer_sources()

    def apply_command_state(self, base_urls, path):
        command_path = urlsplit(path).path

        transport_mode = None
        erase_active = None

        if command_path == "/play":
            transport_mode = "play"
        elif command_path == "/record":
            transport_mode = "record"
        elif command_path == "/erase/on":
            erase_active = True
        elif command_path == "/erase/off":
            erase_active = False

        if transport_mode is None and erase_active is None:
            return

        for base_url in base_urls:
            self.update_device_state(base_url, transport_mode, erase_active)

        self.refresh_device_list()
        self.sync_mixer_sources()

    # --------------------------------------------------------
    # URL / REQUEST HELPERS
    # --------------------------------------------------------

    def normalize_url(self, url):
        url = url.strip().rstrip("/")

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url

        parts = urlsplit(url)

        if parts.scheme == "http" and parts.hostname and parts.port is None:
            netloc = f"{parts.hostname}:5000"

            if parts.username:
                auth = parts.username

                if parts.password:
                    auth += f":{parts.password}"

                netloc = f"{auth}@{netloc}"

            url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

        return url

    def get_selected_base_urls(self, allow_empty=False):
        urls = []

        for index in self.device_list.curselection():
            label = self.device_list.get(index)
            urls.append(label.split("|")[0].strip().rstrip("/"))

        if urls or allow_empty:
            return urls

        raise RuntimeError("No recorder selected. Add/select at least one recorder.")

    def get_primary_base_url(self):
        urls = self.get_selected_base_urls()
        return urls[0]

    def add_manual_host(self):
        url = self.normalize_url(self.manual_host.get())

        dev = {
            "name": "Manual recorder",
            "ip": url,
            "port": "",
            "url": url,
            "properties": {"source": "manual"},
            "transport_mode": "play",
            "erase_active": False,
        }

        self.devices[url] = dev
        self.refresh_device_list()

        for index in range(self.device_list.size()):
            if self.device_list.get(index).startswith(url):
                self.device_list.selection_set(index)
                break

        return url

    def use_manual_host(self):
        url = self.add_manual_host()
        self.log(f"[MANUAL] Added recorder target: {url}")

    def test_manual_host(self):
        self.use_manual_host()
        self.command("/status")

    def request_async(self, path):
        try:
            base_urls = self.get_selected_base_urls()
        except Exception as e:
            self.log(f"[ERROR] {type(e).__name__}: {e}")
            return

        self.apply_command_state(base_urls, path)

        thread = threading.Thread(
            target=self._request_group_worker,
            args=(path, base_urls),
            daemon=True,
        )
        thread.start()

    def build_url_for(self, base_url, path):
        if not path.startswith("/"):
            path = "/" + path

        return base_url.rstrip("/") + path

    def build_url(self, path):
        return self.build_url_for(self.get_primary_base_url(), path)

    def _request_one(self, base_url, path, start_event):
        start_event.wait()
        url = self.build_url_for(base_url, path)

        try:
            self.event_queue.put(("debug", f"[REQUEST] GET {url}"))

            response = requests.get(url, timeout=REQUEST_TIMEOUT)

            self.event_queue.put(("debug", f"[RESPONSE] HTTP {response.status_code} from {url}"))

            try:
                data = response.json()
                pretty = json.dumps(data, indent=2, sort_keys=True)
                self.event_queue.put(("debug", f"[JSON]\n{pretty}"))
                self.event_queue.put(("status_update", (base_url, data)))
            except Exception:
                text = response.text[:2000]
                self.event_queue.put(("debug", f"[TEXT]\n{text}"))

        except Exception as e:
            self.event_queue.put(("debug", f"[ERROR] {url} {type(e).__name__}: {e}"))

    def _request_group_worker(self, path, base_urls):
        self.event_queue.put(("debug", f"[GROUP] GET {path} -> {len(base_urls)} recorder(s)"))

        start_event = threading.Event()
        workers = []

        for base_url in base_urls:
            worker = threading.Thread(
                target=self._request_one,
                args=(base_url, path, start_event),
                daemon=True,
            )
            workers.append(worker)
            worker.start()

        start_event.set()

        for worker in workers:
            worker.join()

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    def command(self, path):
        self.request_async(path)

    def status(self):
        self.command("/status")

    def erase_on(self):
        self.command(f"/erase/on?freq={self.erase_freq.get()}")

    def on_speed_slider(self, value):
        speed = max(MIN_MOTOR_SPEED, int(float(value)))
        self.motor_speed.set(speed)

        if hasattr(self, "speed_label"):
            self.speed_label.config(text=str(speed))

    def apply_motor_speed(self):
        speed = self.motor_speed.get()
        self.command(f"/motor?speed={speed}")

    def motor_forward(self):
        speed = self.motor_speed.get()
        self.command(f"/motor?speed={speed}&reverse=0")

    def motor_reverse(self):
        speed = self.motor_speed.get()
        self.command(f"/motor?speed={speed}&reverse=1")

    def playable_mixer_urls(self):
        return [
            url for url, dev in sorted(self.devices.items())
            if self.displayed_device_state(dev) == "play"
        ]

    def choose_playback_command(self):
        aplay = shutil.which("aplay")
        ffplay = shutil.which("ffplay")

        if sys.platform.startswith("linux") and aplay:
            return "aplay", [
                aplay,
                "-q",
                "-t", "raw",
                "-f", "S16_LE",
                "-r", str(AUDIO_RATE),
                "-c", str(AUDIO_CHANNELS),
            ]

        if ffplay:
            return "ffplay", [
                ffplay,
                "-nodisp",
                "-loglevel", "warning",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-f", "s16le",
                "-ar", str(AUDIO_RATE),
                "-ac", str(AUDIO_CHANNELS),
                "-i", "pipe:0",
            ]

        if aplay:
            return "aplay", [
                aplay,
                "-q",
                "-t", "raw",
                "-f", "S16_LE",
                "-r", str(AUDIO_RATE),
                "-c", str(AUDIO_CHANNELS),
            ]

        raise RuntimeError("No audio player found. Install ffmpeg/ffplay on macOS or alsa-utils/aplay on Linux.")

    def create_mixer_row(self, base_url, source):
        row = ttk.Frame(self.mixer_rows_frame)
        row.pack(fill=tk.X, pady=2)

        ttk.Label(row, text=base_url, width=34).pack(side=tk.LEFT, padx=3)

        canvas = tk.Canvas(
            row,
            width=112,
            height=18,
            bg="white",
            highlightthickness=0,
        )
        canvas.pack(side=tk.LEFT, padx=8)

        volume_var = tk.DoubleVar(value=source["volume"])
        mute_var = tk.BooleanVar(value=source["mute"])

        def on_volume(value, url=base_url):
            with self.mixer_lock:
                if url in self.mixer_sources:
                    self.mixer_sources[url]["volume"] = float(value)

        def on_mute(url=base_url, var=mute_var):
            with self.mixer_lock:
                if url in self.mixer_sources:
                    self.mixer_sources[url]["mute"] = bool(var.get())

        ttk.Scale(
            row,
            from_=0.0,
            to=1.5,
            orient=tk.HORIZONTAL,
            variable=volume_var,
            command=on_volume,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)

        ttk.Checkbutton(row, text="Mute", variable=mute_var, command=on_mute).pack(side=tk.LEFT, padx=3)

        self.mixer_rows[base_url] = {
            "row": row,
            "canvas": canvas,
            "volume_var": volume_var,
            "mute_var": mute_var,
        }

    def start_mixer_source(self, base_url, source):
        thread = source.get("thread")

        if thread and thread.is_alive():
            return

        source["device"] = self.audio_device.get().strip() or "default"
        source["stop_event"] = threading.Event()
        source["queue"] = queue.Queue(maxsize=64)
        source["buffer"] = bytearray()

        thread = threading.Thread(
            target=self._audio_stream_worker,
            args=(base_url, source),
            daemon=True,
        )
        source["thread"] = thread
        thread.start()

    def stop_mixer_source(self, source):
        stop_event = source.get("stop_event")

        if stop_event:
            stop_event.set()

        source["level"] = 0.0

    def sync_mixer_sources(self):
        if not hasattr(self, "mixer_rows_frame"):
            return

        playable_urls = set(self.playable_mixer_urls())

        with self.mixer_lock:
            for base_url in list(self.mixer_sources):
                if base_url not in playable_urls:
                    self.stop_mixer_source(self.mixer_sources[base_url])
                    del self.mixer_sources[base_url]

            for base_url in playable_urls:
                if base_url not in self.mixer_sources:
                    self.mixer_sources[base_url] = {
                        "url": base_url,
                        "queue": queue.Queue(maxsize=64),
                        "buffer": bytearray(),
                        "stop_event": threading.Event(),
                        "thread": None,
                        "volume": 1.0,
                        "mute": False,
                        "level": 0.0,
                    }

        for base_url in list(self.mixer_rows):
            if base_url not in playable_urls:
                self.mixer_rows[base_url]["row"].destroy()
                del self.mixer_rows[base_url]

        with self.mixer_lock:
            current_sources = list(self.mixer_sources.items())

        for base_url, source in current_sources:
            if base_url not in self.mixer_rows:
                self.create_mixer_row(base_url, source)

            if self.mixer_running:
                self.start_mixer_source(base_url, source)

    def start_audio_monitor(self):
        if self.mixer_running:
            self.log("[AUDIO] Mixer already running")
            return

        try:
            backend, cmd = self.choose_playback_command()
        except Exception as e:
            self.log(f"[AUDIO] {e}")
            return

        self.sync_mixer_sources()

        if not self.mixer_sources:
            self.log("[AUDIO] No recorders are currently in play mode")
            return

        self.mixer_stop_event = threading.Event()
        self.mixer_running = True
        self.log(f"[AUDIO] Starting mixer via {backend}")

        with self.mixer_lock:
            for base_url, source in self.mixer_sources.items():
                self.start_mixer_source(base_url, source)

        self.mixer_thread = threading.Thread(
            target=self._mixer_worker,
            args=(cmd,),
            daemon=True,
        )
        self.mixer_thread.start()

    def _audio_stream_worker(self, base_url, source):
        device = quote(source.get("device", "default"), safe="")
        stream_url = self.build_url_for(base_url, f"/audio/stream?device={device}")
        self.event_queue.put(("debug", f"[AUDIO] Stream open {stream_url}"))

        try:
            with requests.get(
                stream_url,
                stream=True,
                timeout=(REQUEST_TIMEOUT, 5),
            ) as response:
                if response.status_code != 200:
                    self.event_queue.put(("mixer_error", f"[AUDIO] HTTP {response.status_code} from {stream_url}"))
                    return

                for chunk in response.iter_content(chunk_size=AUDIO_CHUNK_BYTES):
                    if source["stop_event"].is_set() or self.mixer_stop_event.is_set():
                        break

                    if not chunk:
                        continue

                    try:
                        source["queue"].put_nowait(chunk)
                    except queue.Empty:
                        pass
                    except queue.Full:
                        try:
                            source["queue"].get_nowait()
                            source["queue"].put_nowait(chunk)
                        except Exception:
                            pass

        except Exception as e:
            if not source["stop_event"].is_set() and not self.mixer_stop_event.is_set():
                self.event_queue.put(("mixer_error", f"[AUDIO] {base_url} {type(e).__name__}: {e}"))

        finally:
            source["level"] = 0.0
            self.event_queue.put(("debug", f"[AUDIO] Stream closed {base_url}"))

    def source_samples(self, source, frames):
        wanted_bytes = frames * 2
        buffer = source.setdefault("buffer", bytearray())

        while True:
            try:
                buffer.extend(source["queue"].get_nowait())
            except queue.Empty:
                break

        if len(buffer) > AUDIO_SOURCE_BUFFER_MAX_BYTES:
            del buffer[:len(buffer) - AUDIO_SOURCE_BUFFER_MAX_BYTES]

        raw = bytes(buffer[:wanted_bytes])
        del buffer[:min(len(buffer), wanted_bytes)]

        if len(raw) < wanted_bytes:
            raw += b"\x00" * (wanted_bytes - len(raw))

        return self.pcm_samples(raw)

    def pcm_samples(self, chunk):
        if not chunk:
            return array.array("h")

        if len(chunk) % 2:
            chunk = chunk[:-1]

        samples = array.array("h")
        samples.frombytes(chunk)

        if sys.byteorder != "little":
            samples.byteswap()

        return samples

    def pcm_level(self, samples):
        if not samples:
            return 0.0

        square_sum = sum(sample * sample for sample in samples)
        rms = math.sqrt(square_sum / len(samples))
        return min(1.0, rms / 12000.0)

    def _mixer_worker(self, cmd):
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.mixer_process = proc
        except Exception as e:
            self.event_queue.put(("mixer_error", f"[AUDIO] Could not start playback: {type(e).__name__}: {e}"))
            self.event_queue.put(("mixer_stopped", "[AUDIO] Mixer stopped"))
            return

        frames = AUDIO_CHUNK_BYTES // 2
        chunk_seconds = AUDIO_CHUNK_BYTES / (AUDIO_RATE * AUDIO_CHANNELS * 2)

        try:
            while not self.mixer_stop_event.is_set():
                with self.mixer_lock:
                    sources = list(self.mixer_sources.values())

                mixed = [0] * frames

                for source in sources:
                    samples = self.source_samples(source, frames)
                    level = self.pcm_level(samples)

                    with self.mixer_lock:
                        source["level"] = level
                        volume = source["volume"]
                        mute = source["mute"]

                    if mute or not samples:
                        continue

                    sample_count = min(frames, len(samples))

                    for index in range(sample_count):
                        mixed[index] += int(samples[index] * volume)

                for index, sample in enumerate(mixed):
                    mixed[index] = max(-32768, min(32767, sample))

                output = array.array("h", mixed)

                if sys.byteorder != "little":
                    output.byteswap()

                proc.stdin.write(output.tobytes())
                proc.stdin.flush()
                self.mixer_stop_event.wait(chunk_seconds)

        except BrokenPipeError:
            pass
        except Exception as e:
            self.event_queue.put(("mixer_error", f"[AUDIO] Mixer error: {type(e).__name__}: {e}"))

        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

            if proc.poll() is None:
                proc.terminate()

                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()

            stderr = ""

            try:
                if proc.stderr:
                    stderr = proc.stderr.read().decode(errors="replace").strip()
            except Exception:
                stderr = ""

            if stderr:
                self.event_queue.put(("mixer_error", f"[AUDIO] Playback stderr:\n{stderr[-2000:]}"))

            self.mixer_process = None
            self.event_queue.put(("mixer_stopped", f"[AUDIO] Mixer stopped with code {proc.returncode}"))

    def draw_level_dots(self, canvas, level):
        canvas.delete("all")

        lit = int(round(clamp(level, 0.0, 1.0) * AUDIO_LEVEL_DOTS))
        radius = 4
        gap = 5

        for index in range(AUDIO_LEVEL_DOTS):
            x = 4 + index * (radius + gap)
            color = "#22b14c" if index < lit else "#d9eadf"
            canvas.create_oval(x, 5, x + radius, 5 + radius, fill=color, outline=color)

    def update_mixer_meters(self):
        with self.mixer_lock:
            levels = {
                base_url: source.get("level", 0.0)
                for base_url, source in self.mixer_sources.items()
            }

        for base_url, row in list(self.mixer_rows.items()):
            self.draw_level_dots(row["canvas"], levels.get(base_url, 0.0))

        self.root.after(100, self.update_mixer_meters)

    def stop_audio_monitor(self, quiet=False):
        if not self.mixer_running:
            if not quiet:
                self.log("[AUDIO] Mixer is not running")
            return

        self.mixer_stop_event.set()

        with self.mixer_lock:
            for source in self.mixer_sources.values():
                self.stop_mixer_source(source)

        if self.mixer_process and self.mixer_process.poll() is None:
            self.mixer_process.terminate()

            try:
                self.mixer_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.mixer_process.kill()

        self.mixer_process = None
        self.mixer_running = False

        if not quiet:
            self.log("[AUDIO] Mixer stopping")

    def send_custom(self):
        path = self.custom_path.get().strip()
        self.command(path)

    def quit(self):
        self.log("[QUIT] Closing")

        if self.zeroconf is not None:
            try:
                self.zeroconf.close()
            except Exception:
                pass

        self.stop_audio_monitor(quiet=True)

        self.root.destroy()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = RecorderGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.quit)
    root.mainloop()
