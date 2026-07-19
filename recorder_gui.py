#!/usr/bin/env python3

import json
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf


SERVICE_TYPE = "_recorder._tcp.local."
MIN_MOTOR_SPEED = 180
RECORD_PATH = "/record?led=0"
REQUEST_TIMEOUT = 3.0


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
        self.audio_process = None
        self.audio_starting = False

        self.manual_host = tk.StringVar(value="192.168.0.9")
        self.audio_device = tk.StringVar(value="auto")

        self.motor_speed = tk.IntVar(value=MIN_MOTOR_SPEED)
        self.zeroconf = Zeroconf()
        self.listener = RecorderDiscovery(self.event_queue)
        self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, self.listener)

        self.build_ui()

        self.log("[INIT] GUI started")
        self.log(f"[INIT] Browsing for mDNS service: {SERVICE_TYPE}")

        self.root.after(100, self.process_events)

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

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
            text="Select All",
            command=self.select_all_devices,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            discovery_frame,
            text="Status All",
            command=self.status,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            discovery_frame,
            text="Refresh Log",
            command=lambda: self.log("[INFO] Discovery is continuous; wait a few seconds or use manual URL."),
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
        self.speed_slider.set(self.motor_speed.get())
        self.speed_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)

        self.speed_label = ttk.Label(motor_frame, text=str(self.motor_speed.get()), width=4)
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

        # Audio monitor
        audio_frame = ttk.LabelFrame(main, text="Audio monitor", padding=10)
        audio_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(audio_frame, text="ALSA device:").pack(side=tk.LEFT, padx=3)

        ttk.Entry(
            audio_frame,
            textvariable=self.audio_device,
            width=16,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(audio_frame, text="Start Monitor", command=self.start_audio_monitor).pack(side=tk.LEFT, padx=3)
        ttk.Button(audio_frame, text="Stop Monitor", command=self.stop_audio_monitor).pack(side=tk.LEFT, padx=3)
        ttk.Button(audio_frame, text="List Devices", command=lambda: self.command("/audio/devices")).pack(side=tk.LEFT, padx=3)

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

        self.log_box = scrolledtext.ScrolledText(log_frame, height=22, wrap=tk.WORD)
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

    def process_events(self):
        try:
            while True:
                event, payload = self.event_queue.get_nowait()

                if event == "debug":
                    self.log(payload)

                elif event == "device_added":
                    self.devices[payload["url"]] = payload
                    self.log(
                        f"[DISCOVERY] Found recorder: {payload['name']} "
                        f"at {payload['url']} props={payload['properties']}"
                    )
                    self.refresh_device_list()

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

                elif event == "audio_started":
                    proc, stream_url = payload
                    self.audio_starting = False

                    if proc.poll() is None:
                        self.audio_process = proc
                        self.log(f"[AUDIO] Monitor started: {stream_url}")
                    else:
                        self.audio_process = None
                        self.log(f"[AUDIO] Monitor exited immediately with code {proc.returncode}")

                elif event == "audio_exited":
                    proc, returncode = payload

                    if self.audio_process is proc:
                        self.audio_process = None
                        self.log(f"[AUDIO] Monitor exited with code {returncode}")

                elif event == "audio_error":
                    self.audio_starting = False
                    self.audio_process = None
                    self.log(payload)

        except queue.Empty:
            pass

        self.root.after(100, self.process_events)

    def device_label(self, dev):
        return f"{dev['url']}  |  {dev['name']}"

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

    def _request_one(self, url, start_event):
        start_event.wait()

        try:
            self.event_queue.put(("debug", f"[REQUEST] GET {url}"))

            response = requests.get(url, timeout=REQUEST_TIMEOUT)

            self.event_queue.put(("debug", f"[RESPONSE] HTTP {response.status_code} from {url}"))

            try:
                data = response.json()
                pretty = json.dumps(data, indent=2, sort_keys=True)
                self.event_queue.put(("debug", f"[JSON]\n{pretty}"))
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
            url = self.build_url_for(base_url, path)
            worker = threading.Thread(
                target=self._request_one,
                args=(url, start_event),
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
        self.command("/erase/on?freq=20000")

    def on_speed_slider(self, value):
        speed = max(MIN_MOTOR_SPEED, int(float(value)))
        self.motor_speed.set(speed)
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

    def start_audio_monitor(self):
        if self.audio_starting:
            self.log("[AUDIO] Monitor is starting")
            return

        if self.audio_process and self.audio_process.poll() is None:
            self.log("[AUDIO] Monitor already running")
            return

        ffplay = shutil.which("ffplay")

        if not ffplay:
            self.log("[AUDIO] ffplay not found. Install ffmpeg/ffplay on this computer.")
            return

        device = quote(self.audio_device.get().strip() or "default", safe="")
        stream_url = self.build_url(f"/audio/stream?device={device}")

        cmd = [
            ffplay,
            "-nodisp",
            "-autoexit",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-probesize", "32",
            "-analyzeduration", "0",
            stream_url,
        ]

        self.audio_starting = True
        self.log(f"[AUDIO] Starting monitor: {stream_url}")

        thread = threading.Thread(
            target=self._start_audio_monitor_worker,
            args=(cmd, stream_url),
            daemon=True,
        )
        thread.start()

    def _start_audio_monitor_worker(self, cmd, stream_url):
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.event_queue.put(("audio_started", (proc, stream_url)))
            returncode = proc.wait()
            self.event_queue.put(("audio_exited", (proc, returncode)))

        except Exception as e:
            self.event_queue.put((
                "audio_error",
                f"[AUDIO] Could not start monitor: {type(e).__name__}: {e}",
            ))

    def stop_audio_monitor(self, quiet=False):
        if self.audio_starting:
            self.audio_starting = False
            if not quiet:
                self.log("[AUDIO] Monitor launch is still in progress")
            return

        if not self.audio_process or self.audio_process.poll() is not None:
            self.audio_process = None
            if not quiet:
                self.log("[AUDIO] Monitor is not running")
            return

        self.audio_process.terminate()

        try:
            self.audio_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.audio_process.kill()

        self.audio_process = None
        self.log("[AUDIO] Monitor stopped")

    def send_custom(self):
        path = self.custom_path.get().strip()
        self.command(path)

    def quit(self):
        self.log("[QUIT] Closing Zeroconf")

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
