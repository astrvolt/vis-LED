#!/usr/bin/env python3
"""
LED panel widget — single-file desktop port of vis.html.

An audio-reactive LED grid: per-cell exponential decay, a ring-delayed
"liquid" bleed kernel between neighbors, squircle-shaped cells with
radial glow, a subtle CRT-style flicker, a typewriter console line,
system-audio-driven frequency bars, Spotify cover-art-driven coloring,
and a silence-triggered random color switch — all ported formula-for-
formula from the original canvas/JS version.

Run directly:
    python3 panel_widget.py

On first run it checks for its own dependencies (PySide6, numpy,
requests, soundcard, Pillow) and pip-installs anything missing before
doing anything else, then restarts itself once so the fresh install is
picked up cleanly. Nothing else needs to be installed by hand.

The process then detaches from the launching terminal/parent, so
closing that terminal does not kill the widget.

Hotkeys once running:
    Ctrl+E          settings (spotify connect, target sound device, default color)
    Ctrl+F          toggle fullscreen (Esc also exits fullscreen)
    Win+Shift+W     bring the widget to front if it isn't already on
                    top; hide it if it's already visible and frontmost
    Ctrl+K          quit
Drag the background to move the window; drag the bottom-right corner
to resize (aspect ratio is locked to the panel's own grid ratio).
In fullscreen the visualizer is vertically centered with the console
and transport controls pinned at the bottom, and the cursor
auto-hides after 3s of no mouse movement (any movement brings it
back).

Spotify setup (optional):
    Paste your own Spotify app's Client ID into SPOTIFY_CLIENT_ID
    below, and register REDIRECT_URI (the hosted auth.html page, not
    a bare localhost URL) as that app's Redirect URI in the Spotify
    developer dashboard. auth.html hands the resulting code off to
    this process over 127.0.0.1:8945/status, authenticated by a
    per-attempt state value — see _CallbackHandler for why that
    matters.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import traceback


def _is_frozen() -> bool:
    """
    True when running as a PyInstaller-built exe/app rather than a
    plain "python panel_widget.py" launch. PyInstaller sets sys.frozen
    on the frozen executable; there's no such attribute in a normal
    interpreter run. Both the self-installing bootstrap and the
    detach-from-terminal dance below only make sense for the latter —
    a frozen build already ships its dependencies baked in and is
    already detached from any terminal the moment it's double-clicked
    — so both are skipped entirely when this is true.
    """
    return getattr(sys, "frozen", False)


def _app_dir() -> str:
    """
    Directory to treat as "next to the app" for user-visible files like
    the crash log. For a frozen build this is the directory containing
    the actual exe/app (sys.executable), not a temp extraction path or
    whatever the OS happened to set as the current working directory
    when it was launched (which, for a double-clicked app, is often
    unpredictable — the user's home dir, "/", a mounted DMG, etc.). For
    a plain script run it's the directory containing this file.
    """
    if _is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CRASH_LOG_PATH = os.path.join(_app_dir(), "panel_widget_crash.log")


def _uncaught_exception_handler(exctype, value, tb):
    with open(CRASH_LOG_PATH, "w") as f:
        f.write("Uncaught Exception:\n")
        traceback.print_exception(exctype, value, tb, file=f)

sys.excepthook = _uncaught_exception_handler


# ============================================================================
# SELF-INSTALLING BOOTSTRAP — runs before any third-party import.
#
# Checks each dependency with a plain importlib probe (no import side
# effects yet), pip-installs whatever is missing, then re-execs this
# same script once so the freshly installed packages are picked up by
# a clean interpreter start rather than relying on the current
# process seeing them retroactively. The re-exec is guarded by an env
# var so it only ever happens once per launch, even if something is
# still missing afterward (in which case the real ImportError below
# surfaces normally instead of looping).
#
# Skipped entirely in a frozen build (see _is_frozen): sys.executable
# there is the packaged exe itself, not a python interpreter with pip
# available, and every dependency is already bundled in by PyInstaller
# — so both the probe and the os.execve re-exec below are not just
# unnecessary but actively broken (execve-ing the exe against itself
# with a script path as an argument is not a valid relaunch).
# ============================================================================

_REQUIRED = {
    "PySide6": "PySide6",
    "numpy": "numpy",
    "requests": "requests",
    "soundcard": "soundcard",
    "PIL": "Pillow",
    "pynput": "pynput",
}


def _ensure_dependencies() -> None:
    if _is_frozen():
        return

    if os.environ.get("_PANEL_DEPS_CHECKED") == "1":
        return

    missing_pip_names = []
    for module_name, pip_name in _REQUIRED.items():
        if importlib.util.find_spec(module_name) is None:
            missing_pip_names.append(pip_name)

    if not missing_pip_names:
        # Nothing to install — mark checked so a later re-exec (e.g. after
        # the detach fork below) doesn't redo this probe, but don't
        # restart the process for no reason.
        os.environ["_PANEL_DEPS_CHECKED"] = "1"
        return

    print(f"[panel_widget] installing missing dependencies: {', '.join(missing_pip_names)}")
    base_cmd = [sys.executable, "-m", "pip", "install", "--quiet", *missing_pip_names]
    try:
        subprocess.check_call([*base_cmd, "--break-system-packages"])
    except subprocess.CalledProcessError:
        # --break-system-packages isn't recognized by every pip
        # (older versions, some non-Debian environments) — retry
        # plainly rather than failing outright.
        subprocess.check_call(base_cmd)

    # Something was actually installed — re-exec once so this interpreter
    # starts fresh with the newly installed packages importable from the
    # top, rather than relying on the current process seeing them
    # retroactively (which import caching can't guarantee).
    env = os.environ.copy()
    env["_PANEL_DEPS_CHECKED"] = "1"
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env)


_ensure_dependencies()

import base64
import hashlib
import http.server
import io
import json
import math
import random
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

try:
    from pynput import keyboard
except ImportError:
    keyboard = None


import numpy as np
import requests

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, QTimer, Qt, QEvent,
    Signal, Property,
)
from PySide6.QtGui import (
    QBrush, QColor, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QRadialGradient,
    QRegion, QShortcut, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QDialog, QFrame, QGraphicsOpacityEffect,
    QHBoxLayout, QLabel, QPushButton, QCheckBox, QVBoxLayout, QWidget,
)

try:
    import soundcard as sc
except Exception:  # pragma: no cover - soundcard needs real audio hardware/daemon
    sc = None

try:
    from PIL import Image
except Exception:
    Image = None

# ============================================================================
# CORE ENGINE — grid state, decay, bleed kernel, color crossfade, text segmenting
# ============================================================================

# ---------------------------------------------------------------------------
# Panel configuration — identical constants/names to the JS source.
# ---------------------------------------------------------------------------

GRID_W = 40
GRID_H = 8
GRID_PX_W = 1024                       # base backing resolution for the grid content, excludes padding
CELL = GRID_PX_W / GRID_W              # px per cell at base resolution
GLOW_PAD = 0.75                        # margin around the grid, in cell-units
PAD = CELL * GLOW_PAD                  # px of margin added to every side
CANVAS_W = GRID_PX_W + PAD * 2
CANVAS_H = CELL * GRID_H + PAD * 2     # keep cells square

MAX_LEVEL = 9                          # levels run 0-9, each step = 11% (9 -> 99%)
LEVEL_STEP = 0.11

LEAK_NEAR = 0.18
LEAK_DIAG = 0.12
LEAK_FAR = 0.06
DEATH_FLOOR = 0.015

DECAY_TAU = 700.0                      # ms, exponential decay time constant
BLEED_HOP_DELAY = 20.0                 # ms, extra delay per "ring" a leak travels outward
BLEED_RAMP = 140.0                     # ms, ease-in duration once a leak arrives

DEFAULT_LED_RGB = [222, 222, 222]
COLOR_TRANSITION_MS = 500.0

SILENCE_THRESHOLD = 0.015
SILENCE_HOLD_MS = 1500.0
SILENCE_COLOR_TRANSITION_MS = 1200.0

# Timestamp / transport-controls pill: the two share one footprint in
# controls_stack (see PanelWindow.__init__) and swap via opacity fade
# rather than ever being visible together.
STACK_FADE_MS = 200                 # fade_in()/fade_out() duration for both widgets
STACK_SWITCH_GAP_MS = 100           # pause after one fully fades out before the other fades in

# TimestampLabel's accent color tracks the panel's live LED color (see
# led_color_fn). A short burst of high-frequency repaints makes that
# tracking read as a smooth transition instead of a once-a-second step;
# it runs continuously (not just during an active crossfade) so the
# accent color never has to wait for the once-a-second time tick to
# catch up — a settled color just keeps re-painting the same value,
# which is cheap (see the QPalette note on TimestampLabel.__init__).
TIMESTAMP_COLOR_TICK_MS = 100       # 10fps colour refresh, runs continuously

MAX_HZ = 20000
BAND_COUNT = GRID_W // 2               # one band per bar column, dividers excluded

EDGE_TAPER = 0.35
EDGE_TAPER_SPAN = 4

NEON_PALETTE = [
    (255, 0, 110), (255, 0, 60), (255, 45, 0), (255, 110, 0), (255, 170, 0),
    (255, 230, 0), (200, 255, 0), (110, 255, 0), (0, 255, 60), (0, 255, 140),
    (0, 255, 200), (0, 255, 255), (0, 200, 255), (0, 140, 255), (0, 80, 255),
    (60, 0, 255), (120, 0, 255), (180, 0, 255), (230, 0, 255), (255, 0, 220),
    (255, 0, 170), (255, 60, 60), (255, 220, 60), (60, 255, 60), (60, 220, 255),
    (180, 60, 255), (255, 60, 180), (0, 255, 100), (255, 130, 0), (130, 255, 0),
]

ACCENT_MIN_LIGHTNESS = 0.45
ACCENT_MIN_SATURATION = 0.5


def ease(t: float) -> float:
    """Smoothstep 0->1 ramp, identical to JS ease()."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return t * t * (3 - 2 * t)


def with_death_floor(b: float) -> float:
    return 0.0 if b < DEATH_FLOOR else b


def _build_kernel():
    """Offset -> (dx, dy, weight, ring). Same rings/weights as buildKernel()."""
    kernel = []
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if dx == 0 and dy == 0:
                continue
            dist = math.hypot(dx, dy)
            if dist <= 1.0:
                w, ring = LEAK_NEAR, 1
            elif dist <= 1.5:
                w, ring = LEAK_DIAG, 1
            elif dist <= 2.01:
                w, ring = LEAK_FAR, 2
            else:
                continue
            kernel.append((dx, dy, w, ring))
    return kernel


KERNEL = _build_kernel()
KERNEL_NEAR_ONLY = [k for k in KERNEL if k[3] == 1]  # ring==1 only — drops the outer "far" leak ring


def rgb_to_hsl(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    h = s = 0.0
    d = mx - mn
    if d != 0:
        s = d / (1 - abs(2 * l - 1))
        if mx == r:
            h = 60 * (((g - b) / d) % 6)
        elif mx == g:
            h = 60 * ((b - r) / d + 2)
        else:
            h = 60 * ((r - g) / d + 4)
        if h < 0:
            h += 360
    return h, s, l


def hsl_to_rgb(h, s, l):
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    if h < 60:
        rp, gp, bp = c, x, 0
    elif h < 120:
        rp, gp, bp = x, c, 0
    elif h < 180:
        rp, gp, bp = 0, c, x
    elif h < 240:
        rp, gp, bp = 0, x, c
    elif h < 300:
        rp, gp, bp = x, 0, c
    else:
        rp, gp, bp = c, 0, x
    return (
        round((rp + m) * 255),
        round((gp + m) * 255),
        round((bp + m) * 255),
    )


def edge_curve(band: int) -> float:
    dist_from_edge = min(band, BAND_COUNT - 1 - band)
    if dist_from_edge >= EDGE_TAPER_SPAN:
        return 1.0
    t = dist_from_edge / EDGE_TAPER_SPAN
    eased = t * t * (3 - 2 * t)
    return 1 - EDGE_TAPER * (1 - eased)


def build_band_edges(bin_count: int, sample_rate: float):
    hz_per_bin = sample_rate / 2 / bin_count
    min_bin = 1
    max_bin = min(bin_count - 1, int(MAX_HZ / hz_per_bin))
    edges = [0] * (BAND_COUNT + 1)
    for i in range(BAND_COUNT + 1):
        t = i / BAND_COUNT
        edges[i] = round(min_bin * (max_bin / min_bin) ** t)
    return edges


@dataclass
class TextSegment:
    text: str
    accent: bool


def parse_segments(statement: str) -> list[TextSegment]:
    """Split on **word** into accent/non-accent runs, same as parseSegments()."""
    segments: list[TextSegment] = []
    last = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", statement):
        if m.start() > last:
            segments.append(TextSegment(statement[last:m.start()], False))
        segments.append(TextSegment(m.group(1), True))
        last = m.end()
    if last < len(statement):
        segments.append(TextSegment(statement[last:], False))
    return segments


class LedPanel:
    """
    Holds per-cell level/timing state and computes the decayed+bled
    brightness field. Mirrors levels/setAt/held/litSince/field and the
    led()/recompute() logic exactly.
    """

    def __init__(self):
        n = GRID_W * GRID_H
        self.levels = [0.0] * n
        self.set_at = [0.0] * n
        self.held = [0] * n
        self.lit_since = [0.0] * n
        self.field = [0.0] * n

        # color crossfade state
        self.color_from = list(DEFAULT_LED_RGB)
        self.color_to = list(DEFAULT_LED_RGB)
        self.color_start_at = 0.0
        self.color_duration = 0.0

        # Low Performance Mode: drops the kernel's outer "far" ring (a
        # 5x5 -> immediate-neighbors-only bleed), cutting recompute()'s
        # per-lit-cell inner loop from up to 20 iterations down to 8.
        # The liquid-bleed effect is still there, just shorter-reaching
        # — not the same as turning it off.
        self.low_perf_mode = False

    @staticmethod
    def idx(x: int, y: int) -> int:
        return y * GRID_W + x

    @staticmethod
    def in_bounds(x: int, y: int) -> bool:
        return 0 <= x < GRID_W and 0 <= y < GRID_H

    def _decayed_norm(self, x: int, y: int, now: float) -> float:
        i = self.idx(x, y)
        lvl = self.levels[i]
        if lvl <= 0:
            return 0.0
        base = lvl * LEVEL_STEP
        if self.held[i]:
            return base
        elapsed = now - self.set_at[i]
        if elapsed <= 0:
            return base
        return base * math.exp(-elapsed / DECAY_TAU)

    def recompute(self, now: float) -> None:
        field_ = self.field
        for i in range(len(field_)):
            field_[i] = 0.0

        for y in range(GRID_H):
            for x in range(GRID_W):
                si = self.idx(x, y)
                lvl = self.levels[si]
                if lvl <= 0:
                    continue

                norm = with_death_floor(self._decayed_norm(x, y, now))
                if norm > 0 and norm > field_[si]:
                    field_[si] = norm

                lit_elapsed = now - self.lit_since[si]

                kernel = KERNEL_NEAR_ONLY if self.low_perf_mode else KERNEL
                for dx, dy, w, ring in kernel:
                    nx, ny = x + dx, y + dy
                    if not self.in_bounds(nx, ny):
                        continue

                    arrive_delay = ring * BLEED_HOP_DELAY
                    emitted_ago = lit_elapsed - arrive_delay
                    if emitted_ago <= 0:
                        continue

                    source_norm_at_emit = with_death_floor(norm)
                    if source_norm_at_emit <= 0:
                        continue

                    ramp_t = ease(emitted_ago / BLEED_RAMP)
                    contrib = with_death_floor(source_norm_at_emit * w * ramp_t)
                    if contrib <= 0:
                        continue

                    ni = self.idx(nx, ny)
                    if contrib > field_[ni]:
                        field_[ni] = contrib

    def set_pixel(self, x: int, y: int, level: float, now: float, no_decay: bool = False) -> None:
        if not self.in_bounds(x, y):
            return
        level = max(0, min(MAX_LEVEL, round(level)))
        i = self.idx(x, y)
        if level > self.levels[i]:
            self.lit_since[i] = now
        elif level <= 0:
            self.lit_since[i] = 0.0
        self.levels[i] = level
        self.set_at[i] = now
        self.held[i] = 1 if no_decay else 0

    def led(self, a, b=None, c=None, d=None, now: Optional[float] = None) -> None:
        """
        Mirrors the JS led() overloads:
          led('clear')
          led(x, y, level=MAX_LEVEL, no_decay=False)
          led([[x, y, level], [x, y, level, no_decay], ...])
          led({"x,y": level_or_[level, no_decay], ...})
        `now` must be supplied by the caller (a monotonic ms clock).
        """
        assert now is not None
        if a == "clear":
            for i in range(len(self.levels)):
                self.levels[i] = 0.0
                self.held[i] = 0
                self.lit_since[i] = 0.0
            return

        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            self.set_pixel(int(a), int(b), MAX_LEVEL if c is None else c, now, bool(d))
            return

        if isinstance(a, (list, tuple)):
            for entry in a:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    lvl = entry[2] if len(entry) >= 3 else MAX_LEVEL
                    nd = entry[3] if len(entry) >= 4 else False
                    self.set_pixel(int(entry[0]), int(entry[1]), lvl, now, bool(nd))
            return

        if isinstance(a, dict):
            for key, val in a.items():
                parts = [p.strip() for p in key.split(",")]
                if len(parts) != 2:
                    continue
                x, y = int(parts[0]), int(parts[1])
                if isinstance(val, (list, tuple)):
                    lvl = val[0]
                    nd = val[1] if len(val) > 1 else False
                    self.set_pixel(x, y, lvl, now, bool(nd))
                else:
                    self.set_pixel(x, y, val, now)
            return

    # ---- color() ----
    def get_led_rgb(self, now: float):
        if self.color_duration <= 0:
            return tuple(self.color_to)
        t = ease(min(1.0, max(0.0, (now - self.color_start_at) / self.color_duration)))
        return (
            self.color_from[0] + (self.color_to[0] - self.color_from[0]) * t,
            self.color_from[1] + (self.color_to[1] - self.color_from[1]) * t,
            self.color_from[2] + (self.color_to[2] - self.color_from[2]) * t,
        )

    def color(self, r: float, g: float, b: float, now: float, duration: Optional[float] = None) -> None:
        self.color_from = list(self.get_led_rgb(now))
        self.color_to = [r, g, b]
        self.color_start_at = now
        self.color_duration = COLOR_TRANSITION_MS if duration is None else max(0.0, duration)

# ============================================================================
# AUDIO — loopback capture + FFT band analysis
# ============================================================================

try:
    import soundcard as sc
except Exception:  # pragma: no cover - soundcard needs real audio hardware/daemon
    sc = None

BLOCK_SIZE = 1024          # samples per analysis frame, mirrors analyser.fftSize
SAMPLE_RATE = 44100
SMOOTHING = 0.75           # mirrors analyser.smoothingTimeConstant (exponential smoothing of magnitudes)


@dataclass
class AudioDevice:
    id: str
    name: str
    is_loopback: bool


def list_target_devices() -> list[AudioDevice]:
    """Enumerate candidate 'target sound' devices: loopback (system mix) first, then mics/inputs."""
    devices: list[AudioDevice] = []
    if sc is None:
        return devices
    try:
        default_speaker = sc.default_speaker()
        loop_id = sc.get_microphone(id=default_speaker.name, include_loopback=True).id
        devices.append(AudioDevice(id=loop_id, name=f"System audio ({default_speaker.name})", is_loopback=True))
    except Exception:
        pass
    try:
        for mic in sc.all_microphones(include_loopback=True):
            if any(d.id == mic.id for d in devices):
                continue
            devices.append(AudioDevice(id=mic.id, name=mic.name, is_loopback=getattr(mic, "isloopback", False)))
    except Exception:
        pass
    return devices


class AudioAnalyzer:
    """
    Background thread: captures audio from `device_id` (None = default
    system loopback, "everything" behavior matching the HTML's
    getDisplayMedia(audio:true) capture-whatever-plays default),
    runs an FFT per block, maps magnitudes into BAND_COUNT log-spaced
    bands (0-20kHz), and reports per-band 0..1 levels plus an overall
    silence flag on every frame via `on_frame`.
    """

    def __init__(
        self,
        on_frame: Callable[[list[tuple[int, int, int]]], None],
        on_silence_gap: Callable[[], None],
        gain: float = 16.0,
    ):
        self.on_frame = on_frame
        self.on_silence_gap = on_silence_gap
        self.gain = gain
        self.device_id: Optional[str] = None  # None -> default system loopback
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._smoothed: Optional[np.ndarray] = None
        self._band_edges: Optional[list[int]] = None
        self._silence_since = 0.0
        self._color_switched_for_gap = False
        self.silence_color_switch_enabled = True
        self._lock = threading.Lock()

    def set_gain(self, v: float) -> None:
        with self._lock:
            self.gain = max(0.0, v)

    def set_device(self, device_id: Optional[str]) -> None:
        """Change target device; takes effect on next (re)start."""
        with self._lock:
            self.device_id = device_id
        self.restart()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def _resolve_device(self):
        if sc is None:
            return None
        with self._lock:
            dev_id = self.device_id
        try:
            if dev_id is None:
                # Default target: the whole system output mix (loopback),
                # i.e. "listens to everything" like the browser's
                # getDisplayMedia(audio:true) share-audio default.
                speaker = sc.default_speaker()
                return sc.get_microphone(id=speaker.name, include_loopback=True)
            for mic in sc.all_microphones(include_loopback=True):
                if mic.id == dev_id:
                    return mic
        except Exception:
            return None
        return None

    def _run(self) -> None:
        mic = self._resolve_device()
        if mic is None:
            return

        band_count = BAND_COUNT
        self._smoothed = np.zeros(BLOCK_SIZE // 2 + 1, dtype=np.float64)

        try:
            with mic.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK_SIZE) as rec:
                self._band_edges = build_band_edges(BLOCK_SIZE // 2 + 1, SAMPLE_RATE)
                while not self._stop.is_set():
                    data = rec.record(numframes=BLOCK_SIZE)
                    with self._lock:
                        gain = self.gain
                    mono = data[:, 0] * gain
                    self._process_block(mono)
        except Exception:
            return

    def _process_block(self, mono: np.ndarray) -> None:
        now = time.monotonic() * 1000.0

        window = np.hanning(len(mono))
        spectrum = np.abs(np.fft.rfft(mono * window))
        # Normalize into a 0-255-ish magnitude scale to mirror
        # analyser.getByteFrequencyData()'s 0..255 bytes, then apply the
        # same exponential smoothing the JS AnalyserNode does internally.
        mag = np.clip(spectrum * (255.0 / (len(mono) / 2)), 0, 255)
        if self._smoothed is None or len(self._smoothed) != len(mag):
            self._smoothed = mag.copy()
        else:
            self._smoothed = SMOOTHING * self._smoothed + (1 - SMOOTHING) * mag
        freq_data = self._smoothed

        overall_mag = float(np.mean(freq_data)) / 255.0
        if self.silence_color_switch_enabled:
            if overall_mag < SILENCE_THRESHOLD:
                if self._silence_since == 0:
                    self._silence_since = now
                if not self._color_switched_for_gap and (now - self._silence_since) >= SILENCE_HOLD_MS:
                    self._color_switched_for_gap = True
                    self.on_silence_gap()
            else:
                self._silence_since = 0.0
                self._color_switched_for_gap = False

        edges = self._band_edges
        writes: list[tuple[int, int, int]] = []
        for band in range(BAND_COUNT):
            lo = edges[band]
            hi = max(lo + 1, edges[band + 1])
            hi = min(hi, len(freq_data))
            lo = min(lo, hi - 1) if hi > 0 else lo
            if hi <= lo:
                mag_band = 0.0
            else:
                mag_band = float(np.mean(freq_data[lo:hi])) / 255.0

            norm = (mag_band ** 0.6) * edge_curve(band)
            rows = round(norm * GRID_H)
            col = band * 2
            for row in range(rows):
                y = GRID_H - 1 - row
                level = MAX_LEVEL if row == rows - 1 else max(3, MAX_LEVEL - 1)
                writes.append((col, y, level))

        self.on_frame(writes)

# ============================================================================
# SPOTIFY — PKCE OAuth, polling, cover-art accent extraction
# ============================================================================

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_NOWPLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SPOTIFY_SCOPES = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
# Adaptive poll cadence: background (window unfocused) polls loosely on
# a 3-5s jittered interval; once focused it tightens to 1s; a "hard
# event" (a transport-control click) wakes the loop immediately for a
# near-instant refresh of both playback state and cover art rather than
# waiting out whichever interval is currently in effect.
SPOTIFY_POLL_BACKGROUND_MS = (3000, 5000)
SPOTIFY_POLL_FOCUSED_MS = 1000
SPOTIFY_COLOR_TRANSITION_MS = 900

REDIRECT_PORT = 8945
REDIRECT_URI = "https://visled.athrx.space/w/auth"
# CORS is scoped to exactly this origin (not "*") since the /status
# endpoint below accepts a fetch() from whatever page the browser has
# open — restricting the origin means only auth.html itself, not every
# other tab the user happens to have open, is allowed to talk to it.
REDIRECT_ORIGIN = "https://visled.athrx.space"

def _default_config_dir() -> str:
    """
    Per-OS conventional location for this app's persisted settings and
    Spotify tokens. Windows apps are expected to live under %APPDATA%,
    not a Unix-style ~/.config (which os.path.expanduser("~") would
    still technically resolve to on Windows, just not where a Windows
    user — or an installer/uninstaller — would look for it). macOS/
    Linux keep the original ~/.config/led_panel_widget path.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "led_panel_widget")
    return os.path.join(os.path.expanduser("~"), ".config", "led_panel_widget")


CONFIG_DIR = _default_config_dir()
TOKEN_FILE = os.path.join(CONFIG_DIR, "spotify_tokens.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

FPS_STEPS = (60, 45, 30)
DEFAULT_FPS = 60


def load_app_settings() -> dict:
    """Loads persisted app settings (device, color, always-on-top, fps).
    Missing file / bad JSON / unreadable -> empty dict, so callers just
    fall back to their own defaults via .get()."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_app_settings(settings: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f)
    except Exception:
        pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _random_verifier() -> str:
    return _b64url(secrets.token_bytes(64))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """
    Handles the single redirect hit from Spotify's consent screen.

    Two things make this endpoint safe to leave listening on
    localhost for the duration of the OAuth dance:

    1. It's bound to 127.0.0.1 only (see _run_callback_server) — never
       reachable from anything but this machine, not the whole LAN.
    2. It requires the `state` value that _connect_flow generated and
       sent to Spotify as part of the authorize URL, matched against
       what Spotify hands back on the redirect. Without this, *any*
       page open in the user's browser could fetch()
       http://localhost:8945/status?code=... with an arbitrary code —
       the browser has no same-origin restriction on requests *to*
       localhost, only on reading cross-origin *responses*, so an
       unauthenticated endpoint here is reachable from any open tab,
       not just the real Spotify redirect. `state` is exactly what
       OAuth's spec defines this field for (anti-CSRF), and reusing it
       here as the shared secret needs no extra plumbing beyond what a
       correct PKCE flow already carries.
    """

    server_version = "PanelWidget/1.0"

    def _cors_headers(self):
        # Scoped to the real redirect page's origin rather than "*" —
        # see REDIRECT_ORIGIN above.
        self.send_header("Access-Control-Allow-Origin", REDIRECT_ORIGIN)
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _send_html(self, status: int, body_html: str):
        body = body_html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (matches BaseHTTPRequestHandler naming)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/status":
            self.send_response(404)
            self.end_headers()
            return

        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]
        expected_state = getattr(self.server, "expected_state", None)

        if not state or not expected_state or not secrets.compare_digest(state, expected_state):
            # Wrong/missing state: either this isn't really the
            # Spotify redirect (some other page probing the port), or
            # it's a stale/replayed hit from a previous flow. Reject
            # without touching server.received_code, so a legitimate
            # redirect arriving moments later still gets through.
            self._send_html(
                403,
                "<html><body style='background:#000;color:#dedede;font-family:monospace;"
                "text-align:center;padding-top:20vh'>invalid or expired request.</body></html>",
            )
            return

        self.server.received_code = code  # type: ignore[attr-defined]
        self._send_html(
            200,
            "<html><body style='background:#000;color:#dedede;font-family:monospace;"
            "text-align:center;padding-top:20vh'>connected. you can close this tab.</body></html>",
        )

    def log_message(self, *args):  # silence default stderr logging
        pass


def _run_callback_server(expected_state: str, server_holder: dict, timeout_s: float = 120.0) -> Optional[str]:
    print(f"[Spotify] Listening for auth code on 127.0.0.1:{REDIRECT_PORT}...")
    # 127.0.0.1, not 0.0.0.0: this only ever needs to hear from the
    # browser running on this same machine. Binding every interface
    # made the endpoint reachable from anything else on the LAN too,
    # for no actual benefit.
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.received_code = None  # type: ignore[attr-defined]
    server.expected_state = expected_state  # type: ignore[attr-defined]
    # Published so a newer connect() call can find and close this
    # server if the user retries before this attempt finished — see
    # SpotifyIntegration.connect(). Without this handoff, a second
    # attempt's HTTPServer(...) call on the same REDIRECT_PORT raises
    # "Address already in use" from inside a bare daemon thread (silently
    # killing the second attempt), while the first attempt's server keeps
    # listening with its now-stale `state` value and 403s the real
    # redirect when it finally arrives — which is what auth.html's
    # catch-all error handling was surfacing as "Already connected".
    server_holder["server"] = server

    start_time = time.time()
    while server.received_code is None and (time.time() - start_time) < timeout_s:
        server.timeout = 1.0
        try:
            server.handle_request()
        except (OSError, ValueError):
            # A newer connect() call closed this server out from under
            # us via server_close() — see the docstring above and
            # SpotifyIntegration.connect(). handle_request() doesn't
            # exit cleanly when its socket disappears mid-select(); it
            # raises instead, so this is the normal, expected shape of
            # "a fresher attempt superseded this one," not a real error.
            break

    server_holder.pop("server", None)
    server.server_close()
    return server.received_code  # type: ignore[attr-defined]


@dataclass
class SpotifyState:
    access_token: Optional[str] = None
    expires_at: float = 0.0
    refresh_token: Optional[str] = None
    last_track_id: Optional[str] = None
    is_playing: bool = False
    progress_ms: int = 0
    duration_ms: int = 0
    # monotonic() timestamp of the poll that produced progress_ms above —
    # lets the UI extrapolate "elapsed since last poll" locally for a
    # smooth per-second timestamp without polling Spotify every second.
    progress_captured_at: float = 0.0



class SpotifyIntegration:
    """
    Owns the OAuth dance, token refresh, now-playing polling, and cover
    accent extraction. Calls `on_color(r, g, b, duration_ms)` whenever a
    new track's accent should be applied, and exposes `active` so the
    caller can gate the audio silence-color-switch fallback (a connected
    Spotify session's accent always wins, same priority as the JS).
    """

    def __init__(
        self,
        client_id: str,
        on_color: Callable[[float, float, float, float], None],
        on_track_change: Callable[[str], None],
        on_playing_change: Optional[Callable[[bool], None]] = None,
        on_progress: Optional[Callable[[int, int, float, bool], None]] = None,
    ):
        self.client_id = client_id
        self.on_color = on_color
        self.on_track_change = on_track_change
        self.on_playing_change = on_playing_change
        self.on_progress = on_progress  # (progress_ms, duration_ms, captured_at, is_playing) per poll
        self.state = SpotifyState()
        self.active = False
        self._poll_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._poke_event = threading.Event()  # set to wake the poll loop early
        self._window_focused = True  # PanelWindow pushes real focus state in
        self._verifier: Optional[str] = None
        # Holds {"server": <HTTPServer>} for whichever _run_callback_server
        # call is currently in flight, if any — see connect() and
        # _run_callback_server's server_holder param. A plain dict (not
        # an attribute) so _run_callback_server can pop its own entry
        # from the exact same object connect() is watching, with no
        # ambiguity about which attempt a stale reference belongs to.
        self._connect_lock = threading.Lock()
        self._active_server_holder: Optional[dict] = None
        self._load_tokens()

    # ---- token persistence (replaces localStorage) ----
    def _load_tokens(self) -> None:
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
            self.state.refresh_token = data.get("refresh_token")
        except Exception:
            pass

    def _save_refresh_token(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(TOKEN_FILE, "w") as f:
                json.dump({"refresh_token": self.state.refresh_token}, f)
        except Exception:
            pass

    # ---- OAuth ----
    def connect(self) -> None:
        """Opens the system browser to Spotify's consent screen, then
        waits (on a background thread) for the redirect + code exchange,
        then starts polling.

        If a previous connect() is still waiting on its callback server
        (e.g. the user clicked "connect" again without finishing, or
        after the first attempt seemed to hang), that server is closed
        here first rather than left running: two HTTPServer instances
        can't both bind REDIRECT_PORT, so without this a second attempt
        would raise "Address already in use" inside its own daemon
        thread and die silently, while the first attempt's server kept
        listening with a now-superseded `state` value — and correctly,
        but confusingly, 403s the real redirect when it arrives, which
        is what auth.html was surfacing as a generic "Already connected".
        """
        with self._connect_lock:
            if self._active_server_holder is not None:
                old_server = self._active_server_holder.get("server")
                if old_server is not None:
                    # server_close(), not shutdown(): this server is
                    # driven by a manual handle_request() loop in
                    # _run_callback_server, not serve_forever(), and
                    # shutdown() only ever returns once serve_forever's
                    # own polling loop notices a flag — a loop that
                    # never runs here, so shutdown() would just hang
                    # forever waiting for it. Closing the socket directly
                    # makes the blocked handle_request() call raise
                    # instead, which _run_callback_server now treats as
                    # a normal "superseded by a newer attempt" exit.
                    old_server.server_close()
            self._active_server_holder = {}
            holder = self._active_server_holder
        threading.Thread(target=self._connect_flow, args=(holder,), daemon=True).start()

    def _connect_flow(self, server_holder: dict) -> None:
        self._verifier = _random_verifier()
        challenge = _pkce_challenge(self._verifier)
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": SPOTIFY_SCOPES,
            "state": state,
        }
        url = SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(params)
        webbrowser.open(url)
        code = _run_callback_server(expected_state=state, server_holder=server_holder)
        with self._connect_lock:
            if self._active_server_holder is server_holder:
                self._active_server_holder = None
        if not code:
            return
        if self._exchange_code_for_token(code):
            self.start_polling()

    def _exchange_code_for_token(self, code: str) -> bool:
        if not self._verifier:
            return False
        body = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": self._verifier,
        }
        try:
            res = requests.post(SPOTIFY_TOKEN_URL, data=body, timeout=10)
            if not res.ok:
                return False
            self._apply_token(res.json())
            return True
        except Exception:
            return False

    def _refresh_access_token(self) -> bool:
        if not self.state.refresh_token:
            return False
        body = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.state.refresh_token,
        }
        try:
            res = requests.post(SPOTIFY_TOKEN_URL, data=body, timeout=10)
            if not res.ok:
                return False
            self._apply_token(res.json())
            return True
        except Exception:
            return False

    def _apply_token(self, payload: dict) -> None:
        self.state.access_token = payload.get("access_token")
        self.state.expires_at = time.monotonic() + (payload.get("expires_in", 3600) - 30)
        if payload.get("refresh_token"):
            self.state.refresh_token = payload["refresh_token"]
            self._save_refresh_token()

    def _ensure_fresh_token(self) -> bool:
        if self.state.access_token and time.monotonic() < self.state.expires_at:
            return True
        return self._refresh_access_token()

    # ---- polling / now playing ----
    def start_polling(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def try_resume(self) -> None:
        """On startup, silently resume a session if a refresh token is on disk."""
        if self.state.refresh_token:
            threading.Thread(target=self._resume_flow, daemon=True).start()

    def _resume_flow(self) -> None:
        if self._refresh_access_token():
            self.start_polling()

    def stop(self) -> None:
        self._stop.set()
        self._poke_event.set()  # unblock the wait immediately on shutdown

    def set_window_focused(self, focused: bool) -> None:
        """Called by PanelWindow when the app gains/loses OS focus.
        Only changes which interval the *next* wait uses — doesn't by
        itself trigger an immediate poll (that's poke()'s job)."""
        self._window_focused = focused

    def poke(self) -> None:
        """Wake the poll loop right away for a hard event (a transport
        control click) instead of waiting out the current interval.
        Safe to call from the GUI thread — just sets an Event."""
        self._poke_event.set()

    def _next_wait_seconds(self) -> float:
        if self._window_focused:
            return SPOTIFY_POLL_FOCUSED_MS / 1000.0
        lo, hi = SPOTIFY_POLL_BACKGROUND_MS
        return random.uniform(lo, hi) / 1000.0

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            # Clear *before* polling, not after: a poke() that lands
            # while _poll_now_playing() is still in flight must survive
            # to wake the wait() below. Clearing afterward would erase
            # a poke that arrived mid-poll, effectively swallowing it.
            self._poke_event.clear()
            self._poll_now_playing()
            # Interruptible sleep: returns early the moment poke() sets
            # the event (hard event -> near-instant refresh), otherwise
            # blocks for the full focused/background interval as normal.
            self._poke_event.wait(timeout=self._next_wait_seconds())

    def playback_control(self, action: str) -> None:
        """Action: 'next', 'previous', 'play', 'pause', 'toggle'"""
        print(f"[Spotify] Playback command received: {action}")
        if not self._ensure_fresh_token():
            print("[Spotify] Token refresh failed. Cannot execute playback command.")
            return

        if action == "toggle":
            action = "pause" if self.state.is_playing else "play"
            print(f"[Spotify] Toggling to: {action}")

        endpoints = {
            "next": ("POST", "https://api.spotify.com/v1/me/player/next"),
            "previous": ("POST", "https://api.spotify.com/v1/me/player/previous"),
            "play": ("PUT", "https://api.spotify.com/v1/me/player/play"),
            "pause": ("PUT", "https://api.spotify.com/v1/me/player/pause"),
        }
        if action not in endpoints:
            print(f"[Spotify] Unknown action: {action}")
            return

        method, url = endpoints[action]
        try:
            print(f"[Spotify] Sending {method} to {url}...")
            res = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self.state.access_token}"},
                timeout=10
            )
            if res.ok:
                print("[Spotify] Command successful.")
            else:
                print(f"[Spotify] Command failed: {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[Spotify] Request error: {e}")
        finally:
            # Hard event: wake the poll loop right away so playback
            # state + (if the track changed) cover art refresh almost
            # instantly, rather than waiting out whatever interval was
            # already in flight. Fires even on a failed/erroring
            # command, since the next poll is what tells the UI the
            # command didn't actually take.
            self.poke()

    def _poll_now_playing(self) -> None:
        if not self._ensure_fresh_token():
            self.active = False
            return
        try:
            res = requests.get(
                SPOTIFY_NOWPLAYING_URL,
                headers={"Authorization": f"Bearer {self.state.access_token}"},
                timeout=10,
            )
            if res.status_code == 204:
                # No track is currently playing
                if self.state.last_track_id is not None:
                    self.state.last_track_id = None
                    self.on_track_change("") # Clear console if music stops
                if self.state.is_playing and self.on_playing_change:
                    self.on_playing_change(False)
                self.state.is_playing = False
                self.state.progress_ms = 0
                self.state.duration_ms = 0
                self.active = True
                if self.on_progress:
                    self.on_progress(0, 0, time.monotonic(), False)
                return
            if not res.ok:
                self.active = False
                return
            payload = res.json()
            self.active = True
            new_is_playing = payload.get("is_playing", False)
            if new_is_playing != self.state.is_playing and self.on_playing_change:
                self.on_playing_change(new_is_playing)
            self.state.is_playing = new_is_playing
            item = payload.get("item")
            if not item or not item.get("id"):
                return

            # progress_ms/duration_ms come from this same payload, so
            # they're captured "now" (time.monotonic()) alongside
            # is_playing — the UI extrapolates forward from this pair
            # between polls rather than re-fetching every second.
            self.state.progress_ms = payload.get("progress_ms", 0) or 0
            self.state.duration_ms = item.get("duration_ms", 0) or 0
            self.state.progress_captured_at = time.monotonic()
            if self.on_progress:
                self.on_progress(self.state.progress_ms, self.state.duration_ms, self.state.progress_captured_at, new_is_playing)

            # Print + fetch cover art only on first successful poll or
            # when the track actually changes — this used to fetch the
            # cover on every poll regardless, which is the network hit
            # this refactor is meant to cut out.
            track_changed = self.state.last_track_id is None or item["id"] != self.state.last_track_id
            if track_changed:
                self.state.last_track_id = item["id"]
                track_name = item.get("name", "Unknown Track")
                artist_name = ", ".join([a.get("name", "Unknown Artist") for a in item.get("artists", [])])
                self.on_track_change(f"**Playing: {track_name} - {artist_name}**")

                images = (item.get("album") or {}).get("images") or []
                if images:
                    cover = images[min(1, len(images) - 1)]
                    self._on_new_cover(cover["url"])
        except Exception as e:
            print(f"[Spotify] Poll error: {e}")
            self.active = False

    def _on_new_cover(self, image_url: str) -> None:
        try:
            resp = requests.get(image_url, timeout=10)
            resp.raise_for_status()
        except Exception:
            return
        accent = _extract_accent_color(resp.content)
        if accent:
            self.on_color(accent[0], accent[1], accent[2], SPOTIFY_COLOR_TRANSITION_MS)


