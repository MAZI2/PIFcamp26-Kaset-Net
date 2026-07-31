#!/usr/bin/env python3
"""Live performance controller for the cassette recorder network.

This is the primary GUI. It keeps the performance layout from the mockup, but
owns the real recorder, network, motor, and send-audio logic.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf


SERVICE_TYPE = "_recorder._tcp.local."
RECORD_PATH = "/record"
REQUEST_TIMEOUT = 3.0
AUDIO_RATE = 44100
AUDIO_CHANNELS = 1
AUDIO_CHUNK_BYTES = 4096
AUDIO_SOURCE_BUFFER_MAX_BYTES = AUDIO_RATE * AUDIO_CHANNELS * 2
AUDIO_LEVEL_DOTS = 26

REFERENCE_WIDTH = 1680
REFERENCE_HEIGHT = 945
DESIGN_WIDTH = 1366
DESIGN_HEIGHT = 768
MAX_PLAYERS = 4

BG = "#090b0b"
PANEL = "#101212"
PANEL_EDGE = "#555858"
DIVIDER = "#505252"
TEXT = "#d7d7d7"
MUTED = "#b9babb"
DIM = "#858888"
GREEN = "#89d557"
GREEN_DARK = "#315622"
GREEN_METER = "#78c34d"
RED = "#ff6969"
RED_DARK = "#a83e3e"
YELLOW = "#e6ca61"
CONTROL = "#171919"
CONTROL_EDGE = "#626565"
FADER = "#d1d1d1"
SELECTED = "#d8f5be"


def clamp(value, low, high):
    return max(low, min(high, value))


@dataclass
class RecorderDevice:
    name: str
    url: str
    ip: str
    mode: str = "play"
    speed: int = 0
    reverse: bool = False
    erase: bool = False
    online: bool = True
    monitor_volume: float = 1.0
    monitor_muted: bool = False


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
        self.event_queue.put((
            "device_added",
            {
                "name": name,
                "ip": ip,
                "url": f"http://{ip}:{port}",
            },
        ))

    def update_service(self, zeroconf, service_type, name):
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        self.event_queue.put(("device_removed", name))


class PerformanceCanvas:
    def __init__(self, canvas: tk.Canvas, gui: "PerformanceGUI"):
        self.canvas = canvas
        self.gui = gui
        self.width = DESIGN_WIDTH
        self.height = DESIGN_HEIGHT
        self.scale = min(self.width / REFERENCE_WIDTH, self.height / REFERENCE_HEIGHT)
        self.offset_x = (self.width - REFERENCE_WIDTH * self.scale) / 2
        self.offset_y = (self.height - REFERENCE_HEIGHT * self.scale) / 2
        self.actions: list[tuple[tuple[float, float, float, float], str, object]] = []
        self.player_bounds = ((283, 569), (582, 871), (885, 1149), (1162, 1397))

    def sx(self, value: float) -> float:
        return self.offset_x + value * self.scale

    def sy(self, value: float) -> float:
        return self.offset_y + value * self.scale

    def rx(self, value: float) -> float:
        return (value - self.offset_x) / self.scale

    def ry(self, value: float) -> float:
        return (value - self.offset_y) / self.scale

    def font(self, size: int, bold: bool = False) -> tuple[str, int, str]:
        pixels = max(8, round(size * self.scale))
        return ("DejaVu Sans Mono", -pixels, "bold" if bold else "normal")

    def rect(self, x1, y1, x2, y2, *, fill="", outline=PANEL_EDGE, width=1):
        self.canvas.create_rectangle(
            self.sx(x1),
            self.sy(y1),
            self.sx(x2),
            self.sy(y2),
            fill=fill,
            outline=outline,
            width=max(1, round(width * self.scale)),
            tags=("art",),
        )

    def line(self, x1, y1, x2, y2, *, fill=DIVIDER, width=1):
        self.canvas.create_line(
            self.sx(x1),
            self.sy(y1),
            self.sx(x2),
            self.sy(y2),
            fill=fill,
            width=max(1, round(width * self.scale)),
            tags=("art",),
        )

    def text(self, x, y, value, *, size=15, fill=TEXT, anchor="nw", bold=False):
        self.canvas.create_text(
            self.sx(x),
            self.sy(y),
            text=value,
            fill=fill,
            font=self.font(size, bold),
            anchor=anchor,
            tags=("art",),
        )

    def dot(self, x, y, radius=6, fill=GREEN):
        self.canvas.create_oval(
            self.sx(x - radius),
            self.sy(y - radius),
            self.sx(x + radius),
            self.sy(y + radius),
            fill=fill,
            outline="",
            tags=("art",),
        )

    def action_rect(self, x1, y1, x2, y2, action, payload=None):
        self.actions.append(((x1, y1, x2, y2), action, payload))

    def button(self, x1, y1, x2, y2, label, *, action=None, payload=None, color=TEXT, outline=CONTROL_EDGE, size=15):
        self.rect(x1, y1, x2, y2, fill=CONTROL, outline=outline)
        self.text((x1 + x2) / 2, (y1 + y2) / 2, label, size=size, fill=color, anchor="center")

        if action:
            self.action_rect(x1, y1, x2, y2, action, payload)

    def panel(self, x1, x2, *, selected=False):
        outline = SELECTED if selected else PANEL_EDGE
        self.rect(x1, 13, x2, 883, fill=PANEL, outline=outline, width=2 if selected else 1)

    def divider(self, x1, x2, y):
        self.line(x1, y, x2, y)

    def vertical_meter(self, x, top, bottom, level):
        segment_h = 5
        gap = 2
        total = max(1, int((bottom - top) // (segment_h + gap)))
        lit = int(round(clamp(level, 0.0, 1.0) * total))

        for index in range(total):
            y2 = bottom - index * (segment_h + gap)
            y1 = y2 - segment_h
            color = GREEN_METER if index < lit else GREEN_DARK
            self.rect(x, y1, x + 14, y2, fill=color, outline=color)

    def horizontal_fader(self, x1, x2, y, fraction):
        value_x = x1 + (x2 - x1) * clamp(fraction, 0.0, 1.0)
        self.line(x1, y, x2, y, fill=FADER, width=3)

        for index in range(8):
            x = x1 + (x2 - x1) * index / 7
            self.line(x, y + 11, x, y + 17, fill=DIM)

        self.rect(value_x - 8, y - 12, value_x + 8, y + 12, fill=FADER, outline=FADER)

    def draw(self):
        self.actions = []
        self.canvas.delete("art")
        self.canvas.configure(bg=BG)
        self.draw_connections()
        self.draw_players()
        self.draw_master()
        self.draw_status_bar()

    def draw_connections(self):
        x1, x2 = 13, 265
        self.panel(x1, x2)
        self.text(35, 32, "CONNECTIONS", size=17, bold=True)
        self.divider(30, 241, 63)

        devices = self.gui.ordered_devices()
        self.text(35, 80, f"CONNECTED PLAYERS ({len(devices)})", size=14, fill=MUTED)

        for index in range(MAX_PLAYERS):
            y = 129 + index * 31
            dev = devices[index] if index < len(devices) else None

            if dev:
                selected = dev.url in self.gui.selected_urls
                self.dot(36, y, radius=6, fill=SELECTED if selected else GREEN)
                self.text(56, y, str(index + 1), size=14, anchor="w", fill=SELECTED if selected else TEXT)
                self.text(89, y, dev.ip, size=14, anchor="w", fill=GREEN if dev.online else RED)
                self.action_rect(28, y - 14, 244, y + 14, "toggle_select", dev.url)
            else:
                self.dot(36, y, radius=6, fill=GREEN_DARK)
                self.text(56, y, str(index + 1), size=14, anchor="w", fill=DIM)
                self.text(89, y, "--", size=14, anchor="w", fill=DIM)

        self.button(31, 276, 126, 317, "REFRESH", action="status_selected", size=14)
        self.button(144, 276, 243, 317, "SCAN", action="scan", size=14)
        self.text(35, 349, self.gui.last_message[:20] or "--", size=13, fill=DIM)

        self.text(35, 397, "ADD PLAYER", size=15)
        self.rect(30, 427, 245, 468, fill=CONTROL, outline=CONTROL_EDGE)
        self.button(30, 487, 126, 532, "ADD", action="add_manual", size=14)
        self.button(143, 487, 245, 532, "ADD + TEST", action="add_test", size=14)
        self.divider(35, 240, 588)

        self.text(35, 613, "SELECTION", size=15)
        self.button(30, 650, 245, 694, "SELECT ALL", action="select_all", size=14)
        self.button(30, 717, 245, 761, "CLEAR SELECT", action="clear_select", size=14)

    def draw_players(self):
        devices = self.gui.ordered_devices()

        for index, (x1, x2) in enumerate(self.player_bounds):
            dev = devices[index] if index < len(devices) else None
            self.draw_player(x1, x2, index, dev)

    def draw_player(self, x1, x2, index, dev: RecorderDevice | None):
        selected = bool(dev and dev.url in self.gui.selected_urls)
        self.panel(x1, x2, selected=selected)
        left = x1 + 15
        right = x2 - 18
        center = (x1 + x2) / 2

        title_fill = SELECTED if selected else GREEN
        self.text(left, 31, f"PLAYER {index + 1}", size=17, fill=title_fill, bold=True)
        self.text(left, 58, dev.ip if dev else "--", size=16, fill=GREEN if dev else DIM)
        self.divider(left, right, 89)

        if dev:
            self.action_rect(x1, 13, x2, 89, "toggle_select", dev.url)

        self.text(left, 116, "TRANSPORT", size=15)
        available = right - left
        gap = 15
        button_w = (available - gap) / 2
        rec_outline = RED_DARK if not dev or dev.mode != "record" else RED
        play_outline = "#467c30" if not dev or dev.mode != "play" else GREEN
        self.button(left + 2, 146, left + button_w, 188, "REC", action="record_one", payload=dev.url if dev else None, color=RED, outline=rec_outline)
        self.button(left + button_w + gap, 146, right, 188, "PLAY", action="play_one", payload=dev.url if dev else None, color=GREEN, outline=play_outline)
        self.divider(left, right, 201)

        self.text(left, 220, "INPUT (MIC -> TAPE)", size=15)
        meter_x = right - 53
        self.vertical_meter(meter_x, 257, 441, self.gui.player_audio_level(dev))
        self.text(left, 260, "SOURCE", size=13, fill=MUTED)
        self.text(left, 286, self.gui.audio_source_display(), size=12, fill=GREEN)
        self.text(left, 324, f"MON VOL {self.gui.monitor_volume_label(dev)}", size=13, fill=GREEN)
        vol_left = left + 4
        vol_right = right - 74
        self.horizontal_fader(vol_left, vol_right, 361, self.gui.monitor_volume_fraction(dev))
        self.action_rect(vol_left, 341, vol_right, 381, "monitor_volume_drag", dev.url if dev else None)
        mute_label = "MUTED" if dev and dev.monitor_muted else "MUTE"
        mute_color = RED if dev and dev.monitor_muted else TEXT
        self.button(vol_left, 402, vol_right, 438, mute_label, action="toggle_mute", payload=dev.url if dev else None, color=mute_color, size=13)
        audio_state = self.gui.audio_send_status.get() if dev and dev.mode == "record" else self.gui.mixer_status()
        self.text(left, 469, audio_state, size=12, fill=MUTED)
        self.divider(left, right, 493)

        speed = dev.speed if dev else self.gui.motor_speed
        speed_fraction = clamp(speed / 255.0, 0.0, 1.0)
        self.text(left, 509, "MOTOR", size=15)
        self.text(left, 546, "SPEED", size=14)
        slider_left = left + 6
        slider_right = right - 72
        self.horizontal_fader(slider_left, slider_right, 584, speed_fraction)
        self.action_rect(slider_left, 564, slider_right, 604, "speed_drag", dev.url if dev else None)
        self.text(slider_left, 609, "0", size=12, anchor="center")
        self.text(slider_right, 609, "255", size=12, anchor="center")
        self.rect(right - 48, 571, right, 601, fill=CONTROL, outline="#454848")
        self.text(right - 24, 586, str(speed), size=14, fill=GREEN, anchor="center")

        self.text(left, 656, "DIRECTION", size=15)
        direction_gap = 15
        direction_w = (available - direction_gap) / 2
        self.button(left, 687, left + direction_w, 730, "REV", action="reverse_one", payload=dev.url if dev else None, size=14)
        self.button(right - direction_w, 687, right, 730, "FWD", action="forward_one", payload=dev.url if dev else None, size=14)
        self.divider(left, right, 743)

        self.text(left, 764, "STATUS", size=15)
        self.rect(left, 791, right + 3, 863, fill="#111414", outline="#454848")
        mode = dev.mode.upper() if dev else "--"
        reverse = "REV" if dev and dev.reverse else "FWD"
        state = "ON" if dev and dev.online else "--"
        self.text(left + 10, 802, f"Online: {state}\nMode:   {mode}\nSpeed:  {speed} {reverse}", size=14, fill=GREEN if dev else DIM)
        self.dot(right - 18, 808, radius=6, fill=GREEN if dev and dev.online else RED_DARK)

    def draw_master(self):
        x1, x2 = 1411, 1658
        self.panel(x1, x2)
        left, right = 1425, 1644
        center = (x1 + x2) / 2

        self.text(center, 31, "MASTER", size=17, bold=True, anchor="n")
        self.divider(left, right, 62)
        self.text(left, 79, "SEND AUDIO", size=14)
        self.text(left, 106, "SOURCE", size=12, fill=MUTED)
        self.rect(left, 124, right, 154, fill=CONTROL, outline=CONTROL_EDGE)
        self.text(left + 7, 139, self.gui.audio_source_display(), size=11, fill=GREEN, anchor="w")
        self.text(left, 178, "GAIN", size=12, fill=MUTED)
        self.horizontal_fader(left, right, 209, (self.gui.audio_send_gain.get() - 0.25) / 3.75)
        self.action_rect(left, 190, right, 226, "gain_drag", None)
        self.text(center, 243, f"{self.gui.audio_send_gain.get():.1f}x", size=15, fill=GREEN, anchor="center")
        self.vertical_meter(1612, 265, 355, self.gui.audio_send_level)
        self.text(left, 324, self.gui.audio_send_status.get(), size=12, fill=MUTED)
        self.divider(left, right, 362)

        selected_count = len(self.gui.target_urls())
        self.text(left, 379, f"TRANSPORT ({selected_count} SEL)", size=15)
        self.button(left, 453, right + 1, 492, "PLAY SELECTED", action="play_selected", color=GREEN, outline="#467c30", size=13)
        self.button(left, 499, right + 1, 538, "REC SELECTED", action="record_selected", color=RED, outline=RED_DARK, size=13)
        self.divider(left, right, 550)

        self.text(left, 561, "MOTOR", size=15)
        self.text(left, 588, "SPEED", size=14)
        slider_left, slider_right = 1429, 1574
        self.horizontal_fader(slider_left, slider_right, 620, self.gui.motor_speed / 255.0)
        self.action_rect(slider_left, 600, slider_right, 640, "speed_drag", None)
        self.text(slider_left, 645, "0", size=12, anchor="center")
        self.text(slider_right, 645, "255", size=12, anchor="center")
        self.rect(1601, 608, 1644, 638, fill=CONTROL, outline="#454848")
        self.text(1622, 623, str(self.gui.motor_speed), size=14, fill=GREEN, anchor="center")

        self.text(left, 662, "DIRECTION", size=14)
        gap = 10
        width = (right - left - gap) / 2
        self.button(left, 687, left + width, 728, "REV", action="reverse_selected", size=13)
        self.button(right - width, 687, right + 5, 728, "FWD", action="forward_selected", size=13)
        self.divider(left, right, 739)

        self.text(left, 750, "SYSTEM", size=15)
        self.button(left, 776, right + 1, 815, "STATUS SELECTED", action="status_selected", size=13)
        self.button(left, 823, right + 1, 863, "STOP MOTORS", action="stop_selected", color=RED, outline=RED_DARK, size=13)

    def draw_status_bar(self):
        self.rect(13, 895, 1658, 935, fill=PANEL, outline=PANEL_EDGE)
        self.text(27, 915, "CASSETTE PERFORMANCE CONTROLLER", size=13, anchor="w")
        self.text(342, 915, "|", size=13, fill=MUTED, anchor="center")
        self.text(375, 915, f"{len(self.gui.devices)} PLAYERS", size=13, fill=GREEN, anchor="w")
        self.text(548, 915, "|", size=13, fill=MUTED, anchor="center")
        self.text(580, 915, f"SEND: {self.gui.audio_send_status.get()}", size=13, fill=GREEN if self.gui.audio_send_running else MUTED, anchor="w")
        self.text(1025, 915, self.gui.last_message[-52:], size=13, fill=MUTED, anchor="w")
        self.text(1628, 915, "F11 FULLSCREEN", size=13, anchor="e")

    def hit_test(self, event):
        x = self.rx(event.x)
        y = self.ry(event.y)

        for (x1, y1, x2, y2), action, payload in reversed(self.actions):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return action, payload, x, y, (x1, y1, x2, y2)

        return None, None, x, y, None


class PerformanceGUI:
    def __init__(self, root: tk.Tk, *, windowed: bool = True) -> None:
        self.root = root
        self.windowed = windowed
        self.fullscreen = not windowed
        self.root.title("Cassette Performance Controller")
        self.root.configure(bg="black")

        self.event_queue = queue.Queue()
        self.devices: dict[str, RecorderDevice] = {}
        self.selected_urls: set[str] = set()
        self.last_message = ""
        self.motor_speed = 0
        self.drag_action = None
        self.drag_payload = None

        self.mixer_running = False
        self.mixer_process = None
        self.mixer_stop_event = threading.Event()
        self.mixer_thread = None
        self.mixer_sources = {}
        self.mixer_lock = threading.Lock()

        self.audio_send_running = False
        self.audio_send_process = None
        self.audio_send_thread = None
        self.audio_send_stop_event = threading.Event()
        self.audio_send_generation = 0
        self.audio_send_targets: list[str] = []
        self.audio_send_queues = {}
        self.audio_send_post_threads = []
        self.audio_send_source_map = {}
        self.audio_send_level = 0.0
        self.audio_send_source = tk.StringVar(value="0" if sys.platform == "darwin" else "default")
        self.audio_send_status = tk.StringVar(value="Send idle")
        self.audio_send_gain = tk.DoubleVar(value=2.0)
        self.audio_output_device = tk.StringVar(value="auto")
        self.manual_host = tk.StringVar(value="192.168.0.9")

        self.zeroconf = None
        self.listener = None
        self.browser = None

        self.shell = tk.Frame(root, bg="black")
        self.shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            self.shell,
            width=DESIGN_WIDTH,
            height=DESIGN_HEIGHT,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.place(relx=0.5, rely=0.5, anchor="center", width=DESIGN_WIDTH, height=DESIGN_HEIGHT)
        self.artwork = PerformanceCanvas(self.canvas, self)
        self.build_overlay_widgets()
        self.redraw()

        self.canvas.bind("<Button-1>", self.on_canvas_down)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_up)
        self.root.bind("<Escape>", lambda _event: self.quit())
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind_all("<Control-a>", self.select_all_event)
        self.root.bind_all("<Control-A>", self.select_all_event)
        self.root.bind_all("<Command-a>", self.select_all_event)
        self.root.bind_all("<Command-A>", self.select_all_event)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        if windowed:
            self.enter_windowed()
        else:
            self.enter_fullscreen()

        self.log("[INIT] Performance GUI ready")
        self.root.after(100, self.process_events)
        self.root.after(100, self.update_meters)
        self.root.after(500, self.list_local_audio_sources)

    def build_overlay_widgets(self):
        style = ttk.Style(self.root)

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Perf.TCombobox", fieldbackground=CONTROL, background=CONTROL, foreground=TEXT)
        style.configure("Perf.Horizontal.TScale", background=PANEL)

        self.manual_entry = tk.Entry(
            self.canvas,
            textvariable=self.manual_host,
            bg=CONTROL,
            fg=TEXT,
            insertbackground=TEXT,
            relief=tk.FLAT,
            bd=0,
            font=("DejaVu Sans Mono", 11),
        )
        self.canvas.create_window(
            self.artwork.sx(39),
            self.artwork.sy(448),
            window=self.manual_entry,
            width=max(10, self.artwork.sx(237) - self.artwork.sx(39)),
            height=max(10, self.artwork.sy(463) - self.artwork.sy(432)),
            anchor="w",
            tags=("overlay",),
        )

        self.source_combo = ttk.Combobox(
            self.canvas,
            textvariable=self.audio_send_source,
            style="Perf.TCombobox",
            state="readonly",
        )
        self.canvas.create_window(
            self.artwork.sx(1428),
            self.artwork.sy(139),
            window=self.source_combo,
            width=max(10, self.artwork.sx(1640) - self.artwork.sx(1428)),
            height=24,
            anchor="w",
            tags=("overlay",),
        )

        self.output_entry = tk.Entry(
            self.canvas,
            textvariable=self.audio_output_device,
            bg=CONTROL,
            fg=TEXT,
            insertbackground=TEXT,
            relief=tk.FLAT,
            bd=0,
            font=("DejaVu Sans Mono", 10),
        )
        self.canvas.create_window(
            self.artwork.sx(1428),
            self.artwork.sy(172),
            window=self.output_entry,
            width=max(10, self.artwork.sx(1640) - self.artwork.sx(1428)),
            height=22,
            anchor="w",
            tags=("overlay",),
        )

    def redraw(self):
        self.artwork.draw()
        self.canvas.tag_raise("overlay")

    def log(self, message):
        self.last_message = message
        print(message, flush=True)
        self.redraw()

    def ordered_devices(self):
        return sorted(self.devices.values(), key=lambda dev: dev.url)[:MAX_PLAYERS]

    def target_urls(self):
        if self.selected_urls:
            return sorted(url for url in self.selected_urls if url in self.devices)
        return [dev.url for dev in self.ordered_devices()]

    def normalize_url(self, url):
        url = url.strip().rstrip("/")

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url

        parts = urlsplit(url)

        if parts.scheme == "http" and parts.hostname and parts.port is None:
            url = urlunsplit((parts.scheme, f"{parts.hostname}:5000", parts.path, parts.query, parts.fragment))

        return url

    def add_device(self, name, url, ip=None):
        url = self.normalize_url(url)
        ip = ip or urlsplit(url).hostname or url
        existing = self.devices.get(url)

        if existing:
            existing.name = name or existing.name
            existing.ip = ip
            existing.online = True
        else:
            self.devices[url] = RecorderDevice(name=name or "Recorder", url=url, ip=ip)

        if not self.selected_urls:
            self.selected_urls.add(url)

        self.sync_mixer_sources()
        self.redraw()
        return url

    def add_manual_host(self, test=False):
        try:
            url = self.add_device("Manual recorder", self.manual_host.get())
            self.log(f"[MANUAL] Added {url}")

            if test:
                self.request_async([url], "/status")

        except Exception as e:
            self.log(f"[ERROR] {type(e).__name__}: {e}")

    def start_discovery(self):
        if self.browser is not None:
            self.log("[DISCOVERY] Already scanning")
            return

        self.log(f"[DISCOVERY] Scanning {SERVICE_TYPE}")
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
                    url = self.add_device(payload["name"], payload["url"], payload["ip"])
                    self.log(f"[DISCOVERY] Found {url}")

                elif event == "device_removed":
                    for url, dev in list(self.devices.items()):
                        if dev.name == payload:
                            dev.online = False
                    self.sync_mixer_sources()
                    self.redraw()

                elif event == "status_update":
                    base_url, data = payload
                    self.update_device_from_status(base_url, data)

                elif event == "audio_send_sources":
                    self.set_audio_send_sources(payload)

                elif event == "audio_send_stopped":
                    generation, message = payload

                    if generation != self.audio_send_generation:
                        continue

                    self.audio_send_running = False
                    self.audio_send_process = None
                    self.audio_send_targets = []
                    self.audio_send_queues = {}
                    self.audio_send_post_threads = []
                    self.audio_send_level = 0.0
                    self.audio_send_status.set(message)
                    self.log(f"[AUDIO SEND] {message}")

        except queue.Empty:
            pass

        self.root.after(100, self.process_events)

    def update_device_from_status(self, base_url, data):
        dev = self.devices.get(base_url)

        if not dev or not isinstance(data, dict):
            return

        mode = data.get("mode")
        speed = data.get("motor_speed")
        reverse = data.get("motor_reverse")

        if mode in ["play", "record"]:
            dev.mode = mode

        if speed is not None:
            dev.speed = int(speed)

        if reverse is not None:
            dev.reverse = bool(reverse)

        dev.erase = bool(data.get("erase", False))
        dev.online = True
        self.sync_mixer_sources()
        self.redraw()

    def command_targets(self, urls, path):
        urls = [url for url in urls if url in self.devices]

        if not urls:
            self.log("[TARGETS] No recorder selected")
            return

        self.apply_local_command_state(urls, path)

        if urlsplit(path).path in [RECORD_PATH, "/play", "/power/off"]:
            self.sync_record_audio_send()

        self.request_async(urls, path)

    def record_mode_urls(self):
        return [
            dev.url for dev in self.ordered_devices()
            if dev.online and dev.mode == "record"
        ]

    def sync_record_audio_send(self):
        record_urls = self.record_mode_urls()

        if record_urls:
            self.start_audio_send(record_urls)
        else:
            self.stop_audio_send()

    def apply_local_command_state(self, urls, path):
        command_path = urlsplit(path).path

        for url in urls:
            dev = self.devices.get(url)

            if not dev:
                continue

            if command_path == "/play":
                dev.mode = "play"
            elif command_path == RECORD_PATH:
                dev.mode = "record"
            elif command_path == "/stop":
                dev.speed = 0
            elif command_path == "/reverse/on":
                dev.reverse = True
            elif command_path == "/reverse/off":
                dev.reverse = False
            elif command_path == "/motor":
                query = urlsplit(path).query
                match = re.search(r"(?:^|&)speed=(\d+)", query)

                if match:
                    dev.speed = int(match.group(1))

        self.redraw()
        self.sync_mixer_sources()

    def request_async(self, urls, path):
        thread = threading.Thread(
            target=self._request_group_worker,
            args=(path, urls),
            daemon=True,
        )
        thread.start()

    def build_url_for(self, base_url, path):
        if not path.startswith("/"):
            path = "/" + path

        return base_url.rstrip("/") + path

    def _request_one(self, base_url, path, start_event):
        start_event.wait()
        url = self.build_url_for(base_url, path)

        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)

            try:
                data = response.json()
                self.event_queue.put(("status_update", (base_url, data)))
            except Exception:
                pass

            self.event_queue.put(("debug", f"[HTTP] {response.status_code} {url}"))

        except Exception as e:
            dev = self.devices.get(base_url)

            if dev:
                dev.online = False

            self.event_queue.put(("debug", f"[ERROR] {url} {type(e).__name__}: {e}"))

    def _request_group_worker(self, path, urls):
        start_event = threading.Event()
        workers = []

        for base_url in urls:
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

    def playable_mixer_urls(self):
        return [
            dev.url for dev in self.ordered_devices()
            if dev.online and dev.mode == "play"
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

        raise RuntimeError("No audio playback tool found. Install ffmpeg/ffplay on macOS or alsa-utils/aplay on Linux.")

    def sync_mixer_sources(self):
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
                        "level": 0.0,
                    }

            sources = list(self.mixer_sources.items())

        if sources and not self.mixer_running:
            self.start_audio_monitor()
            return

        if not sources and self.mixer_running:
            self.stop_audio_monitor(quiet=True)
            return

        if self.mixer_running:
            for base_url, source in sources:
                self.start_mixer_source(base_url, source)

    def start_audio_monitor(self):
        if self.mixer_running:
            return

        try:
            backend, cmd = self.choose_playback_command()
        except Exception as e:
            self.log(f"[MIXER] {e}")
            return

        with self.mixer_lock:
            sources = list(self.mixer_sources.items())

        if not sources:
            return

        self.mixer_stop_event = threading.Event()
        self.mixer_running = True
        self.log(f"[MIXER] Starting automatic output via {backend}")

        for base_url, source in sources:
            self.start_mixer_source(base_url, source)

        self.mixer_thread = threading.Thread(
            target=self._mixer_worker,
            args=(cmd,),
            daemon=True,
        )
        self.mixer_thread.start()

    def start_mixer_source(self, base_url, source):
        thread = source.get("thread")

        if thread and thread.is_alive():
            return

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

    def _audio_stream_worker(self, base_url, source):
        stream_url = self.build_url_for(base_url, "/audio/stream?device=auto")
        self.event_queue.put(("debug", f"[MIXER] Stream open {stream_url}"))

        try:
            with requests.get(
                stream_url,
                stream=True,
                timeout=(REQUEST_TIMEOUT, 5),
            ) as response:
                if response.status_code != 200:
                    self.event_queue.put(("debug", f"[MIXER] HTTP {response.status_code} from {stream_url}"))
                    return

                for chunk in response.iter_content(chunk_size=AUDIO_CHUNK_BYTES):
                    if source["stop_event"].is_set() or self.mixer_stop_event.is_set():
                        break

                    if not chunk:
                        continue

                    try:
                        source["queue"].put_nowait(chunk)
                    except queue.Full:
                        try:
                            source["queue"].get_nowait()
                            source["queue"].put_nowait(chunk)
                        except Exception:
                            pass

        except Exception as e:
            if not source["stop_event"].is_set() and not self.mixer_stop_event.is_set():
                self.event_queue.put(("debug", f"[MIXER] {base_url} {type(e).__name__}: {e}"))

        finally:
            source["level"] = 0.0
            self.event_queue.put(("debug", f"[MIXER] Stream closed {base_url}"))

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
            self.event_queue.put(("debug", f"[MIXER] Could not start playback: {type(e).__name__}: {e}"))
            self.mixer_running = False
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
                    source["level"] = level
                    dev = self.devices.get(source["url"])
                    volume = dev.monitor_volume if dev else 1.0
                    muted = dev.monitor_muted if dev else False

                    if muted:
                        continue

                    sample_count = min(frames, len(samples))

                    for index in range(sample_count):
                        mixed[index] += int(samples[index] * volume)

                for index, sample in enumerate(mixed):
                    mixed[index] = int(clamp(sample, -32768, 32767))

                output = array.array("h", mixed)

                if sys.byteorder != "little":
                    output.byteswap()

                proc.stdin.write(output.tobytes())
                proc.stdin.flush()
                self.mixer_stop_event.wait(chunk_seconds)

        except BrokenPipeError:
            pass
        except Exception as e:
            self.event_queue.put(("debug", f"[MIXER] Error: {type(e).__name__}: {e}"))

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
                self.event_queue.put(("debug", f"[MIXER] Playback stderr:\n{stderr[-2000:]}"))

            self.mixer_process = None
            self.mixer_running = False
            self.event_queue.put(("debug", "[MIXER] Automatic output stopped"))

    def stop_audio_monitor(self, quiet=False):
        if not self.mixer_running:
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
            self.log("[MIXER] Stopping automatic output")

    def on_canvas_down(self, event):
        action, payload, x, y, rect = self.artwork.hit_test(event)

        if action in ["speed_drag", "gain_drag", "monitor_volume_drag"]:
            self.drag_action = action
            self.drag_payload = payload
            self.handle_drag(action, payload, x, rect, commit=False)
            return

        self.handle_action(action, payload)

    def on_canvas_drag(self, event):
        if not self.drag_action:
            return

        action, payload, x, _y, rect = self.artwork.hit_test(event)
        self.handle_drag(self.drag_action, self.drag_payload, x, rect, commit=False)

    def on_canvas_up(self, event):
        if not self.drag_action:
            return

        action, payload, x, _y, rect = self.artwork.hit_test(event)
        self.handle_drag(self.drag_action, self.drag_payload, x, rect, commit=True)
        self.drag_action = None
        self.drag_payload = None

    def handle_drag(self, action, payload, x, rect, commit=False):
        if action == "gain_drag":
            left, _top, right, _bottom = 1429, 190, 1644, 226
            fraction = clamp((x - left) / (right - left), 0.0, 1.0)
            self.audio_send_gain.set(0.25 + fraction * 3.75)
            self.redraw()
            return

        if action == "speed_drag":
            slider = self.speed_slider_for_payload(payload)
            left, right = slider
            fraction = clamp((x - left) / (right - left), 0.0, 1.0)
            speed = int(round(fraction * 255))
            self.motor_speed = speed

            urls = [payload] if payload else self.target_urls()
            for url in urls:
                dev = self.devices.get(url)
                if dev:
                    dev.speed = speed

            self.redraw()

            if commit:
                self.command_targets(urls, f"/motor?speed={speed}")

        if action == "monitor_volume_drag":
            if not payload:
                return

            slider = self.monitor_volume_slider_for_payload(payload)
            left, right = slider
            fraction = clamp((x - left) / (right - left), 0.0, 1.0)
            dev = self.devices.get(payload)

            if dev:
                dev.monitor_volume = round(fraction * 1.5, 2)
                self.redraw()

    def speed_slider_for_payload(self, payload):
        if payload:
            devices = self.ordered_devices()

            for index, dev in enumerate(devices):
                if dev.url == payload:
                    x1, x2 = self.artwork.player_bounds[index]
                    left = x1 + 15
                    right = x2 - 18
                    return left + 6, right - 72

        return 1429, 1574

    def monitor_volume_slider_for_payload(self, payload):
        devices = self.ordered_devices()

        for index, dev in enumerate(devices):
            if dev.url == payload:
                x1, x2 = self.artwork.player_bounds[index]
                left = x1 + 15
                right = x2 - 18
                return left + 4, right - 74

        return 0, 1

    def handle_action(self, action, payload):
        if not action:
            return

        if action == "scan":
            self.start_discovery()
        elif action == "add_manual":
            self.add_manual_host(test=False)
        elif action == "add_test":
            self.add_manual_host(test=True)
        elif action == "select_all":
            self.selected_urls = set(self.devices)
            self.redraw()
        elif action == "clear_select":
            self.selected_urls.clear()
            self.redraw()
        elif action == "toggle_select" and payload:
            if payload in self.selected_urls:
                self.selected_urls.remove(payload)
            else:
                self.selected_urls.add(payload)
            self.redraw()
        elif action == "record_one" and payload:
            self.selected_urls = {payload}
            self.command_targets([payload], RECORD_PATH)
        elif action == "play_one" and payload:
            self.selected_urls = {payload}
            self.command_targets([payload], "/play")
        elif action == "record_selected":
            self.command_targets(self.target_urls(), RECORD_PATH)
        elif action == "play_selected":
            self.command_targets(self.target_urls(), "/play")
        elif action == "status_selected":
            self.command_targets(self.target_urls(), "/status")
        elif action == "stop_selected":
            self.command_targets(self.target_urls(), "/stop")
        elif action == "reverse_selected":
            self.command_targets(self.target_urls(), "/reverse/on")
        elif action == "forward_selected":
            self.command_targets(self.target_urls(), "/reverse/off")
        elif action == "reverse_one" and payload:
            self.command_targets([payload], "/reverse/on")
        elif action == "forward_one" and payload:
            self.command_targets([payload], "/reverse/off")
        elif action == "toggle_mute" and payload:
            dev = self.devices.get(payload)

            if dev:
                dev.monitor_muted = not dev.monitor_muted
                self.redraw()

    def select_all_event(self, event=None):
        widget = getattr(event, "widget", None)

        if widget is not None and widget.winfo_class() in ["Entry", "TEntry", "Text", "TCombobox"]:
            return None

        self.selected_urls = set(self.devices)
        self.redraw()
        return "break"

    def audio_source_display(self):
        value = self.audio_send_source.get().strip()

        if not value:
            return "--"

        return value[:24]

    def mixer_status(self):
        if self.mixer_running:
            return "Monitor on"

        if self.playable_mixer_urls():
            return "Monitor starting"

        return "Monitor idle"

    def monitor_volume_fraction(self, dev):
        if not dev:
            return 0.0

        return clamp(dev.monitor_volume / 1.5, 0.0, 1.0)

    def monitor_volume_label(self, dev):
        if not dev:
            return "--"

        return f"{dev.monitor_volume:.2f}x"

    def player_audio_level(self, dev):
        if not dev:
            return 0.0

        if dev.url in self.audio_send_targets:
            return self.audio_send_level

        with self.mixer_lock:
            source = self.mixer_sources.get(dev.url)

            if source:
                if dev.monitor_muted:
                    return 0.0

                return clamp(source.get("level", 0.0) * dev.monitor_volume, 0.0, 1.0)

        return 0.0

    def choose_audio_send_command(self):
        source = self.selected_audio_send_source_id()
        ffmpeg = shutil.which("ffmpeg")
        arecord = shutil.which("arecord")

        if sys.platform == "darwin":
            if not ffmpeg:
                raise RuntimeError("ffmpeg not found. Install ffmpeg to capture macOS audio.")

            if not source:
                source = "0"

            return [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "avfoundation",
                "-i", f":{source}",
                "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_RATE),
                "-f", "s16le",
                "pipe:1",
            ]

        if sys.platform.startswith("linux") and arecord:
            if not source:
                source = "default"

            return [
                arecord,
                "-q",
                "-D", source,
                "-f", "S16_LE",
                "-r", str(AUDIO_RATE),
                "-c", str(AUDIO_CHANNELS),
                "-t", "raw",
            ]

        if ffmpeg:
            if not source:
                source = "default"

            return [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "alsa",
                "-i", source,
                "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_RATE),
                "-f", "s16le",
                "pipe:1",
            ]

        raise RuntimeError("No local audio capture tool found. Install ffmpeg or alsa-utils.")

    def selected_audio_send_source_id(self):
        selected = self.audio_send_source.get().strip()
        return self.audio_send_source_map.get(selected, selected)

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

    def apply_pcm_gain(self, chunk, gain):
        if abs(gain - 1.0) < 0.001:
            return chunk

        samples = self.pcm_samples(chunk)

        for index, sample in enumerate(samples):
            samples[index] = int(clamp(sample * gain, -32768, 32767))

        if sys.byteorder != "little":
            samples.byteswap()

        return samples.tobytes()

    def set_audio_send_sources(self, sources):
        self.audio_send_source_map = {
            source["label"]: source["id"]
            for source in sources
        }
        labels = [source["label"] for source in sources]
        self.source_combo.configure(values=labels)

        current = self.audio_send_source.get().strip()
        id_to_label = {source["id"]: source["label"] for source in sources}

        if current in id_to_label:
            self.audio_send_source.set(id_to_label[current])
        elif labels and current not in labels:
            self.audio_send_source.set(labels[0])

        self.log(f"[AUDIO SEND] Found {len(labels)} local input source(s)")

    def parse_macos_avfoundation_sources(self, output):
        sources = []
        in_audio_section = False

        for line in output.splitlines():
            if "AVFoundation audio devices:" in line:
                in_audio_section = True
                continue

            if "AVFoundation video devices:" in line:
                in_audio_section = False
                continue

            if not in_audio_section:
                continue

            match = re.search(r"\[(\d+)\]\s+(.+)$", line)

            if not match:
                continue

            source_id, name = match.groups()
            sources.append({"id": source_id, "label": f"{source_id}: {name.strip()}"})

        return sources

    def parse_linux_alsa_sources(self, output):
        sources = [{"id": "default", "label": "default: ALSA default input"}]

        for line in output.splitlines():
            if not line or line[0].isspace():
                continue

            source_id = line.strip()

            if source_id == "null":
                continue

            if not any(source["id"] == source_id for source in sources):
                sources.append({"id": source_id, "label": source_id})

        return sources

    def list_local_audio_sources(self):
        thread = threading.Thread(target=self._list_local_audio_sources_worker, daemon=True)
        thread.start()

    def _list_local_audio_sources_worker(self):
        try:
            if sys.platform == "darwin":
                ffmpeg = shutil.which("ffmpeg")

                if not ffmpeg:
                    self.event_queue.put(("debug", "[AUDIO SEND] ffmpeg not found"))
                    return

                result = subprocess.run(
                    [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                output = (result.stderr or result.stdout or "").strip()
                self.event_queue.put(("audio_send_sources", self.parse_macos_avfoundation_sources(output)))
                return

            arecord = shutil.which("arecord")

            if arecord:
                result = subprocess.run(
                    [arecord, "-L"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                self.event_queue.put(("audio_send_sources", self.parse_linux_alsa_sources(result.stdout.strip())))
                return

            self.event_queue.put(("debug", "[AUDIO SEND] No local input lister found"))

        except Exception as e:
            self.event_queue.put(("debug", f"[AUDIO SEND] List inputs failed: {type(e).__name__}: {e}"))

    def start_audio_send(self, base_urls=None):
        if self.audio_send_running:
            new_targets = sorted(set(base_urls or self.target_urls()))

            if new_targets == sorted(self.audio_send_targets):
                self.log(f"[AUDIO SEND] Already sending to {len(new_targets)} recorder(s)")
                return

            self.stop_audio_send(quiet=True)

        try:
            target_urls = sorted(set(base_urls or self.target_urls()))
            cmd = self.choose_audio_send_command()

        except Exception as e:
            self.log(f"[AUDIO SEND] {e}")
            return

        if not target_urls:
            self.log("[AUDIO SEND] No target recorders")
            return

        output_device = quote(self.audio_output_device.get().strip() or "auto", safe="")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except Exception as e:
            self.log(f"[AUDIO SEND] Could not start local capture: {type(e).__name__}: {e}")
            return

        self.audio_send_generation += 1
        generation = self.audio_send_generation
        stop_event = threading.Event()
        queues = {base_url: queue.Queue(maxsize=32) for base_url in target_urls}
        self.audio_send_stop_event = stop_event
        self.audio_send_process = proc
        self.audio_send_targets = target_urls
        self.audio_send_queues = queues
        self.audio_send_post_threads = []
        self.audio_send_running = True
        self.audio_send_status.set(f"Sending {len(target_urls)}")
        self.log(f"[AUDIO SEND] Sending to {len(target_urls)} recorder(s)")

        for base_url in target_urls:
            post_url = self.build_url_for(
                base_url,
                f"/audio/playback?device={output_device}&rate={AUDIO_RATE}&channels={AUDIO_CHANNELS}",
            )
            post_thread = threading.Thread(
                target=self._audio_send_post_worker,
                args=(base_url, post_url, queues[base_url], stop_event),
                daemon=True,
            )
            self.audio_send_post_threads.append(post_thread)
            post_thread.start()

        self.audio_send_thread = threading.Thread(
            target=self._audio_send_capture_worker,
            args=(generation, proc, queues, stop_event),
            daemon=True,
        )
        self.audio_send_thread.start()

    def enqueue_audio_send_chunk(self, chunk, queues, stop_event):
        for target_queue in list(queues.values()):
            if stop_event.is_set():
                return

            try:
                target_queue.put(chunk, timeout=0.1)
            except queue.Full:
                try:
                    target_queue.get_nowait()
                    target_queue.put_nowait(chunk)
                except Exception:
                    pass

    def finish_audio_send_queues(self, queues=None):
        target_queues = queues or self.audio_send_queues

        for target_queue in list(target_queues.values()):
            try:
                target_queue.put_nowait(None)
            except queue.Full:
                try:
                    target_queue.get_nowait()
                    target_queue.put_nowait(None)
                except Exception:
                    pass

    def _audio_send_capture_worker(self, generation, proc, queues, stop_event):
        total_bytes = 0

        try:
            while not stop_event.is_set():
                chunk = proc.stdout.read(AUDIO_CHUNK_BYTES)

                if not chunk:
                    break

                output_chunk = self.apply_pcm_gain(chunk, self.audio_send_gain.get())
                total_bytes += len(output_chunk)
                self.audio_send_level = self.pcm_level(self.pcm_samples(output_chunk))
                self.enqueue_audio_send_chunk(output_chunk, queues, stop_event)

        except Exception as e:
            if not stop_event.is_set():
                self.event_queue.put(("debug", f"[AUDIO SEND] Capture error: {type(e).__name__}: {e}"))

        finally:
            stop_event.set()
            self.finish_audio_send_queues(queues)

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
                self.event_queue.put(("debug", f"[AUDIO SEND] Capture stderr:\n{stderr[-2000:]}"))

            self.event_queue.put(("audio_send_stopped", (generation, f"Send idle ({total_bytes} bytes)")))

    def _audio_send_post_worker(self, base_url, post_url, target_queue, stop_event):
        def generate():
            while not stop_event.is_set():
                try:
                    chunk = target_queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                if chunk is None:
                    break

                yield chunk

        try:
            response = requests.post(
                post_url,
                data=generate(),
                headers={"Content-Type": "application/octet-stream"},
                timeout=(REQUEST_TIMEOUT, 3600),
            )
            self.event_queue.put(("debug", f"[AUDIO SEND] {base_url} HTTP {response.status_code}"))

        except Exception as e:
            if not stop_event.is_set():
                self.event_queue.put(("debug", f"[AUDIO SEND] {base_url} error: {type(e).__name__}: {e}"))

    def stop_audio_send(self, quiet=False):
        if not self.audio_send_running:
            if not quiet:
                self.log("[AUDIO SEND] Not sending")
            return

        self.audio_send_status.set("Stopping")
        self.audio_send_stop_event.set()

        if self.audio_send_process and self.audio_send_process.poll() is None:
            self.audio_send_process.terminate()

        self.finish_audio_send_queues()

        if not quiet:
            self.log("[AUDIO SEND] Stopping")

    def update_meters(self):
        self.redraw()
        self.root.after(100, self.update_meters)

    def toggle_fullscreen(self, _event=None):
        if self.fullscreen:
            self.enter_windowed()
        else:
            self.enter_fullscreen()
        return "break"

    def enter_fullscreen(self):
        self.fullscreen = True
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()

    def enter_windowed(self):
        self.fullscreen = False
        self.root.attributes("-topmost", False)
        self.root.overrideredirect(False)
        self.root.geometry(f"{DESIGN_WIDTH}x{DESIGN_HEIGHT}")
        self.root.minsize(DESIGN_WIDTH, DESIGN_HEIGHT)

    def quit(self):
        self.stop_audio_send(quiet=True)
        self.stop_audio_monitor(quiet=True)

        if self.zeroconf is not None:
            try:
                self.zeroconf.close()
            except Exception:
                pass

        self.root.destroy()


def parse_args():
    parser = argparse.ArgumentParser(description="Run the cassette performance controller.")
    parser.add_argument("--fullscreen", action="store_true", help="open fullscreen")
    return parser.parse_args()


def main():
    args = parse_args()
    root = tk.Tk()
    PerformanceGUI(root, windowed=not args.fullscreen)
    root.mainloop()


if __name__ == "__main__":
    main()
