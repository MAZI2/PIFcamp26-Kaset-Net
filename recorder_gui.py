#!/usr/bin/env python3

import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf


SERVICE_TYPE = "_recorder._tcp.local."
MIN_MOTOR_SPEED = 180
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

        self.selected_url = tk.StringVar()
        self.manual_host = tk.StringVar(value="http://raspberrypi.local:5000")

        self.motor_speed = tk.IntVar(value=MIN_MOTOR_SPEED)
        self.erase_freq = tk.IntVar(value=20000)

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

        self.device_combo = ttk.Combobox(
            discovery_frame,
            textvariable=self.selected_url,
            state="readonly",
            width=95,
        )
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(
            discovery_frame,
            text="Status",
            command=self.status,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            discovery_frame,
            text="Refresh Log",
            command=lambda: self.log("[INFO] Discovery is continuous; wait a few seconds or use manual URL."),
        ).pack(side=tk.LEFT, padx=3)

        # Manual host
        manual_frame = ttk.LabelFrame(main, text="Manual recorder URL", padding=10)
        manual_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Entry(
            manual_frame,
            textvariable=self.manual_host,
            width=95,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(
            manual_frame,
            text="Use Manual",
            command=self.use_manual_host,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(
            manual_frame,
            text="Test Manual",
            command=self.test_manual_host,
        ).pack(side=tk.LEFT, padx=3)

        # Power / mode commands
        power_frame = ttk.LabelFrame(main, text="Power and mode", padding=10)
        power_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(power_frame, text="Power ON", command=lambda: self.command("/power/on")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Power OFF", command=lambda: self.command("/power/off")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Play", command=lambda: self.command("/play")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Record", command=lambda: self.command("/record")).pack(side=tk.LEFT, padx=3)
        ttk.Button(power_frame, text="Status", command=lambda: self.command("/status")).pack(side=tk.LEFT, padx=3)

        # Erase commands
        erase_frame = ttk.LabelFrame(main, text="Erase", padding=10)
        erase_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(erase_frame, text="Frequency Hz:").pack(side=tk.LEFT, padx=3)

        ttk.Entry(
            erase_frame,
            textvariable=self.erase_freq,
            width=10,
        ).pack(side=tk.LEFT, padx=3)

        ttk.Button(erase_frame, text="Erase ON", command=self.erase_on).pack(side=tk.LEFT, padx=3)
        ttk.Button(erase_frame, text="Erase OFF", command=lambda: self.command("/erase/off")).pack(side=tk.LEFT, padx=3)

        ttk.Button(erase_frame, text="20 kHz", command=lambda: self.erase_on_fixed(20000)).pack(side=tk.LEFT, padx=3)
        ttk.Button(erase_frame, text="30 kHz", command=lambda: self.erase_on_fixed(30000)).pack(side=tk.LEFT, padx=3)
        ttk.Button(erase_frame, text="40 kHz", command=lambda: self.erase_on_fixed(40000)).pack(side=tk.LEFT, padx=3)
        ttk.Button(erase_frame, text="50 kHz", command=lambda: self.erase_on_fixed(50000)).pack(side=tk.LEFT, padx=3)

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
                    self.devices[payload["name"]] = payload
                    self.log(
                        f"[DISCOVERY] Found recorder: {payload['name']} "
                        f"at {payload['url']} props={payload['properties']}"
                    )
                    self.refresh_device_combo()

                elif event == "device_removed":
                    if payload in self.devices:
                        removed = self.devices.pop(payload)
                        self.log(f"[DISCOVERY] Removed recorder: {payload} at {removed['url']}")
                        self.refresh_device_combo()

        except queue.Empty:
            pass

        self.root.after(100, self.process_events)

    def refresh_device_combo(self):
        values = []

        for name, dev in sorted(self.devices.items()):
            values.append(f"{dev['url']}  |  {name}")

        self.device_combo["values"] = values

        if values and not self.selected_url.get():
            self.selected_url.set(values[0])
            self.log(f"[DISCOVERY] Auto-selected: {values[0]}")

    # --------------------------------------------------------
    # URL / REQUEST HELPERS
    # --------------------------------------------------------

    def normalize_url(self, url):
        url = url.strip().rstrip("/")

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url

        return url

    def get_base_url(self):
        selected = self.selected_url.get().strip()

        if selected:
            # Combobox value format: "http://ip:port | name"
            return selected.split("|")[0].strip().rstrip("/")

        manual = self.manual_host.get().strip()

        if manual:
            return self.normalize_url(manual)

        raise RuntimeError("No recorder selected or entered manually")

    def use_manual_host(self):
        url = self.normalize_url(self.manual_host.get())
        self.selected_url.set(url)
        self.log(f"[MANUAL] Using manual host: {url}")

    def test_manual_host(self):
        self.use_manual_host()
        self.command("/status")

    def request_async(self, path):
        thread = threading.Thread(target=self._request_worker, args=(path,), daemon=True)
        thread.start()

    def _request_worker(self, path):
        try:
            base_url = self.get_base_url()

            if not path.startswith("/"):
                path = "/" + path

            url = base_url.rstrip("/") + path

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
            self.event_queue.put(("debug", f"[ERROR] {type(e).__name__}: {e}"))

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    def command(self, path):
        self.request_async(path)

    def status(self):
        self.command("/status")

    def erase_on(self):
        freq = self.erase_freq.get()
        self.command(f"/erase/on?freq={freq}")

    def erase_on_fixed(self, freq):
        self.erase_freq.set(freq)
        self.command(f"/erase/on?freq={freq}")

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

    def send_custom(self):
        path = self.custom_path.get().strip()
        self.command(path)

    def quit(self):
        self.log("[QUIT] Closing Zeroconf")

        try:
            self.zeroconf.close()
        except Exception:
            pass

        self.root.destroy()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = RecorderGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.quit)
    root.mainloop()