def _extract_accent_color(image_bytes: bytes) -> Optional[tuple[int, int, int]]:
    """
    Same bucket/score/floor logic as extractAccentColor() in the HTML:
    downscale, bucket by coarse RGB, score by population x saturation x
    brightness bias, then floor lightness/saturation on the winner.
    """
    if Image is None:
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception:
        return None

    sample = 48
    img = img.resize((sample, sample))
    pixels = img.getdata()

    buckets: dict[int, list] = {}
    for r, g, b, a in pixels:
        if a < 128:
            continue
        h, s, l = rgb_to_hsl(r, g, b)
        if l < 0.06 or l > 0.94:
            continue
        key = ((r >> 3 & 0x1F) * 24) + ((g >> 3 & 0x1F) * 24) + (b >> 3 & 0x1F)
        entry = buckets.setdefault(key, [0, 0, 0, 0])
        entry[0] += r
        entry[1] += g
        entry[2] += b
        entry[3] += 1

    best = None
    best_score = -1.0
    best_hsl = None
    for r_sum, g_sum, b_sum, n in buckets.values():
        avg_r, avg_g, avg_b = r_sum / n, g_sum / n, b_sum / n
        h, s, l = rgb_to_hsl(avg_r, avg_g, avg_b)
        brightness_bias = 0.3 + l
        score = math.log(1 + n) * (0.25 + s) * brightness_bias
        if score > best_score:
            best_score = score
            best = (round(avg_r), round(avg_g), round(avg_b))
            best_hsl = (h, s, l)

    if best is None:
        return None

    h, s, l = best_hsl
    if l < ACCENT_MIN_LIGHTNESS or s < ACCENT_MIN_SATURATION:
        return hsl_to_rgb(h, max(s, ACCENT_MIN_SATURATION), max(l, ACCENT_MIN_LIGHTNESS))
    return best

