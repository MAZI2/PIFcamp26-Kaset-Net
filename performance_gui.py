#!/usr/bin/env python3
"""Standalone, non-integrated mockup of the cassette central mixer UI.

The reference artwork is laid out on a 1680 x 945 coordinate grid and scaled
uniformly onto the laptop's 1366 x 768 display.  The design itself never grows:
on a larger monitor, fullscreen mode only adds black space around the centered
1366 x 768 canvas.

This file deliberately contains no embedded recorder, network, audio, or motor
logic. Press F2 to open the live `recorder_gui.py` controller from this branch.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass


REFERENCE_WIDTH = 1680
REFERENCE_HEIGHT = 945
DESIGN_WIDTH = 1366
DESIGN_HEIGHT = 768
PROJECT_DIR = pathlib.Path(__file__).resolve().parent
LIVE_CONTROLLER = PROJECT_DIR / "recorder_gui.py"

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
CONTROL = "#171919"
CONTROL_EDGE = "#626565"
FADER = "#d1d1d1"


@dataclass(frozen=True)
class Player:
    number: int
    ip: str


PLAYERS = (
    Player(1, "192.168.0.9"),
    Player(2, "192.168.0.10"),
    Player(3, "192.168.0.11"),
    Player(4, "192.168.0.12"),
)


class MixerArtwork:
    """Draw the whole interface from a single reference coordinate system."""

    def __init__(self, canvas: tk.Canvas, width: int, height: int) -> None:
        self.canvas = canvas
        self.width = width
        self.height = height
        self.scale = min(width / REFERENCE_WIDTH, height / REFERENCE_HEIGHT)
        self.offset_x = (width - REFERENCE_WIDTH * self.scale) / 2
        self.offset_y = (height - REFERENCE_HEIGHT * self.scale) / 2

    def sx(self, value: float) -> float:
        return self.offset_x + value * self.scale

    def sy(self, value: float) -> float:
        return self.offset_y + value * self.scale

    def font(self, size: int, bold: bool = False) -> tuple[str, int, str]:
        # Negative Tk sizes are pixels, which avoids desktop DPI-dependent drift.
        pixels = max(8, round(size * self.scale))
        return ("DejaVu Sans Mono", -pixels, "bold" if bold else "normal")

    def rect(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        fill: str = "",
        outline: str = PANEL_EDGE,
        width: float = 1,
    ) -> None:
        self.canvas.create_rectangle(
            self.sx(x1),
            self.sy(y1),
            self.sx(x2),
            self.sy(y2),
            fill=fill,
            outline=outline,
            width=max(1, round(width * self.scale)),
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        fill: str = DIVIDER,
        width: float = 1,
    ) -> None:
        self.canvas.create_line(
            self.sx(x1),
            self.sy(y1),
            self.sx(x2),
            self.sy(y2),
            fill=fill,
            width=max(1, round(width * self.scale)),
        )

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: int = 15,
        fill: str = TEXT,
        anchor: str = "nw",
        bold: bool = False,
    ) -> None:
        self.canvas.create_text(
            self.sx(x),
            self.sy(y),
            text=value,
            fill=fill,
            font=self.font(size, bold),
            anchor=anchor,
        )

    def dot(self, x: float, y: float, radius: float = 6, fill: str = GREEN) -> None:
        self.canvas.create_oval(
            self.sx(x - radius),
            self.sy(y - radius),
            self.sx(x + radius),
            self.sy(y + radius),
            fill=fill,
            outline="",
        )

    def button(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        label: str,
        *,
        color: str = TEXT,
        outline: str = CONTROL_EDGE,
        size: int = 15,
    ) -> None:
        self.rect(x1, y1, x2, y2, fill=CONTROL, outline=outline)
        self.text(
            (x1 + x2) / 2,
            (y1 + y2) / 2,
            label,
            size=size,
            fill=color,
            anchor="center",
        )

    def panel(self, x1: float, x2: float) -> None:
        self.rect(x1, 13, x2, 883, fill=PANEL, outline=PANEL_EDGE)

    def divider(self, x1: float, x2: float, y: float) -> None:
        self.line(x1, y, x2, y)

    def meter(self, x: float, top: float, bottom: float, lit_fraction: float = 1.0) -> None:
        segment_h = 5
        gap = 2
        total = int((bottom - top) // (segment_h + gap))
        lit = round(total * lit_fraction)
        for index in range(total):
            y2 = bottom - index * (segment_h + gap)
            y1 = y2 - segment_h
            color = GREEN_METER if index < lit else GREEN_DARK
            self.rect(x, y1, x + 14, y2, fill=color, outline=color)

    def vertical_fader(self, x: float, top: float, bottom: float, value_y: float) -> None:
        self.line(x, top, x, bottom, fill=FADER, width=3)
        for y in (top, top + 36, top + 72, top + 108, top + 144, bottom):
            self.line(x - 20, y, x - 15, y, fill=DIM)
        self.rect(x - 12, value_y - 7, x + 12, value_y + 7, fill=FADER, outline=FADER)

    def horizontal_fader(
        self,
        x1: float,
        x2: float,
        y: float,
        value_x: float,
        *,
        small: bool = False,
    ) -> None:
        self.line(x1, y, x2, y, fill=FADER, width=3)
        tick_top = y + (10 if small else 11)
        for index in range(8):
            x = x1 + (x2 - x1) * index / 7
            length = 6 if index in (0, 7) else 4
            self.line(x, tick_top, x, tick_top + length, fill=DIM)
        half = 8 if small else 12
        self.rect(value_x - 7, y - half, value_x + 7, y + half, fill=FADER, outline=FADER)

    def draw(self) -> None:
        self.canvas.delete("all")
        self.canvas.configure(bg=BG)
        self.draw_connections()
        player_bounds = ((283, 569), (582, 871), (885, 1149), (1162, 1397))
        for player, (x1, x2) in zip(PLAYERS, player_bounds):
            self.draw_player(x1, x2, player)
        self.draw_master()
        self.draw_status_bar()

    def draw_connections(self) -> None:
        x1, x2 = 13, 265
        self.panel(x1, x2)
        self.text(35, 32, "CONNECTIONS", size=17, bold=True)
        self.divider(30, 241, 63)
        self.text(35, 80, "CONNECTED PLAYERS (4)", size=14, fill=MUTED)

        rows = (
            (129, "1", "192.168.0.9"),
            (160, "2", "192.168.0.10"),
            (191, "3", "192.168.0.11"),
            (222, "4", "192.168.0.12"),
        )
        for y, number, ip in rows:
            self.dot(36, y, radius=6)
            self.text(56, y, number, size=14, anchor="w")
            self.text(89, y, ip, size=14, anchor="w")

        self.button(31, 276, 126, 317, "REFRESH", size=14)
        self.button(144, 276, 243, 317, "SCAN", size=14)
        self.text(35, 349, "--", size=15, fill=DIM)

        self.text(35, 397, "ADD PLAYER", size=15)
        self.rect(30, 427, 245, 468, fill=CONTROL, outline=CONTROL_EDGE)
        self.text(39, 448, "IP or URL", size=14, fill="#686a6b", anchor="w")
        self.button(30, 487, 126, 532, "ADD", size=14)
        self.button(143, 487, 245, 532, "ADD + TEST", size=14)
        self.text(35, 561, "--", size=15, fill=DIM)
        self.divider(35, 240, 588)

        self.text(35, 613, "GLOBAL ACTIONS", size=15)
        self.button(30, 650, 245, 694, "POWER ON ALL", size=14)
        self.button(30, 717, 245, 761, "POWER OFF ALL", size=14)

    def draw_player(self, x1: float, x2: float, player: Player) -> None:
        self.panel(x1, x2)
        left = x1 + 15
        right = x2 - 18
        center = (x1 + x2) / 2

        self.text(left, 31, f"PLAYER {player.number}", size=17, fill=GREEN, bold=True)
        self.text(left, 58, player.ip, size=16, fill=GREEN)
        self.divider(left, right, 89)

        self.text(left, 116, "RECORD HEAD", size=15)
        available = right - left
        gap = 15
        button_w = (available - 2 * gap) / 3
        self.button(left + 2, 146, left + button_w, 188, "REC", color=RED, outline=RED_DARK)
        self.button(
            left + button_w + gap,
            146,
            left + 2 * button_w + gap,
            188,
            "PLAY",
            color=GREEN,
            outline="#467c30",
        )
        self.button(right - button_w, 146, right, 188, "STOP")
        self.divider(left, right, 201)

        self.text(left, 220, "INPUT (MIC -> TAPE)", size=15)
        fader_x = center - 13
        meter_x = right - 53
        label_x = fader_x - 69
        for y, label in ((260, "+12"), (295, "+6"), (331, "0"), (367, "-6"), (403, "-12"), (439, "--∞")):
            self.text(label_x, y, label, size=13, anchor="center")
        self.vertical_fader(fader_x, 257, 440, 332)
        self.meter(meter_x, 257, 441, lit_fraction=0.98)
        self.text(center, 467, "GAIN  0.0 dB", size=15, fill=GREEN, anchor="center")
        self.divider(left, right, 493)

        self.text(left, 509, "MOTOR", size=15)
        self.text(left, 546, "SPEED", size=14)
        slider_left = left + 6
        slider_right = right - 72
        self.horizontal_fader(slider_left, slider_right, 584, (slider_left + slider_right) / 2)
        self.text(slider_left, 609, "0", size=12, anchor="center")
        self.text(slider_right, 609, "100", size=12, anchor="center")
        self.rect(right - 48, 571, right, 601, fill=CONTROL, outline="#454848")
        self.text(right - 24, 586, "50", size=14, fill=GREEN, anchor="center")

        self.text(left, 656, "DIRECTION", size=15)
        direction_gap = 15
        direction_w = (available - 2 * direction_gap) / 3
        self.button(left, 687, left + direction_w, 730, "◀ REV", size=14)
        self.button(
            left + direction_w + direction_gap,
            687,
            left + 2 * direction_w + direction_gap,
            730,
            "STOP",
            size=14,
        )
        self.button(right - direction_w, 687, right, 730, "FWD ▶", size=14)
        self.divider(left, right, 743)

        self.text(left, 764, "STATUS", size=15)
        self.rect(left, 791, right + 3, 863, fill="#111414", outline="#454848")
        self.text(left + 10, 802, "Power: ON\nMode:  STOP\nSpeed: 0", size=14, fill=GREEN)
        self.dot(right - 18, 808, radius=6)

    def draw_master(self) -> None:
        x1, x2 = 1411, 1658
        self.panel(x1, x2)
        left, right = 1425, 1644
        center = (x1 + x2) / 2

        self.text(center, 31, "MASTER", size=17, bold=True, anchor="n")
        self.divider(left, right, 62)
        self.text(left, 79, "MASTER OUTPUT", size=14)

        fader_x = 1531
        meter_x = 1612
        for y, label in ((121, "+12"), (159, "+6"), (196, "0"), (232, "-6"), (268, "-12"), (304, "--∞")):
            self.text(1458, y, label, size=13, anchor="center")
        self.vertical_fader(fader_x, 118, 308, 196)
        self.meter(meter_x, 118, 309, lit_fraction=0.98)
        self.text(fader_x, 335, "0.0 dB", size=15, fill=GREEN, anchor="center")
        self.divider(left, right, 362)

        self.text(left, 379, "TRANSPORT (ALL)", size=15)
        self.button(left, 407, right + 1, 445, "STOP ALL", color=RED, outline=RED_DARK, size=14)
        self.button(left, 453, right + 1, 492, "PLAY ALL", color=GREEN, outline="#467c30", size=14)
        self.button(left, 499, right + 1, 538, "REC ALL", color=RED, outline=RED_DARK, size=14)
        self.divider(left, right, 550)

        self.text(left, 561, "MOTOR (ALL)", size=15)
        self.text(left, 588, "SPEED", size=14)
        slider_left, slider_right = 1429, 1574
        self.horizontal_fader(slider_left, slider_right, 620, 1504, small=True)
        self.text(slider_left, 645, "0", size=12, anchor="center")
        self.text(slider_right, 645, "100", size=12, anchor="center")
        self.rect(1601, 608, 1644, 638, fill=CONTROL, outline="#454848")
        self.text(1622, 623, "50", size=14, fill=GREEN, anchor="center")

        self.text(left, 662, "DIRECTION", size=14)
        gap = 10
        width = (right - left - 2 * gap) / 3
        self.button(left, 687, left + width, 728, "◀ REV", size=13)
        self.button(left + width + gap, 687, left + 2 * width + gap, 728, "STOP", size=13)
        self.button(right - width, 687, right + 5, 728, "FWD ▶", size=13)
        self.divider(left, right, 739)

        self.text(left, 750, "SYSTEM", size=15)
        self.button(left, 776, right + 1, 815, "STATUS ALL", size=14)
        self.button(left, 823, right + 1, 863, "STOP ALL MOTORS", color=RED, outline=RED_DARK, size=14)

    def draw_status_bar(self) -> None:
        self.rect(13, 895, 1658, 935, fill=PANEL, outline=PANEL_EDGE)
        self.text(27, 915, "CENTRAL MIXER INTERFACE", size=13, anchor="w")
        self.text(262, 915, "|", size=13, fill=MUTED, anchor="center")
        self.text(299, 915, "4 PLAYERS CONNECTED", size=13, fill=GREEN, anchor="w")
        self.text(650, 915, "MIC INPUT: ", size=13, anchor="w")
        self.text(752, 915, "ALSA (hw:1,0)", size=13, fill=GREEN, anchor="w")
        self.text(894, 915, "|", size=13, fill=MUTED, anchor="center")
        self.text(938, 915, "TIME: 14:32:18", size=13, anchor="w")
        self.text(1628, 915, "F2: LIVE CONTROLLER", size=13, anchor="e")


class PerformanceGUI:
    def __init__(self, root: tk.Tk, *, windowed: bool = True) -> None:
        self.root = root
        self.windowed = windowed
        self.fullscreen = not windowed
        self.root.title("Central Mixer Interface — UI Mockup")
        self.root.configure(bg="black")

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
        self.artwork = MixerArtwork(self.canvas, DESIGN_WIDTH, DESIGN_HEIGHT)
        self.artwork.draw()

        self.root.bind("<Escape>", lambda _event: self.root.destroy())
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<F2>", self.open_live_controller)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        if windowed:
            self.enter_windowed()
        else:
            self.enter_fullscreen()

    def toggle_fullscreen(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        if self.fullscreen:
            self.enter_windowed()
        else:
            self.enter_fullscreen()
        return "break"

    def enter_fullscreen(self) -> None:
        """Use the physical screen, including space reserved by desktop panels."""
        self.fullscreen = True
        self.root.overrideredirect(True)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"{screen_width}x{screen_height}+0+0")
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()

    def enter_windowed(self) -> None:
        self.fullscreen = False
        self.root.attributes("-topmost", False)
        self.root.overrideredirect(False)
        self.root.geometry(f"{DESIGN_WIDTH}x{DESIGN_HEIGHT}")
        self.root.minsize(DESIGN_WIDTH, DESIGN_HEIGHT)

    def open_live_controller(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        subprocess.Popen(
            [sys.executable, str(LIVE_CONTROLLER)],
            cwd=str(PROJECT_DIR),
        )
        return "break"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show the standalone central mixer UI mockup.")
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="open fullscreen instead of in a fixed 1366x768 window",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = tk.Tk()
    PerformanceGUI(root, windowed=not args.fullscreen)
    root.mainloop()


if __name__ == "__main__":
    main()