# ============================================================================
# RENDER WIDGET — squircle + glow painting
# ============================================================================

SQUIRCLE_N = 4  # squircle exponent, matches JS `n`
SQUIRCLE_STEPS = 20  # matches JS `steps`


def _squircle_path(cx: float, cy: float, r: float, n: float) -> QPainterPath:
    path = QPainterPath()
    for i in range(SQUIRCLE_STEPS + 1):
        t = (i / SQUIRCLE_STEPS) * math.pi * 2
        ct, st = math.cos(t), math.sin(t)
        x = cx + math.copysign(abs(ct) ** (2 / n), ct) * r
        y = cy + math.copysign(abs(st) ** (2 / n), st) * r
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


CONTROL_ACCENT = QColor(222, 222, 222)  # same white-ish accent used for the console cursor / settings dialog


class _IconButton(QPushButton):
    """
    A transparent, borderless button that paints its own vector glyph
    (prev / play / pause / next) instead of using text or a font icon.
    Matches the panel's neon-on-black look and needs no icon assets or
    font glyphs that might render differently across systems.

    Hover state is a plain circle, sitting tight inside the pill, so the
    transport controls read as compact and precise. It fades in and out
    (rather than snapping) via an animated hoverAmount, while a press
    always jumps straight to full so clicks stay responsive. The hover
    glow/ring optionally tints toward the panel's current live LED color
    (led_color_fn) so the controls feel colored by the same source as
    the grid, rather than fixed white regardless of what's playing.
    """

    SIZE = 42
    FADE_MS = 150  # duration for the hover glyph/glow to fade in and out

    def __init__(self, kind: str, parent=None, led_color_fn: Optional[Callable[[], tuple]] = None):
        super().__init__(parent)
        self.kind = kind  # "prev" | "play" | "next"
        self._playing = False  # only meaningful for kind == "play"
        self._hover_amount = 0.0  # 0.0 = idle, 1.0 = fully hovered; animated between
        self._led_color_fn = led_color_fn
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

        self._hover_anim = QPropertyAnimation(self, b"hoverAmount", self)
        self._hover_anim.setDuration(self.FADE_MS)
        self._hover_anim.setEasingCurve(QEasingCurve.OutCubic)

    def set_playing(self, playing: bool) -> None:
        if self.kind != "play" or playing == self._playing:
            return
        self._playing = playing
        self.update()

    def _animate_hover(self, target: float) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_amount)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def enterEvent(self, event):
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _get_hover_amount(self) -> float:
        return self._hover_amount

    def _set_hover_amount(self, value: float) -> None:
        self._hover_amount = value
        self.update()

    hoverAmount = Property(float, _get_hover_amount, _set_hover_amount)

    def _accent_rgb(self) -> tuple:
        # Falls back to the original neutral white-ish accent whenever no
        # live color source is wired up, or the panel hasn't rendered a
        # color yet — never leaves the glow uncolored/black.
        if self._led_color_fn is None:
            return (222, 222, 222)
        try:
            r, g, b = self._led_color_fn()
            return (int(r), int(g), int(b))
        except Exception:
            return (222, 222, 222)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        cx, cy = self.SIZE / 2, self.SIZE / 2
        # isDown() snaps straight to full so a click/press always feels
        # instant, never waiting on the fade even mid-transition.
        amount = 1.0 if self.isDown() else self._hover_amount
        ar, ag, ab = self._accent_rgb()

        if amount > 0.0:
            circle_r = self.SIZE / 2 - 2
            glow = QRadialGradient(QPointF(cx, cy), circle_r * 1.4)
            glow.setColorAt(0.0, QColor(ar, ag, ab, round((70 if self.isDown() else 50) * amount)))
            glow.setColorAt(1.0, QColor(ar, ag, ab, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(cx, cy), circle_r * 1.4, circle_r * 1.4)

            painter.setPen(QPen(QColor(ar, ag, ab, round(150 * amount)), 1))
            painter.setBrush(QColor(ar, ag, ab, round(22 * amount)))
            painter.drawEllipse(QPointF(cx, cy), circle_r, circle_r)

        # Glyph color eases between idle and active tones with the same
        # hover amount, rather than snapping, so the whole glyph fades
        # together with the ring/glow around it.
        idle = (222, 222, 222, 200)
        hot = (255, 255, 255, 235)
        glyph = QColor(
            round(idle[0] + (hot[0] - idle[0]) * amount),
            round(idle[1] + (hot[1] - idle[1]) * amount),
            round(idle[2] + (hot[2] - idle[2]) * amount),
            round(idle[3] + (hot[3] - idle[3]) * amount),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(glyph)

        if self.kind == "play":
            if self._playing:
                painter.drawRoundedRect(QRectF(15.5, 15, 4, 12), 1.5, 1.5)
                painter.drawRoundedRect(QRectF(22.5, 15, 4, 12), 1.5, 1.5)
            else:
                tri = QPainterPath()
                tri.moveTo(15.5, 14)
                tri.lineTo(15.5, 28)
                tri.lineTo(26.5, 21)
                tri.closeSubpath()
                painter.drawPath(tri)
        elif self.kind == "prev":
            painter.drawRoundedRect(QRectF(14.5, 15, 2.5, 12), 1, 1)
            tri = QPainterPath()
            tri.moveTo(27.5, 15)
            tri.lineTo(27.5, 27)
            tri.lineTo(18.5, 21)
            tri.closeSubpath()
            painter.drawPath(tri)
        else:  # "next"
            tri = QPainterPath()
            tri.moveTo(14.5, 15)
            tri.lineTo(14.5, 27)
            tri.lineTo(23.5, 21)
            tri.closeSubpath()
            painter.drawPath(tri)
            painter.drawRoundedRect(QRectF(25, 15, 2.5, 12), 1, 1)

        painter.end()


class TimestampLabel(QLabel):
    """
    "mm:ss / mm:ss" progress readout, painted in the panel's current
    live LED accent color (same source _IconButton's hover glow uses)
    so it reads as part of the same instrument rather than a bolted-on
    label. Occupies the exact same footprint as SpotifyControls's pill
    (see the stacking in PanelWindow.__init__) and is meant to be shown
    opposite that pill's own hover fade: visible while the transport
    controls are hidden, covered by them once the pill fades in on
    hover — so the two never compete for the same few square pixels.

    Ticks its own display once a second, extrapolating forward from
    the last poll's (progress_ms, duration_ms, captured_at) via
    wall-clock elapsed time rather than re-polling Spotify every
    second — polling stays on its existing 1-5s adaptive cadence.

    The accent color runs on its own independent 100ms timer instead
    of riding the once-a-second time tick, so a crossfade reads as a
    smooth transition rather than snapping into place only once a
    second (see _sample_color).
    """

    TICK_MS = 1000

    def __init__(self, parent=None, led_color_fn: Optional[Callable[[], tuple]] = None):
        super().__init__(parent)
        self._led_color_fn = led_color_fn
        self._progress_ms = 0
        self._duration_ms = 0
        self._captured_at = 0.0
        self._is_playing = False

        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setText("")
        # Static styling only — color is deliberately left out of this
        # stylesheet and driven via setPalette() instead (see
        # _paint_color). setStyleSheet() forces a full CSS reparse and
        # style re-polish every time it's called; doing that 10x/sec on
        # the color timer was expensive enough to visibly stall the GUI
        # thread, which made the once-a-second time tick lag and skip.
        # A QPalette color change is just a property write plus a
        # repaint — cheap enough to do at that rate.
        self.setStyleSheet(
            f"QLabel {{ font-family: {CONSOLE_FONT_STACK}; font-size: 12px; "
            "letter-spacing: 0.5px; background: transparent; }}"
        )

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)  # starts visible: pill starts hidden too
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_anim.setDuration(STACK_FADE_MS)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(self.TICK_MS)

        # Color sampling runs on its own fast, independent timer rather
        # than riding the once-a-second time tick above, so the accent
        # color updates smoothly instead of snapping once a second.
        self._last_applied_rgb = None
        self._color_timer = QTimer(self)
        self._color_timer.timeout.connect(self._sample_color)
        self._color_timer.start(TIMESTAMP_COLOR_TICK_MS)

        self._sample_color()

    def _accent_rgb(self) -> tuple:
        if self._led_color_fn is None:
            return (222, 222, 222)
        try:
            r, g, b = self._led_color_fn()
            return (int(r), int(g), int(b))
        except Exception:
            return (222, 222, 222)

    def _paint_color(self, rgb: tuple) -> None:
        r, g, b = rgb
        pal = self.palette()
        pal.setColor(QPalette.WindowText, QColor(r, g, b, 220))  # QLabel's default foreground role
        self.setPalette(pal)

    def set_progress(self, progress_ms: int, duration_ms: int, captured_at: float, is_playing: bool) -> None:
        self._progress_ms = max(0, progress_ms)
        self._duration_ms = max(0, duration_ms)
        self._captured_at = captured_at
        self._is_playing = is_playing
        self._render()
        # Re-phase the once-a-second ticker to start counting from this
        # fresh poll rather than whatever arbitrary schedule it was
        # already on. Without this, the immediate _render() above lands
        # off-cycle from the next scheduled _on_tick fire, so ticks after
        # a poll arrive at whatever leftover interval the old schedule
        # happened to have — e.g. 350ms, then 1000ms after that — instead
        # of an even 1000ms apart. Restarting the timer here makes every
        # tick exactly TICK_MS after the last known-good timestamp.
        self._timer.start(self.TICK_MS)

    @staticmethod
    def _fmt(ms: int) -> str:
        total_s = max(0, int(ms // 1000))
        return f"{total_s // 60}:{total_s % 60:02d}"

    def _on_tick(self) -> None:
        self._render()

    def _render(self) -> None:
        if self._duration_ms <= 0:
            self.setText("")
            return
        shown_progress = self._progress_ms
        if self._is_playing and self._captured_at:
            elapsed = (time.monotonic() - self._captured_at) * 1000.0
            shown_progress = min(self._duration_ms, self._progress_ms + max(0.0, elapsed))
        self.setText(f"{self._fmt(shown_progress)} / {self._fmt(self._duration_ms)}")

    def _sample_color(self) -> None:
        # Called every 100ms by self._color_timer, independent of the
        # once-a-second time tick. Only repaints when the accent color
        # actually moved, so a settled color just gets a no-op check
        # 10x/sec instead of a real repaint.
        rgb = self._accent_rgb()
        if rgb == self._last_applied_rgb:
            return
        self._last_applied_rgb = rgb
        self._paint_color(rgb)

    def fade_in(self):
        # Sample right away (rather than waiting for the next 100ms
        # _color_timer tick) so becoming visible mid-crossfade picks up
        # the live color immediately, and resume the 100ms color timer
        # that fade_out() paused while hidden.
        self._sample_color()
        if not self._color_timer.isActive():
            self._color_timer.start(TIMESTAMP_COLOR_TICK_MS)
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def fade_out(self):
        # Not visible once this completes — no point repainting its
        # color at 10fps while it's covered by the transport pill.
        self._color_timer.stop()
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()


class SpotifyControls(QWidget):
    """
    Prev / Play-Pause / Next transport controls, grouped in a dark pill
    that echoes the settings dialog's panel style. Fades in on hover of
    the bottom bar; the play glyph reflects Spotify's actual playback
    state rather than a fixed label.
    """

    # Equal gap on all four sides between the pill's edge and each
    # button's edge (also referenced by BOTTOM_BAR_H's sizing further
    # down, so the bottom bar always has room for the full pill height).
    GAP = 3

    def __init__(self, parent, on_action: Callable[[str], None], led_color_fn: Optional[Callable[[], tuple]] = None):
        super().__init__(parent)
        self.on_action = on_action
        self.setFixedSize(144, _IconButton.SIZE + self.GAP * 2)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(self.GAP, self.GAP, self.GAP, self.GAP)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignCenter)

        self.prev_btn = _IconButton("prev", led_color_fn=led_color_fn)
        self.prev_btn.clicked.connect(lambda: self.on_action("previous"))

        self.play_btn = _IconButton("play", led_color_fn=led_color_fn)
        self.play_btn.clicked.connect(self._on_play_clicked)

        self.next_btn = _IconButton("next", led_color_fn=led_color_fn)
        self.next_btn.clicked.connect(lambda: self.on_action("next"))

        layout.addWidget(self.prev_btn)
        layout.addWidget(self.play_btn)
        layout.addWidget(self.next_btn)

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)

        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_anim.setDuration(STACK_FADE_MS)

    def _on_play_clicked(self):
        # Flip the icon immediately so the click feels responsive; the
        # next poll (or the on_playing_change callback) corrects it if
        # the actual toggle command didn't go through.
        self.play_btn.set_playing(not self.play_btn._playing)
        self.on_action("toggle")

    def set_playing(self, playing: bool) -> None:
        self.play_btn.set_playing(playing)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.width(), self.height()), self.height() / 2, self.height() / 2)
        painter.setPen(QPen(QColor(222, 222, 222, 60), 1))
        painter.setBrush(QColor(10, 10, 10, 140))
        painter.drawPath(path)
        painter.end()

    def fade_in(self):
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(0.9)
        self._fade_anim.start()

    def fade_out(self):
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

class PanelRenderWidget(QWidget):
    """
    Renders the LED grid at a fixed logical resolution (CANVAS_W x
    CANVAS_H) scaled to fill this widget's current size, preserving
    aspect ratio via a centered viewport + uniform scale (equivalent to
    the JS's integer-cell-size fitCanvas(), but smoothly scaled since Qt
    handles the crispness via device-pixel-ratio-aware painting).
    """

    FRAME_MS = 16  # ~60fps, matches requestAnimationFrame cadence closely enough

    def __init__(self, panel: LedPanel, parent=None, fps: int = DEFAULT_FPS):
        super().__init__(parent)
        self.panel = panel
        self._flicker_t = 0.0
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._fps = fps
        self._timer.start(round(1000.0 / fps))
        # Low Performance Mode: swaps the glow pass's per-cell
        # QRadialGradient (allocated + evaluated fresh every frame,
        # per lit cell) for a flat semi-transparent circle of the same
        # size/color. Same halo, no gradient math — this is the paint
        # side's share of LPM; LedPanel.low_perf_mode is the other half.
        self.low_perf_mode = False

    def set_fps(self, fps: int) -> None:
        """Retargets the render tick rate live — used by the settings
        dialog's 60/45/30 toggle. Restarting the QTimer with a new
        interval takes effect on the next tick; nothing else about the
        engine's own timing (which runs off wall-clock `now`, not frame
        count) needs to change."""
        if fps == self._fps:
            return
        self._fps = fps
        self._timer.start(round(1000.0 / fps))

    def _tick(self):
        now = time.monotonic() * 1000.0
        self.panel.recompute(now)
        self.update()

    def _flicker_mul(self, now: float) -> float:
        t = now * 0.0021
        return 0.965 + math.sin(t) * 0.02 + math.sin(t * 2.7) * 0.012

    def paintEvent(self, event):  # noqa: N802
        now = time.monotonic() * 1000.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        scale = min(w / CANVAS_W, h / CANVAS_H)
        off_x = (w - CANVAS_W * scale) / 2
        off_y = (h - CANVAS_H * scale) / 2

        painter.translate(off_x, off_y)
        painter.scale(scale, scale)

        lr, lg, lb = self.panel.get_led_rgb(now)
        shimmer = self._flicker_mul(now)

        pad = CELL * 0.12
        size = CELL - pad * 2
        r = size / 2

        field = self.panel.field

        # Glow pass first (soft, low alpha, radius scales with brightness).
        # GLOW_BOOST widens + intensifies the halo at high brightness only
        # (b**3 weighting keeps low/mid-brightness cells' glow unchanged
        # and only fattens the halo as a cell approaches full/"active").
        GLOW_BOOST = 0.12
        lpm = self.low_perf_mode
        for y in range(GRID_H):
            for x in range(GRID_W):
                b = field[self.panel.idx(x, y)] * shimmer
                if b <= 0.0001:
                    continue
                cx = PAD + x * CELL + CELL / 2
                cy = PAD + y * CELL + CELL / 2
                boost = GLOW_BOOST * (b ** 3)
                glow_r = r * (1.8 + b * 1.4) * (1.0 + boost)
                glow_alpha = min(1.0, 0.55 * b * (1.0 + boost))

                painter.setPen(Qt.NoPen)
                if lpm:
                    # LPM: flat semi-transparent circles instead of a
                    # QRadialGradient — skips the gradient's per-pixel
                    # interpolation, which is the expensive part at this
                    # cell count, not the number of draw calls. A single
                    # flat circle read as a hard-edged disc rather than a
                    # glow, especially where many dim/trailing cells'
                    # circles overlapped into a flat smear. Two flat
                    # circles (a dim wide one, a brighter tight one) is
                    # still just plain scanline fills — no gradient math —
                    # but roughly steps down the smooth version's falloff
                    # instead of cutting straight from peak alpha to zero.
                    outer_alpha = glow_alpha * 0.22
                    inner_alpha = glow_alpha * 0.55
                    painter.setBrush(QColor(int(lr), int(lg), int(lb), int(255 * outer_alpha)))
                    painter.drawEllipse(QPointF(cx, cy), glow_r, glow_r)
                    painter.setBrush(QColor(int(lr), int(lg), int(lb), int(255 * inner_alpha)))
                    painter.drawEllipse(QPointF(cx, cy), glow_r * 0.5, glow_r * 0.5)
                else:
                    grad = QRadialGradient(QPointF(cx, cy), glow_r)
                    grad.setColorAt(0.0, QColor(int(lr), int(lg), int(lb), int(255 * glow_alpha)))
                    grad.setColorAt(1.0, QColor(int(lr), int(lg), int(lb), 0))
                    painter.setBrush(grad)
                    painter.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # Crisp squircle body pass on top.
        # CORE_BOOST nudges the top of the brightness range a bit
        # brighter (mix can edge past 1.0, clamped below) — same b**3
        # weighting as the glow so it's only noticeable on the
        # brightest/"active" cells, not a global brightness change.
        CORE_BOOST = 0.08
        for y in range(GRID_H):
            for x in range(GRID_W):
                b = field[self.panel.idx(x, y)] * shimmer
                if b <= 0.0001:
                    continue
                cx = PAD + x * CELL + CELL / 2
                cy = PAD + y * CELL + CELL / 2

                core_alpha = min(1.0, 0.15 + b * 0.95)
                mix = min(1.0 + CORE_BOOST, (0.42 + 0.58 * b) * (1.0 + CORE_BOOST * (b ** 3)))
                rr = min(255, round(lr * mix))
                gg = min(255, round(lg * mix))
                bb = min(255, round(lb * mix))

                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(int(rr), int(gg), int(bb), int(255 * core_alpha)))
                path = _squircle_path(cx, cy, r * (0.72 + 0.28 * b), SQUIRCLE_N)
                painter.drawPath(path)

        painter.end()

# ============================================================================
# CONSOLE WIDGET — typewriter print() label
# ============================================================================

ACCENT_COLOR = "rgba(255, 255, 255, 0.92)"
BASE_COLOR = "rgba(222, 222, 222, 0.88)"
# Swap the lead name to try a different console typeface — all fall back
# gracefully if a given font isn't installed. "Berkeley Mono" and "IBM
# Plex Mono" both read well against small LED-style text; kept JetBrains
# Mono as one of the fallbacks since it was the original.
CONSOLE_FONT_STACK = (
    '"IBM Plex Mono", "Berkeley Mono", "JetBrains Mono", "SF Mono", '
    "Menlo, Consolas, monospace"
)


class ConsoleLabel(QLabel):
    """
    Bottom-anchored typewriter label. The label is taller than one text
    line (it can hold LINE_BUFFER wrapped lines) and sits bottom-aligned
    in the bar, so short text hugs the bottom edge. As the typewriter
    print grows past the current line count (or a resize rewraps the
    text into more/fewer lines), the visible content slides up/down by
    a line-height instead of jumping, by animating a vertical pixel
    offset applied on top of the layout's own bottom-aligned position.
    """

    LINE_BUFFER = 2  # max simultaneous wrapped lines the label reserves room for

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)


        self.setStyleSheet(
            f'QLabel {{ color: {BASE_COLOR}; font-family: {CONSOLE_FONT_STACK}; '
            'font-size: 15px; letter-spacing: 0.3px; '
            "background: transparent; }}"
        )
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._segments = []
        self._total_len = 0
        self._shown = 0
        self._token = 0

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)
        self._pending_action = None  # "start" | "tick"

        self._cursor_visible = True
        self._cursor_blink = QTimer(self)
        self._cursor_blink.timeout.connect(self._toggle_cursor)
        self._cursor_blink.start(500)

        # Reserve LINE_BUFFER lines of height plus move-up room so wrapped text has
        # somewhere to slide within, and fix the label to its parent's full width so
        # word-wrap boundaries (and therefore line counts) are stable.
        #
        # BOTTOM_SAFETY_PX is added in here too, as real extra height, rather
        # than living only inside the bottom content margin (_set_slide_offset
        # below). At the fully-scrolled LINE_BUFFER-line state the margin
        # there is round(slide) + BOTTOM_SAFETY_PX == TEXT_SLIDE_PX +
        # BOTTOM_SAFETY_PX — without this term matching that, the safety pad
        # had nowhere to come from except the LINE_BUFFER-line budget itself,
        # leaving the box exactly BOTTOM_SAFETY_PX short of what LINE_BUFFER
        # real lines need and clipping the last line's tail (e.g. a wrapped
        # 2nd line losing its bottom half once text reached 2/2 lines).
        fm = self.fontMetrics()
        self._line_h = fm.lineSpacing()
        self.setFixedHeight(self._line_h * self.LINE_BUFFER + TEXT_SLIDE_PX + self.BOTTOM_SAFETY_PX)


        self._line_count = 1
        self._slide_offset = 0.0  # current animated y-offset, in px (0 = resting/bottom)
        self._slide_anim = QPropertyAnimation(self, b"slideOffset", self)
        self._slide_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._slide_anim.setDuration(220)

        # See resizeEvent for why this mask exists — clips any text that
        # overflows the label's own box (e.g. a 3+ line statement being
        # scrolled through a 2-line window) instead of letting it paint
        # straight through the top edge and past the panel.
        self.setMask(QRegion(0, 0, max(1, self.width()), self.height()))

    # -- animated vertical offset ---------------------------------------
    # The label is fixed at LINE_BUFFER lines tall and bottom-aligns its
    # text via setAlignment(AlignBottom). Sliding the visible text up by
    # one line is done by growing the label's own top content margin —
    # that pushes QLabel's normal (correctly-wrapping, correctly-painting)
    # text layout upward within its box, with no custom painting needed.
    def _get_slide_offset(self) -> float:
        return self._slide_offset

    # Bottom clearance under the wrapped text so descenders (g/p/y/q on
    # the last visible line) never sit flush against the box edge. This
    # rides in the bottom content margin (see _set_slide_offset below),
    # which is what actually reserves clearance under AlignVCenter text.
    # Was 1px, which measured out to not be enough room for real
    # descenders (see the cut-off tails on "Oura, pipenpodol"'s "p"s);
    # 4px gives real breathing room.
    #
    # This same constant is folded into __init__'s setFixedHeight too, so
    # it's genuine headroom on top of the LINE_BUFFER-line budget rather
    # than carved out of it (see the comment there for why that distinction
    # matters). Bumping this now grows both the box height here and the
    # matching term in _console_need below by the same amount, so the two
    # stay in lockstep instead of the static estimate quietly drifting
    # out of sync with what the live widget actually needs.
    BOTTOM_SAFETY_PX = 4

    def _set_slide_offset(self, value: float) -> None:
        self._slide_offset = value
        # value is >= 0 (0 = resting at center, positive = shifted
        # up); the bottom margin grows as the offset becomes more positive
        # so AlignVCenter text is pushed upward by that many pixels.
        # The constant safety pad rides along underneath regardless of
        # slide state, so it's present at rest too, not just mid-slide.
        bottom_margin = round(value) + self.BOTTOM_SAFETY_PX
        m = self.contentsMargins()
        self.setContentsMargins(m.left(), m.top(), m.right(), bottom_margin)


    slideOffset = Property(float, _get_slide_offset, _set_slide_offset)

    def _current_line_count(self, plain_text: Optional[str] = None) -> int:
        """How many wrapped lines the given plain text needs at this
        label's current width. Defaults to the *fully revealed* current
        statement (used on resize); the typewriter passes in just the
        characters shown so far, so the slide tracks the print live and
        happens right before a new line is actually needed.

        Uncapped: a statement can wrap to more than LINE_BUFFER lines
        (e.g. a long "Playing: ..." line at a narrow width), and the
        slide offset below grows to match so every line still passes
        through the visible window in turn, rather than being silently
        clamped and left to overflow past the label's own box.

        Measured via QTextDocument (the same rich-text layout engine
        QLabel itself uses to paint) rather than plain QFontMetrics
        boundingRect — the label renders Qt.RichText markup (accent
        spans, the blinking cursor span), and plain-text metrics don't
        reproduce that layout, which was letting the computed line
        count silently disagree with what actually got painted and
        the slide never (or wrongly) fired as a result."""
        if plain_text is None:
            plain_text = "".join(seg.text for seg in self._segments)
        if not plain_text:
            return 1
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setDocumentMargin(0)
        doc.setTextWidth(max(1, self.width()))
        doc.setPlainText(plain_text)
        return max(1, round(doc.size().height() / self._line_h))

    def _sync_slide_to_line_count(self, plain_text: Optional[str] = None, animate: bool = True) -> None:
        target_lines = self._current_line_count(plain_text)
        if target_lines == self._line_count and not animate:
            return
        self._line_count = target_lines
        target_offset = (target_lines - 1) * TEXT_SLIDE_PX
        self._slide_anim.stop()
        if animate:
            self._slide_anim.setStartValue(self._slide_offset)
            self._slide_anim.setEndValue(target_offset)
            self._slide_anim.start()
        else:
            self._set_slide_offset(target_offset)


    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        # Word-wrap boundaries depend on width, so a resize can change how
        # many lines the current text needs — re-anchor without animating
        # the initial jump on first layout, but slide smoothly thereafter.
        #
        # Must measure against whatever's actually been typed so far
        # (_shown_plain_text), never the full final statement — a resize
        # firing mid-typewriter-reveal (e.g. the transport pill's own
        # fade in/out nudging the bar's layout) used to jump straight to
        # the FINAL line count here, then the very next typewriter tick
        # would recompute from the partial text and snap back down,
        # producing a visible up/down flicker every time a resize landed
        # mid-reveal. Using the same partial-text source as the
        # typewriter keeps this call and _advance's own call always in
        # agreement about "what's on screen right now".

        # Failsafe: _truncate_segments only ran once, against the width
        # at print time. A resize that narrows the label enough to push
        # the *current* statement past LINE_BUFFER lines used to just
        # slide/mask that overflow out of view with no visual sign it
        # was cut — now it gets truncated to an ellipsis instead, same
        # as the print-time path, so a 3rd line never silently appears
        # (masked or otherwise).
        full_text = "".join(seg.text for seg in self._segments)
        if self._current_line_count(full_text) > self.LINE_BUFFER:
            self._truncate_segments(limit=self.LINE_BUFFER)
            self._total_len = sum(len(s.text) for s in self._segments)
            self._shown = min(self._shown, self._total_len)
            self.setText(self._segments_to_html(self._shown))

        self._sync_slide_to_line_count(
            self._shown_plain_text(), animate=event.oldSize().width() > 0
        )
        # setMask clips this widget's rendered output (including rich
        # text QLabel paints via internal margins, which does NOT clip
        # to rect() on its own) to exactly its own box. Without this, a
        # statement wrapped to more lines than LINE_BUFFER pushes its
        # earliest line(s) above y=0 via the slide offset's top margin,
        # and that overflow paints straight through the label's top edge
        # and out past the bottom bar / panel edge instead of being
        # hidden — older lines need to scroll up and out cleanly, like a
        # real console/ticker, not spill past the panel.
        self.setMask(QRegion(self.rect()))

    def _segments_to_html(self, char_limit: int) -> str:
        out = []
        remaining = char_limit
        for seg in self._segments:
            if remaining <= 0:
                break
            chunk = seg.text[:remaining]
            remaining -= len(chunk)
            escaped = chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if seg.accent:
                out.append(f'<span style="color:{ACCENT_COLOR}">{escaped}</span>')
            else:
                out.append(escaped)
        return "".join(out)

    def _shown_plain_text(self) -> str:
        """Plain (unescaped, no markup) text revealed so far — used to
        measure wrapped line count as the typewriter print progresses."""
        out = []
        remaining = self._shown
        for seg in self._segments:
            if remaining <= 0:
                break
            chunk = seg.text[:remaining]
            remaining -= len(chunk)
            out.append(chunk)
        return "".join(out)

    def _truncate_segments(self, limit: int = LINE_BUFFER) -> None:
        """Truncate segments to fit within the line limit, adding an ellipsis."""
        full_text = "".join(seg.text for seg in self._segments)
        if self._current_line_count(full_text) <= limit:
            return

        # Binary search for the largest character index that fits within the limit
        low, high = 0, len(full_text)
        best_cutoff = 0
        while low <= high:
            mid = (low + high) // 2
            if self._current_line_count(full_text[:mid]) <= limit:
                best_cutoff = mid
                low = mid + 1
            else:
                high = mid - 1

        # Reconstruct segments up to the cutoff
        new_segments = []
        chars_collected = 0
        for seg in self._segments:
            if chars_collected >= best_cutoff:
                break
            remaining = best_cutoff - chars_collected
            chunk_text = seg.text[:remaining]
            if chunk_text:
                new_segments.append(TextSegment(chunk_text, seg.accent))
            chars_collected += len(seg.text)

        # Ensure that adding "..." doesn't push the line count over the limit
        # If it does, we need to remove characters from the last segment.
        while True:
            test_text = "".join(s.text for s in new_segments) + "..."
            if self._current_line_count(test_text) <= limit or not new_segments:
                break

            # Shorten the last segment
            last_seg = new_segments[-1]
            if len(last_seg.text) > 1:
                new_segments[-1] = TextSegment(last_seg.text[:-1], last_seg.accent)
            else:
                new_segments.pop()

        new_segments.append(TextSegment("...", False))
        self._segments = new_segments

    def print(self, statement: str) -> None:
        self._token += 1
        my_token = self._token
        self._timer.stop()
        self._fade_anim.stop()

        # If there's currently visible text, fade it out first and only
        # swap in the new statement once that fade-out has finished — a
        # true "old line fades away, then new line appears" sequence,
        # rather than swapping the text immediately and merely fading
        # in the *new* content over the old one's vanishing act.
        has_visible_text = bool(self._segments) and self._opacity_effect.opacity() > 0.0
        if has_visible_text:
            self._fade_to(0.0, 150)
            self._fade_anim.finished.connect(lambda: self._begin_statement(statement, my_token))
        else:
            self._begin_statement(statement, my_token)

    def _begin_statement(self, statement: str, my_token: int) -> None:
        if my_token != self._token:
            return  # superseded by a newer print() call while we were fading out
        try:
            self._fade_anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass

        self._segments = parse_segments(statement)
        self._truncate_segments(limit=self.LINE_BUFFER)
        self._total_len = sum(len(s.text) for s in self._segments)
        self._shown = 0

        # New statement starts fresh at the bottom line, no slide carried
        # over from whatever the previous statement had wrapped to.
        self._line_count = 1
        self._slide_anim.stop()
        self._set_slide_offset(0.0)

        self.setText("")
        self._set_opacity(0.0)

        delay = 0 if statement == "" else 120
        self._pending_action = "start"
        self._timer.start(delay)

    def _set_opacity(self, value: float) -> None:
        self._opacity_effect.setOpacity(value)

    def _fade_to(self, target: float, duration: int) -> None:
        self._fade_anim.stop()
        try:
            self._fade_anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass  # nothing was connected
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(target)
        self._fade_anim.setDuration(duration)
        self._fade_anim.start()

    def _on_timer(self) -> None:
        my_token = self._token
        if self._pending_action == "start":
            self._fade_to(1.0, 500)
            if self._total_len == 0:
                self.setText(self._segments_to_html(0))
                self._pending_action = None
                return
            self._pending_action = "tick"
            self._advance(my_token)
        elif self._pending_action == "tick":
            self._advance(my_token)

    def _advance(self, my_token: int) -> None:
        if my_token != self._token:
            return
        self._shown += 1
        done = self._shown >= self._total_len
        html = self._segments_to_html(self._shown)
        if not done and self._cursor_visible:
            html += '<span style="color:rgba(222,222,222,0.75)">_</span>'
        self.setText(html)

        # Check the line count for the text revealed *so far* — this is
        # what makes the slide happen right before a new line is needed,
        # rather than jumping once the whole final line count is known.
        self._sync_slide_to_line_count(self._shown_plain_text())

        if not done:
            per_char = max(20, min(40, 900 / max(1, self._total_len)))
            self._pending_action = "tick"
            self._timer.start(int(per_char))
        else:
            self._pending_action = None

    def _toggle_cursor(self):
        self._cursor_visible = not self._cursor_visible

# ============================================================================
# SETTINGS DIALOG — Ctrl+E panel
# ============================================================================

ACCENT = "#DEDEDE"

DIALOG_STYLE = f"""
QDialog {{
    background-color: #0a0a0a;
    border: 1px solid rgba(222,222,222,0.35);
    border-radius: 14px;
}}
QLabel {{
    color: rgba(222,222,222,0.9);
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
}}
QLabel#heading {{
    font-size: 15px;
    font-weight: 600;
    color: {ACCENT};
}}
QPushButton {{
    background: transparent;
    border: 1px solid rgba(222,222,222,0.4);
    color: rgba(222,222,222,0.85);
    border-radius: 999px;
    padding: 8px 18px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
}}
QPushButton:hover {{
    border-color: rgba(222,222,222,0.8);
    background: rgba(222,222,222,0.08);
}}
QComboBox {{
    background: #111;
    border: 1px solid rgba(222,222,222,0.3);
    border-radius: 8px;
    padding: 6px 10px;
    color: rgba(222,222,222,0.9);
    font-family: 'JetBrains Mono', monospace;
}}
QFrame#sep {{
    background: rgba(222,222,222,0.15);
    max-height: 1px;
    min-height: 1px;
}}
"""


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent,
        on_connect_spotify,
        on_device_selected,
        on_color_selected,
        on_always_on_top_changed,
        on_fps_changed,
        on_lpm_changed,
        current_device_id,
        spotify_connected: bool,
        current_default_color,
        current_always_on_top: bool,
        current_fps: int,
        current_low_perf_mode: bool,
    ):
        super().__init__(parent)
        self.setWindowTitle("settings")
        self.setModal(False)
        self.setFixedWidth(360)
        self.setStyleSheet(DIALOG_STYLE)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self._on_connect_spotify = on_connect_spotify
        self._on_device_selected = on_device_selected
        self._on_color_selected = on_color_selected
        self._on_always_on_top_changed = on_always_on_top_changed
        self._on_fps_changed = on_fps_changed
        self._on_lpm_changed = on_lpm_changed
        self._current_default_color = current_default_color


        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        heading = QLabel("panel settings")
        heading.setObjectName("heading")
        layout.addWidget(heading)

        # --- Spotify ---
        layout.addWidget(QLabel("spotify"))
        spotify_row = QHBoxLayout()
        status = "connected — cover art drives color" if spotify_connected else "not connected"
        self.spotify_status = QLabel(status)
        spotify_row.addWidget(self.spotify_status)
        spotify_row.addStretch()
        connect_btn = QPushButton("reconnect" if spotify_connected else "connect spotify")
        connect_btn.clicked.connect(self._handle_connect)
        spotify_row.addWidget(connect_btn)
        layout.addLayout(spotify_row)

        sep1 = QFrame()
        sep1.setObjectName("sep")
        layout.addWidget(sep1)

        # --- Target sound ---
        layout.addWidget(QLabel("target sound (default: everything)"))
        self.device_combo = QComboBox()
        self.device_combo.addItem("System audio — everything", userData=None)
        devices = list_target_devices()
        selected_index = 0
        for i, dev in enumerate(devices, start=1):
            label = dev.name + ("  (loopback)" if dev.is_loopback else "")
            self.device_combo.addItem(label, userData=dev.id)
            if dev.id == current_device_id:
                selected_index = i
        self.device_combo.setCurrentIndex(selected_index)
        self.device_combo.currentIndexChanged.connect(self._handle_device_change)
        layout.addWidget(self.device_combo)

        sep2 = QFrame()
        sep2.setObjectName("sep")
        layout.addWidget(sep2)

        # --- Default color ---
        layout.addWidget(QLabel("default led color"))
        color_row = QHBoxLayout()
        self.color_preview = QLabel()
        self.color_preview.setFixedSize(28, 20)
        self._update_color_preview(current_default_color)
        color_row.addWidget(self.color_preview)
        pick_btn = QPushButton("choose color")
        pick_btn.clicked.connect(self._pick_color)
        color_row.addWidget(pick_btn)
        color_row.addStretch()
        layout.addLayout(color_row)

        # --- Always on top ---
        layout.addWidget(QLabel("window behavior"))
        self.always_on_top_cb = QCheckBox("always on top")
        self.always_on_top_cb.setChecked(current_always_on_top)
        self.always_on_top_cb.stateChanged.connect(self._handle_always_on_top)
        layout.addWidget(self.always_on_top_cb)

        sep3 = QFrame()
        sep3.setObjectName("sep")
        layout.addWidget(sep3)

        # --- Performance: Run FPS (always-visible 3-way) + LPM checkbox ---
        layout.addWidget(QLabel("performance"))

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("run fps"))
        fps_row.addStretch()
        self.fps_combo = QComboBox()
        for fps in FPS_STEPS:  # 60, 45, 30 — all three, always shown
            self.fps_combo.addItem(str(fps), userData=fps)
        if current_fps in FPS_STEPS:
            self.fps_combo.setCurrentIndex(FPS_STEPS.index(current_fps))
        self.fps_combo.currentIndexChanged.connect(self._handle_fps_step_change)
        fps_row.addWidget(self.fps_combo)
        layout.addLayout(fps_row)

        self.lpm_cb = QCheckBox("low performance mode")
        self.lpm_cb.setChecked(current_low_perf_mode)
        self.lpm_cb.stateChanged.connect(self._handle_lpm_toggle)
        layout.addWidget(self.lpm_cb)

        sep4 = QFrame()
        sep4.setObjectName("sep")
        layout.addWidget(sep4)

        footer = QLabel(
            "[ctrl] + [e] - settings   |   [ctrl] + [f] - fullscreen (esc to exit)\n"
            "[win] + [shift] + [w] - bring to front / hide   |   [ctrl] + [k] - quit"
        )
        footer.setStyleSheet("color: rgba(222,222,222,0.45); font-size: 11px; margin-top: 6px;")
        footer.setWordWrap(True)
        layout.addWidget(footer)

    def _update_color_preview(self, rgb):
        r, g, b = [int(c) for c in rgb]
        self.color_preview.setStyleSheet(
            f"background: rgb({r},{g},{b}); border-radius: 5px; border: 1px solid rgba(255,255,255,0.15);"
        )

    def _handle_connect(self):
        self.spotify_status.setText("opening browser…")
        self._on_connect_spotify()

    def _handle_device_change(self, index: int):
        device_id = self.device_combo.itemData(index)
        self._on_device_selected(device_id)

    def _handle_always_on_top(self, state: int):
        is_checked = state == 2
        self._on_always_on_top_changed(is_checked)

    def _handle_fps_step_change(self, index: int):
        fps = self.fps_combo.itemData(index)
        self._on_fps_changed(fps)

    def _handle_lpm_toggle(self, state: int):
        is_checked = state == 2
        self._on_lpm_changed(is_checked)

    def _pick_color(self):
        r, g, b = [int(c) for c in self._current_default_color]
        initial = QColor(r, g, b)
        color = QColorDialog.getColor(initial, self, "default led color")
        if color.isValid():
            rgb = (color.red(), color.green(), color.blue())
            self._current_default_color = rgb
            self._update_color_preview(rgb)
            self._on_color_selected(rgb)

# ============================================================================
# SCREEN WAKE LOCK — best-effort "keep display awake" for fullscreen
# ============================================================================

class ScreenWakeLock:
    """
    Inhibits idle-triggered display sleep/screensaver while active;
    restores normal power management on release. Meant to be held only
    while the widget is fullscreen (see PanelWindow._enter_fullscreen /
    _exit_fullscreen) — a windowed visualizer sitting in the background
    has no business keeping the whole machine awake.

    Platform-specific and deliberately best-effort: acquire() never
    raises. If the relevant mechanism isn't available (missing helper
    binary, unsupported platform, sandboxed environment, etc.) it just
    quietly does nothing rather than taking down the widget over a
    screensaver.
    """

    def __init__(self):
        self._active = False
        self._proc: Optional[subprocess.Popen] = None  # macOS/Linux: the inhibitor-holding process

    def acquire(self) -> None:
        if self._active:
            return
        self._active = True
        if sys.platform == "win32":
            self._acquire_windows()
        elif sys.platform == "darwin":
            self._acquire_macos()
        else:
            self._acquire_linux()

    def release(self) -> None:
        if not self._active:
            return
        self._active = False
        if sys.platform == "win32":
            self._release_windows()
        else:
            self._release_subprocess()

    # --- Windows: SetThreadExecutionState, reset to ES_CONTINUOUS alone
    # to hand power management back once released. ---
    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001
    _ES_DISPLAY_REQUIRED = 0x00000002

    def _acquire_windows(self) -> None:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(
                self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED | self._ES_DISPLAY_REQUIRED
            )
        except Exception:
            pass

    def _release_windows(self) -> None:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self._ES_CONTINUOUS)
        except Exception:
            pass

    # --- macOS: caffeinate ships with the OS, no extra dependency. Held
    # for as long as the subprocess lives; killing it releases the lock. ---
    def _acquire_macos(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["caffeinate", "-d"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._proc = None

    # --- Linux: systemd-inhibit holds the lock for the lifetime of the
    # command it wraps, so wrap an inert `sleep infinity` and kill it to
    # release. Only inhibits idle/sleep (not e.g. shutdown), matching
    # "keep the screen awake" rather than "block the user from suspending". ---
    def _acquire_linux(self) -> None:
        try:
            self._proc = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle:sleep",
                    "--who=panel_widget",
                    "--why=Fullscreen visualizer",
                    "--mode=block",
                    "sleep", "infinity",
                ],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._proc = None

    def _release_subprocess(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception:
            pass
        self._proc = None

# ============================================================================
# MAIN WINDOW — frameless rounded resizable window, hotkeys
# ============================================================================

ASPECT = CANVAS_W / CANVAS_H
CORNER_RADIUS = 22
MIN_WIDTH = 420

# The window's total height is the render area (locked to ASPECT) plus this
# fixed-height chrome below it. Baking these into the aspect math is what
# keeps the render widget's *actual* drawable area matching ASPECT at every
# window size — otherwise the grid gets letterboxed with big side bars, since
# PanelRenderWidget.paintEvent centers+scales CANVAS_W:CANVAS_H to fit
# whatever space it's actually given, not the window's own aspect ratio.
#
# BOTTOM_BAR_H must be tall enough to fit whichever of the bar's two
# children needs more room: ConsoleLabel (LINE_BUFFER lines at its 15px
# monospace font, plus the bar's top margin reserved for text headroom)
# or SpotifyControls (its pill, which is IconButton.SIZE + 2*controls'
# own vertical gap — see SpotifyControls.__init__). Sized from the same
# numbers those two widgets use rather than read from a live QFontMetrics
# or a live SpotifyControls instance, since this constant is needed before
# QApplication (and therefore any font metrics or real widgets) exist.
_CONSOLE_FONT_PX = 15
_CONSOLE_LINE_H_ESTIMATE = round(_CONSOLE_FONT_PX * 1.45)  # matches typical monospace lineSpacing()
# ^ was 1.3, which undershot real descender height (ascent+descent+leading)
# for most monospace fonts at this size and clipped tails on glyphs like
# "y"/"g"/"p" against the bar's fixed height. 1.45 gives descenders room
# without needing live QFontMetrics (see note above on why this stays a
# static estimate).
_BAR_TOP_MARGIN = 6
_BAR_BOTTOM_MARGIN = 2
# + ConsoleLabel.BOTTOM_SAFETY_PX: mirrors the real fixed-height formula in
# ConsoleLabel.__init__ (line_h*LINE_BUFFER + TEXT_SLIDE_PX + BOTTOM_SAFETY_PX)
# now that the safety pad is genuine extra height there rather than borrowed
# from the two-line budget, so this static estimate doesn't quietly
# under-count the live widget by BOTTOM_SAFETY_PX again. Works out to the
# same 56px as _controls_need below, so BOTTOM_BAR_H — and the window's
# overall bottom gap — doesn't move: the console side just starts actually
# using headroom the bar already reserved for the controls pill, instead of
# the bar itself growing.
_console_need = (
    _CONSOLE_LINE_H_ESTIMATE * 2 + ConsoleLabel.BOTTOM_SAFETY_PX
    + _BAR_TOP_MARGIN + _BAR_BOTTOM_MARGIN
)  # LINE_BUFFER=2
_controls_need = _IconButton.SIZE + SpotifyControls.GAP * 2 + _BAR_TOP_MARGIN + _BAR_BOTTOM_MARGIN
BOTTOM_BAR_H = max(_console_need, _controls_need)
BOTTOM_MARGIN = 10
VISUALIZER_SIDE_PAD = 14  # left/right breathing room around the LED grid

VISUALIZER_TOP_PAD = 12   # top breathing room, keeps the grid off the rounded window edge
TEXT_SLIDE_PX = 10  # how much the visualizer and text move up when text wraps
CHROME_H = BOTTOM_BAR_H + BOTTOM_MARGIN



def _window_height_for_width(win_w: int) -> int:
    render_h = win_w / ASPECT
    return int(render_h + VISUALIZER_TOP_PAD + CHROME_H)

# NOTE: paste a real Spotify app Client ID here (developer.spotify.com/dashboard),
# and register REDIRECT_URI from spotify_integration.py as that app's Redirect URI.
SPOTIFY_CLIENT_ID = "890fb35c2b40401aabd06a1aedb32bb6"


class PanelWindow(QWidget):
    # Spotify's polling/OAuth work happens on plain background threads
    # (threading.Thread, not QThread), which never run a Qt event loop.
    # QTimer.singleShot(0, ...) called from such a thread silently never
    # fires — there's no loop there to deliver the timer event — so
    # anything marshaled that way (console text, play/pause icon) just
    # never arrives. Signals are the correct cross-thread bridge: Qt
    # detects the emitting thread differs from the receiving QObject's
    # thread and auto-queues the call onto the GUI thread's event loop,
    # which *is* running. No singleShot(0, ...) needed anywhere here.
    _console_signal = Signal(str)
    _playing_signal = Signal(bool)
    _track_signal = Signal(str)  # raw Spotify track-change, pre boot-delay gate
    _toggle_visibility_signal = Signal()  # see the global hotkey setup below
    _progress_signal = Signal(int, int, float, bool)  # progress_ms, duration_ms, captured_at, is_playing

    # -- animated visualizer movement, mirrors ConsoleLabel's slideOffset pattern --
    

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(MIN_WIDTH, _window_height_for_width(MIN_WIDTH))
        self.resize(920, _window_height_for_width(920))

        # Gate for the "pop in after 6s since boot" behavior on the
        # Spotify track line — see _on_spotify_track_change below.
        self._boot_time = time.monotonic()
        self._track_reveal_delay_s = 6.0
        self._track_reveal_done = False
        self._pending_track_info: Optional[str] = None

        # Load persisted settings (device, color, always-on-top, fps,
        # low-perf-mode) up front so every widget below can be
        # constructed with the right values from the start, rather than
        # constructing with defaults and re-applying afterward.
        saved = load_app_settings()

        self.panel = LedPanel()
        self.default_color = list(saved.get("default_color", DEFAULT_LED_RGB))
        self.always_on_top = saved.get("always_on_top", True)
        self.fps = saved.get("fps", DEFAULT_FPS)
        if self.fps not in FPS_STEPS:
            self.fps = DEFAULT_FPS
        self.low_perf_mode = saved.get("low_perf_mode", False)
        self._selected_device_id = saved.get("device_id")
        if self.default_color != list(DEFAULT_LED_RGB):
            # LedPanel always boots at DEFAULT_LED_RGB — push the
            # restored color in immediately (duration=0, no crossfade)
            # so the panel doesn't flash the stock default before
            # Spotify/silence logic gets a chance to touch it.
            self.panel.color(*self.default_color, now=time.monotonic() * 1000.0, duration=0)
        self.panel.low_perf_mode = self.low_perf_mode

        self.render_widget = PanelRenderWidget(self.panel, fps=self.fps)
        self.render_widget.low_perf_mode = self.low_perf_mode

        self.audio = AudioAnalyzer(
            on_frame=self._on_audio_frame,
            on_silence_gap=self._on_silence_gap,
        )
        self.spotify = SpotifyIntegration(
            SPOTIFY_CLIENT_ID,
            on_color=self._on_spotify_color,
            on_track_change=self._on_spotify_track_change,
            on_playing_change=self._on_spotify_playing_change,
            on_progress=self._on_spotify_progress,
        )
        self.spotify.try_resume()

        # Bottom Bar Container
        self.bottom_bar = QWidget(self)
        self.bottom_bar.setFixedHeight(BOTTOM_BAR_H)
        self.bottom_bar.setAttribute(Qt.WA_TranslucentBackground)
        
        bottom_layout = QHBoxLayout(self.bottom_bar)
        bottom_layout.setContentsMargins(16, _BAR_TOP_MARGIN, 16, _BAR_BOTTOM_MARGIN)
        bottom_layout.setSpacing(0)

        self.console = ConsoleLabel(self.bottom_bar)
        bottom_layout.addWidget(self.console, stretch=75, alignment=Qt.AlignBottom)

        led_color_fn = lambda: self.panel.get_led_rgb(time.monotonic() * 1000.0)

        # The timestamp and the transport-control pill share one
        # footprint (same size, same position in the bar) rather than
        # sitting side by side — a plain QWidget with no layout manager
        # holds both children at that shared rect (set in
        # _position_controls_stack below) so one can fade out exactly
        # as the other fades in, "covering" the same spot instead of
        # the two competing for separate space in the bar.
        self.controls_stack = QWidget(self.bottom_bar)
        self.controls_stack.setAttribute(Qt.WA_TranslucentBackground)

        self.timestamp = TimestampLabel(self.controls_stack, led_color_fn=led_color_fn)

        self.controls = SpotifyControls(
            self.controls_stack,
            self._dispatch_playback_control,
            led_color_fn=led_color_fn,
        )
        # Match controls_stack's size to the pill's own fixed size
        # (set inside SpotifyControls.__init__) rather than
        # recomputing the same 144 x (SIZE + GAP*2) formula here —
        # one source of truth for that number.
        self.controls_stack.setFixedSize(self.controls.size())
        self._position_controls_stack()

        bottom_layout.addWidget(self.controls_stack, stretch=25, alignment=Qt.AlignRight | Qt.AlignVCenter)

        # Queued cross-thread delivery — see the note on the class body.
        self._console_signal.connect(self.console.print)
        self._playing_signal.connect(self.controls.set_playing)
        self._progress_signal.connect(self.timestamp.set_progress)
        self._track_signal.connect(self._reveal_track_info)
        self._toggle_visibility_signal.connect(self.toggle_visibility)

        self.render_wrap = QWidget(self)
        render_wrap_layout = QVBoxLayout(self.render_wrap)
        render_wrap_layout.setContentsMargins(
            VISUALIZER_SIDE_PAD, VISUALIZER_TOP_PAD, VISUALIZER_SIDE_PAD, 0
        )
        render_wrap_layout.addWidget(self.render_widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, BOTTOM_MARGIN)
        layout.setSpacing(0)
        layout.addWidget(self.render_wrap, stretch=1)
        
        layout.addWidget(self.bottom_bar, stretch=0)


        # Initial Boot Message
        QTimer.singleShot(100, lambda: self.console.print(
            "Settings [Ctrl+E]  |  Fullscreen [Ctrl+F]  |  Bring to front [Win+Shift+W]  |  Quit [Ctrl+K]"
        ))
        QTimer.singleShot(5000, lambda: self.console.print(""))


        self._drag_pos: QPoint | None = None
        self._resizing = False
        self._resize_start_geo = None
        self._resize_start_pos = None
        self._grip_hover = False  # tracks whether the cursor is over the resize-grip corner

        # Guards the delayed half of _swap_controls_stack: bumped on every
        # enter/leaveEvent so a fade_in scheduled by an earlier hover flip
        # (still waiting out STACK_SWITCH_GAP_MS) is dropped if the cursor
        # has since flipped again, rather than firing on top of the newer
        # state.
        self._stack_swap_token = 0

        # --- Fullscreen mode state ---
        self._is_fullscreen = False
        self._pre_fullscreen_geo = None       # windowed geometry to restore on exit
        self._pre_fullscreen_flags = None     # windowFlags() to restore on exit (Tool/frameless/etc.)
        self._cursor_hidden = False
        self._cursor_idle_timer = QTimer(self)
        self._cursor_idle_timer.setSingleShot(True)
        self._cursor_idle_timer.timeout.connect(self._hide_cursor_if_fullscreen)
        self.CURSOR_IDLE_MS = 3000
        self._wake_lock = ScreenWakeLock()    # held only while fullscreen — see _enter/_exit_fullscreen

        # Needed so mouseMoveEvent fires on plain hover, not just while a
        # button is held — that's what lets the grip highlight/cursor
        # react before the user starts dragging.
        self.setMouseTracking(True)

        # PanelWindow.mouseMoveEvent above only ever sees moves over
        # this widget's own bare background — Qt routes a mouse-move to
        # whichever *child* widget is actually under the cursor instead
        # (render_widget, the console, the transport-control buttons),
        # and none of those forward it back up. In windowed mode that's
        # invisible because the grip/drag logic those moves would drive
        # only matters near the very edges, which are mostly bare
        # background anyway. In fullscreen, render_widget alone covers
        # almost the entire screen, so nearly every real mouse move
        # never reached mouseMoveEvent at all — the cursor only
        # reappeared on the rare move that happened to land on the
        # thin uncovered margin, making the 3s idle-hide look like it
        # needed far more movement than intended. Installing self as an
        # application-wide event filter catches MouseMove regardless of
        # which child widget it's actually delivered to.
        QApplication.instance().installEventFilter(self)

        self._install_shortcuts()

        self._settings_dialog: SettingsDialog | None = None
        # self._selected_device_id was already restored from disk above.
        # AudioAnalyzer has no constructor arg for it, so set the field
        # directly (not via set_device(), which also calls restart() —
        # unnecessary and premature before start() below).
        self.audio.device_id = self._selected_device_id

        self.audio.start()

        # Global hotkey to hide/show (Win+Shift+W)
        if keyboard:
            def on_activate():
                # pynput's GlobalHotKeys listener runs on its own plain
                # thread, same as Spotify's polling threads above — a
                # QTimer.singleShot(0, ...) fired from here never arrives
                # (no Qt event loop on this thread to deliver it), which is
                # exactly why this hotkey silently did nothing. Emitting
                # the signal instead lets Qt queue it onto the GUI thread
                # correctly, same as the Spotify signals do.
                self._toggle_visibility_signal.emit()

            self._global_hotkey = keyboard.GlobalHotKeys({
                '<cmd>+<shift>+w': on_activate
            })
            self._global_hotkey.start()
            print("[panel_widget] Global hotkey Win+Shift+W is now active.")



    # ---------------------------------------------------------------
    # Rounded-corner clip, kept pure black with the glow-safe padding
    # already reserved inside the canvas coordinate space by the
    # engine's own PAD constant — nothing here crops that margin.
    # ---------------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self._is_fullscreen:
            # Fullscreen: don't pin render_wrap to a width-derived fixed
            # height — that formula assumes the aspect-locked windowed
            # sizing model (width picked by the user, height follows).
            # In fullscreen the viewport's own aspect ratio is whatever
            # the monitor has, so instead let render_wrap fill all
            # available vertical space above the bottom bar (its
            # stretch=1 in the QVBoxLayout already does this once no
            # fixed height overrides it) and let PanelRenderWidget's own
            # paintEvent center+letterbox CANVAS_W:CANVAS_H within that
            # box — which is exactly the "visualizer vertically
            # centered" behavior asked for.
            self.render_wrap.setMinimumHeight(0)
            self.render_wrap.setMaximumHeight(16777215)  # Qt's QWIDGETSIZE_MAX — clears setFixedHeight's cap
        else:
            # Windowed: fix the visualizer height to prevent it from
            # shrinking when the spacer grows.
            self.render_wrap.setFixedHeight(int(self.width() / ASPECT) + VISUALIZER_TOP_PAD)

    def eventFilter(self, watched, event):  # noqa: N802
        # See the installEventFilter() call in __init__ for why this
        # exists: child widgets (render_widget, console, controls)
        # swallow mouse-move events before PanelWindow.mouseMoveEvent
        # ever sees them. This catches MouseMove application-wide so
        # the fullscreen cursor-idle timer resets on *any* movement
        # over the widget, not just the sliver of bare background.
        # Always returns False — this only observes the event, it
        # never consumes it, so every widget's own handling continues
        # exactly as before.
        if self._is_fullscreen and event.type() == QEvent.MouseMove:
            self._restart_cursor_idle_timer()
        return False

    def changeEvent(self, event):  # noqa: N802
        # Drives Spotify's adaptive poll cadence: focused (1s) vs
        # background (3-5s jittered). QEvent.ActivationChange fires on
        # both gaining and losing OS focus, so read isActiveWindow()
        # fresh each time rather than assuming direction from the event.
        if event.type() == QEvent.ActivationChange:
            self.spotify.set_window_focused(self.isActiveWindow())
        super().changeEvent(event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        # Rounded corners make sense against a desktop background; at
        # fullscreen the widget fills the whole screen edge-to-edge, so
        # keep it a plain rectangle instead of clipping the corners
        # (radius=0) against nothing.
        radius = 0 if self._is_fullscreen else CORNER_RADIUS
        path.addRoundedRect(QRectF(0, 0, self.width(), self.height()), radius, radius)
        painter.fillPath(path, QBrush(QColor(0, 0, 0, 255)))
        painter.end()

    # ---------------------------------------------------------------
    # Fixed-aspect resize: dragging the bottom-right corner (an 18px
    # grip zone) resizes while keeping CANVAS_W:CANVAS_H locked, same
    # ratio the JS's fitCanvas() derives from GRID_W/GRID_H + GLOW_PAD.
    # Dragging anywhere else on the background moves the window, since
    # a frameless window has no titlebar to grab.
    # ---------------------------------------------------------------
    GRIP = 18

    def _in_resize_grip(self, pos: QPoint) -> bool:
        return pos.x() >= self.width() - self.GRIP and pos.y() >= self.height() - self.GRIP

    def mousePressEvent(self, event):
        if self._is_fullscreen:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            if self._in_resize_grip(event.pos()):
                self._resizing = True
                self._resize_start_geo = self.geometry()
                self._resize_start_pos = event.globalPosition().toPoint()
            else:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_fullscreen:
            # Any movement shows the cursor immediately and resets the
            # 3s idle countdown. No resize-grip / drag-to-move handling
            # in fullscreen — the window has no user-adjustable
            # geometry there, same reason those are skipped below.
            self._restart_cursor_idle_timer()
            super().mouseMoveEvent(event)
            return

        if self._resizing and self._resize_start_geo is not None:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            new_w = max(MIN_WIDTH, self._resize_start_geo.width() + delta.x())
            new_h = _window_height_for_width(new_w)
            self.resize(new_w, new_h)
        elif self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        else:
            # Not actively dragging/resizing — just hovering. Track
            # whether we're over the grip so the cursor swaps to signal
            # the corner is draggable.
            over_grip = self._in_resize_grip(event.pos())
            if over_grip != self._grip_hover:
                self._grip_hover = over_grip
                self.setCursor(Qt.SizeFDiagCursor if over_grip else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        # Mouse left the window entirely — drop the grip cursor rather
        # than leaving it stuck on if the pointer exits fast.
        if self._grip_hover and not self._resizing:
            self._grip_hover = False
            self.setCursor(Qt.ArrowCursor)
        # Transport controls fade with the whole widget's hover state now
        # (not just the bottom bar strip) so they appear as soon as the
        # cursor is anywhere over the panel, not only over that section.
        # The timestamp does the opposite — it occupies the exact same
        # spot (see controls_stack in __init__) and is meant to show
        # only when the transport pill isn't, so the two never overlap.
        self._swap_controls_stack(fade_out=self.controls, fade_in=self.timestamp)
        super().leaveEvent(event)

    def enterEvent(self, event):  # noqa: N802
        self._swap_controls_stack(fade_out=self.timestamp, fade_in=self.controls)
        super().enterEvent(event)

    def _swap_controls_stack(self, fade_out, fade_in) -> None:
        # Sequential rather than simultaneous: `fade_out` finishes
        # fading all the way out, then — after a small beat
        # (STACK_SWITCH_GAP_MS) — `fade_in` starts fading in. Without
        # the gap the two visibly crossfade through each other in the
        # same footprint instead of reading as one swapping out for
        # the other.
        self._stack_swap_token += 1
        token = self._stack_swap_token
        fade_out.fade_out()

        def _do_fade_in():
            if self._stack_swap_token == token:  # not superseded by a newer hover flip
                fade_in.fade_in()

        QTimer.singleShot(STACK_FADE_MS + STACK_SWITCH_GAP_MS, _do_fade_in)

    def mouseReleaseEvent(self, event):
        self._resizing = False
        self._drag_pos = None
        if self._grip_hover and not self._in_resize_grip(event.pos()):
            self._grip_hover = False
            self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------------
    # Hotkeys
    # ---------------------------------------------------------------
    def toggle_visibility(self):
        """
        Win+Shift+W behavior:
          - hidden               -> show, raise, focus
          - visible but NOT the frontmost window -> just raise/focus
            it (bring-to-top), same as a click would, WITHOUT
            hiding it first — previously this branch fell into the
            plain "isVisible() -> hide()" case below, so pressing the
            hotkey while something else was in front of the widget
            hid it instead of surfacing it, which reads as the hotkey
            "doing nothing useful" from the user's point of view.
          - visible AND already frontmost -> hide (the original
            toggle behavior, unchanged for the one case it actually
            makes sense: you're looking at it, so hide it).
        isActiveWindow() is what distinguishes "on top" from merely
        "visible" — a window can be uncovered-but-not-focused too, but
        Qt/most window managers keep those in sync closely enough for
        this to match what the user perceives as "on top" in practice.
        """
        if not self.isVisible():
            self._bring_to_front()
        elif not self.isActiveWindow():
            self._bring_to_front()
        else:
            self.hide()

    def _bring_to_front(self):
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------------------------------------------------------------
    # Fullscreen mode
    # ---------------------------------------------------------------
    def toggle_fullscreen(self):
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _exit_fullscreen_if_active(self):
        # Bound to bare Esc — only acts while actually fullscreen, so
        # Esc is otherwise a no-op (doesn't e.g. close the window).
        if self._is_fullscreen:
            self._exit_fullscreen()

    def _enter_fullscreen(self):
        if self._is_fullscreen:
            return
        self._is_fullscreen = True
        self._pre_fullscreen_geo = self.geometry()
        self._pre_fullscreen_flags = self.windowFlags()

        # Dragging/resizing (the frameless-window affordances) don't
        # make sense at fullscreen size — drop any in-progress drag so
        # a stale _drag_pos doesn't yank the window the instant it's
        # restored later.
        self._drag_pos = None
        self._resizing = False
        if self._grip_hover:
            self._grip_hover = False
            self.setCursor(Qt.ArrowCursor)

        self.showFullScreen()
        # showFullScreen() changes the widget's size, which fires
        # resizeEvent — that's what actually switches render_wrap over
        # to the fill-and-center sizing (see resizeEvent above).

        self._restart_cursor_idle_timer()
        self._wake_lock.acquire()

    def _exit_fullscreen(self):
        if not self._is_fullscreen:
            return
        self._is_fullscreen = False
        self._cursor_idle_timer.stop()
        self._show_cursor()
        self._wake_lock.release()

        self.showNormal()
        if self._pre_fullscreen_geo is not None:
            self.setGeometry(self._pre_fullscreen_geo)
        # windowFlags are preserved by Qt across showFullScreen()/
        # showNormal() on every platform this actually ships on, but
        # restoring them explicitly costs nothing and guards against
        # any platform quirk that doesn't.
        if self._pre_fullscreen_flags is not None:
            self.setWindowFlags(self._pre_fullscreen_flags)
            self.show()

    # ---------------------------------------------------------------
    # Cursor auto-hide (fullscreen only): 3s of no mouse movement
    # hides the cursor; any movement shows it again immediately.
    # ---------------------------------------------------------------
    def _restart_cursor_idle_timer(self):
        self._show_cursor()
        if self._is_fullscreen:
            self._cursor_idle_timer.start(self.CURSOR_IDLE_MS)

    def _hide_cursor_if_fullscreen(self):
        if self._is_fullscreen and not self._cursor_hidden:
            self._cursor_hidden = True
            self.setCursor(Qt.BlankCursor)
            # setCursor() on self doesn't override a child that set its
            # own cursor explicitly — and _IconButton does exactly that
            # (Qt.PointingHandCursor, for the hand-cursor-on-hover
            # affordance in windowed mode). Without this, the three
            # transport buttons would keep showing a visible cursor
            # over themselves even while the rest of the fullscreen
            # window is blanked.
            for btn in (self.controls.prev_btn, self.controls.play_btn, self.controls.next_btn):
                btn.setCursor(Qt.BlankCursor)
            # Fullscreen-only: the transport pill is a UI affordance
            # meant to respond to an active cursor — there's no pointer
            # to hover it or click its buttons once the cursor itself
            # is gone. Route through the same delayed swap enterEvent/
            # leaveEvent use (fade the pill fully out, then — after
            # STACK_SWITCH_GAP_MS — fade the timestamp in) rather than
            # an outright .hide(), so the timestamp is actually told to
            # take over instead of just being left at whatever opacity
            # hovering happened to leave it in. Disabled outright so it
            # can't still catch a click while faded/invisible.
            self.controls.setEnabled(False)
            self._swap_controls_stack(fade_out=self.controls, fade_in=self.timestamp)

    def _show_cursor(self):
        if self._cursor_hidden:
            self._cursor_hidden = False
            self.setCursor(Qt.ArrowCursor)
            for btn in (self.controls.prev_btn, self.controls.play_btn, self.controls.next_btn):
                btn.setCursor(Qt.PointingHandCursor)
            # Mirror image of _hide_cursor_if_fullscreen: swap the
            # timestamp back out for the transport pill, same delayed
            # sequencing.
            self.controls.setEnabled(True)
            self._swap_controls_stack(fade_out=self.timestamp, fade_in=self.controls)

    def _install_shortcuts(self):
        settings_sc = QShortcut(QKeySequence("Ctrl+E"), self)
        settings_sc.activated.connect(self._open_settings)
        quit_sc = QShortcut(QKeySequence("Ctrl+K"), self)
        quit_sc.activated.connect(self._quit)
        fullscreen_sc = QShortcut(QKeySequence("Ctrl+F"), self)
        fullscreen_sc.activated.connect(self.toggle_fullscreen)
        # Esc only ever exits fullscreen — it's not bound to anything
        # while windowed, so it doesn't clash with, e.g., a settings
        # dialog's own Esc-to-close behavior.
        exit_fullscreen_sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        exit_fullscreen_sc.activated.connect(self._exit_fullscreen_if_active)

    def _open_settings(self):
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(
            self,
            on_connect_spotify=self.spotify.connect,
            on_device_selected=self._set_target_device,
            on_color_selected=self._set_default_color,
            on_always_on_top_changed=self._set_always_on_top,
            on_fps_changed=self._set_fps,
            on_lpm_changed=self._set_lpm,
            current_device_id=self._selected_device_id,
            spotify_connected=self.spotify.active,
            current_default_color=self.default_color,
            current_always_on_top=self.always_on_top,
            current_fps=self.fps,
            current_low_perf_mode=self.low_perf_mode,
        )
        dialog.destroyed.connect(lambda: setattr(self, "_settings_dialog", None))
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.show()
        self._settings_dialog = dialog

    def _on_spotify_track_change(self, track_info: str):
        # Called from Spotify's polling thread. Emitting a Signal (rather
        # than calling QTimer.singleShot directly here) is what actually
        # gets this onto the GUI thread — see the note on the class body.
        # The 6s "pop in after boot" gating itself happens GUI-side, in
        # _reveal_track_info, once the signal has already crossed threads.
        self._track_signal.emit(track_info)

    def _reveal_track_info(self, track_info: str) -> None:
        """
        The first track line after boot pops in 6s after boot (so it
        doesn't collide with/cut off the "Settings - [Ctrl]+[E]..." boot
        hint, which is on screen for the first 5s). Once that initial
        6s window has passed, later track changes (skips, pauses, etc.)
        print immediately as normal — only the first reveal is delayed.
        """
        if self._track_reveal_done:
            self._console_signal.emit(track_info)
            return

        elapsed = time.monotonic() - self._boot_time
        remaining = self._track_reveal_delay_s - elapsed
        self._pending_track_info = track_info
        if remaining <= 0:
            self._flush_pending_track_info()
        else:
            QTimer.singleShot(int(remaining * 1000), self._flush_pending_track_info)

    def _flush_pending_track_info(self) -> None:
        self._track_reveal_done = True
        if self._pending_track_info is not None:
            self._console_signal.emit(self._pending_track_info)
            self._pending_track_info = None

    def _on_spotify_playing_change(self, is_playing: bool):
        self._playing_signal.emit(is_playing)

    def _on_spotify_progress(self, progress_ms: int, duration_ms: int, captured_at: float, is_playing: bool):
        # Called from SpotifyIntegration's poll thread (see the note on
        # this class's Signal declarations) — emit rather than touch
        # self.timestamp directly, so the actual QLabel update happens
        # on the GUI thread.
        self._progress_signal.emit(progress_ms, duration_ms, captured_at, is_playing)

    def _position_controls_stack(self):
        # controls_stack has no layout manager — self.timestamp and
        # self.controls are both manually sized to its full rect so
        # they occupy exactly the same space and one can visually
        # "cover" the other via opacity alone (see leaveEvent/
        # enterEvent). Called once at construction; controls_stack is
        # fixed-size so this never needs to run again on resize.
        rect = self.controls_stack.rect()
        self.timestamp.setGeometry(rect)
        self.controls.setGeometry(rect)

    def _quit(self):
        self.audio.stop()
        self.spotify.stop()
        self._wake_lock.release()  # safety net: normally released by _exit_fullscreen already
        QApplication.instance().quit()

    # ---------------------------------------------------------------
    # Audio -> LED writes
    # ---------------------------------------------------------------
    def _on_audio_frame(self, writes):
        if not writes:
            return
        now = time.monotonic() * 1000.0
        self.panel.led(writes, now=now)

    def _on_silence_gap(self):
        if self.spotify.active:
            return
        r, g, b = random.choice(NEON_PALETTE)
        now = time.monotonic() * 1000.0
        self.panel.color(r, g, b, now=now, duration=SILENCE_COLOR_TRANSITION_MS)

    def _on_spotify_color(self, r, g, b, duration_ms):
        now = time.monotonic() * 1000.0
        self.panel.color(r, g, b, now=now, duration=duration_ms)

    # ---------------------------------------------------------------
    # Transport controls — dispatched off the GUI thread
    # ---------------------------------------------------------------
    def _dispatch_playback_control(self, action: str) -> None:
        """
        SpotifyIntegration.playback_control() does a blocking
        `requests` call (plus a possible token refresh, also blocking)
        before it ever returns. Calling it directly from a button's
        `clicked` handler — as this used to do — runs that network
        round-trip on the GUI thread, so the whole widget (visualizer
        included) freezes for however long Spotify's API takes to
        answer.

        Firing it on a plain background thread instead — same
        `threading.Thread`-based approach SpotifyIntegration already
        uses for polling/resume — keeps the click instant. Nothing in
        playback_control() touches Qt or GUI state directly (it only
        reads/writes self.state and eventually calls poke(), which
        just sets a threading.Event), so it's safe to run off-thread
        with no signal marshaling needed on the way in.
        """
        threading.Thread(
            target=self.spotify.playback_control,
            args=(action,),
            daemon=True,
        ).start()

    def _persist_settings(self) -> None:
        """Writes the current settings snapshot to disk. Called after
        every individual setting change rather than once at exit, so
        nothing is lost if the process is killed instead of quit
        cleanly (Ctrl+K)."""
        save_app_settings({
            "device_id": self._selected_device_id,
            "default_color": self.default_color,
            "always_on_top": self.always_on_top,
            "fps": self.fps,
            "low_perf_mode": self.low_perf_mode,
        })

    def _set_always_on_top(self, enabled: bool):
        self.always_on_top = enabled
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        self._persist_settings()

    def _set_target_device(self, device_id):
        self._selected_device_id = device_id
        self.audio.set_device(device_id)
        self._persist_settings()

    def _set_default_color(self, rgb):
        self.default_color = list(rgb)
        now = time.monotonic() * 1000.0
        self.panel.color(rgb[0], rgb[1], rgb[2], now=now)
        self._persist_settings()

    def _set_fps(self, fps: int):
        if fps not in FPS_STEPS:
            return
        self.fps = fps
        self.render_widget.set_fps(fps)
        self._persist_settings()

    def _set_lpm(self, enabled: bool):
        self.low_perf_mode = enabled
        self.panel.low_perf_mode = enabled
        self.render_widget.low_perf_mode = enabled
        self._persist_settings()


# ============================================================================
# ENTRY POINT — process detachment + app launch
# ============================================================================

SPOTIFY_CLIENT_ID_NOTE = None  # (see SPOTIFY_CLIENT_ID near the top of MAIN WINDOW section)


def _detach_if_needed() -> None:
    """
    Re-exec detached from the controlling terminal/parent, once.
    Uses an env var as a re-entry guard so the detached copy doesn't
    try to detach again. On POSIX this re-execs via subprocess.Popen
    with start_new_session=True, landing the child in a new session
    so SIGHUP from a closed terminal never reaches it; on Windows,
    DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP achieves the same
    "survives closing the launching console" behavior.

    Skipped entirely in a frozen build (see _is_frozen): a
    double-clicked .exe/.app is launched by the OS with no controlling
    terminal to detach from in the first place, and re-execing via
    [sys.executable, os.path.abspath(__file__), ...] would try to
    launch the packaged exe with a source-file path as an argument,
    which is not what this exe expects.
    """
    if _is_frozen():
        return

    if os.environ.get("_PANEL_DETACHED") == "1":
        return

    env = os.environ.copy()
    env["_PANEL_DETACHED"] = "1"

    if os.name == "nt":
        import subprocess
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *sys.argv[1:]],
            env=env,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sys.exit(0)
    else:
        # Re-exec detached via subprocess.Popen + start_new_session,
        # rather than a raw os.fork()/os.fork() double-fork.
        #
        # By this point in startup, several imports (requests,
        # soundcard, pynput) may already have spun up background
        # threads — e.g. soundcard's CoreAudio enumeration on macOS.
        # os.fork() in an already-multithreaded process only
        # duplicates the calling thread; any lock another thread held
        # at fork time is duplicated in its locked state too, forever
        # unlockable in the child. On Linux this mostly goes
        # unnoticed; on modern macOS it reliably segfaults
        # (EXC_BAD_ACCESS) inside Apple's own threaded frameworks,
        # which are explicitly documented as not fork-safe.
        #
        # subprocess.Popen forks-and-execs internally in a
        # C-implemented, signal/thread-safe way (posix_spawn under
        # the hood on modern Python), sidestepping the hazard
        # entirely. start_new_session=True is the modern equivalent
        # of the old setsid() call — new session, no controlling
        # terminal, survives the parent terminal closing (SIGHUP).
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *sys.argv[1:]],
            env=env,
            start_new_session=True,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sys.exit(0)


def main() -> None:
    try:
        _detach_if_needed()

        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(True)

        window = PanelWindow()
        window.show()

        sys.exit(app.exec())
    except Exception as e:
        with open(CRASH_LOG_PATH, "a") as f:
            f.write("\n--- Fatal Error in main ---\n")
            traceback.print_exc(file=f)
        print(f"Fatal error: {e}")



if __name__ == "__main__":
    main()