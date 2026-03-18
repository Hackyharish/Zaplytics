"""
╔══════════════════════════════════════════════════════════════════╗
║   STM32 G474RE — CZT Power Analyser Dashboard                   ║
║   Reads LPUART1 @ 209700 baud via pyserial                      ║
║                                                                 ║
║   Install:  pip install pyserial matplotlib numpy               ║
║   Run:      python power_analyzer_dashboard.py                  ║
║             python power_analyzer_dashboard.py COM3             ║
║             python power_analyzer_dashboard.py /dev/ttyUSB0     ║
╚══════════════════════════════════════════════════════════════════╝

Parses the exact UART frame your firmware emits:

  [V] Min:XXXX Max:XXXX Pk-Pk:XXXX Bias:XXX.X Freq:XX.XXHz VRMS:X.XXXXV
  [V] CZT Harmonics (fund=XX.XXHz  fR=X.XXXXHz)
  H01 (  50Hz):  X.XXXXv  [bin XXX]
  ...
  THD:  XX.XX%

  [I] Bias:XXXX | V_adc=X.XXXv V_sens=X.XXXv V0G_cal=X.XXXv | PkPk:XXXX | Freq:XX.XXHz | IRMS:X.XXXXA
  [I] CZT Harmonics (fund=XX.XXHz  fR=X.XXXXHz)
  H01 (  50Hz):  X.XXXXa rms  [bin XXX]
  ...   
  THD:  XX.XX%

  [PWR] Active:  XXX.XX W | Apparent:  XXX.XX VA | PF: X.XXX | Phase:  XX.X deg
  ----------------------------------------
"""

import sys
import re
import threading
import time
import collections
import queue
from datetime import datetime

import serial
import serial.tools.list_ports
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.animation as animation
from matplotlib.patches import FancyBboxPatch, Arc, FancyArrowPatch
from matplotlib.ticker import MaxNLocator
import matplotlib.patheffects as pe

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
BAUD_RATE     = 209700
NUM_HARMONICS = 10
HISTORY_LEN   = 120      # data-points kept in rolling window
REFRESH_MS    = 250      # plot refresh interval in ms

# ═══════════════════════════════════════════════════════════════════════════════
# THEME  — phosphor-green oscilloscope / retro instrument aesthetic
# ═══════════════════════════════════════════════════════════════════════════════
C = {
    "bg":       "#080c08",   # near-black with slight green tint
    "panel":    "#0d130d",
    "panel2":   "#111811",
    "border":   "#1a2e1a",
    "grid":     "#0f1f0f",
    "glow":     "#00ff41",   # phosphor green
    "glow2":    "#39ff14",
    "volt":     "#00e676",   # bright green  → voltage
    "curr":     "#00b0ff",   # cyan-blue     → current
    "pwr":      "#ff9100",   # amber         → power
    "pf":       "#ea80fc",   # magenta       → power factor
    "warn":     "#ff3d00",   # red-orange    → THD warning
    "ok":       "#69f0ae",   # mint          → THD good
    "text_hi":  "#c8ffc8",
    "text_mid": "#4caf50",
    "text_lo":  "#1b5e20",
    "sep":      "#2e7d32",
}

HARM_PALETTE = [
    "#00e676","#00b0ff","#ff9100","#ea80fc",
    "#ffeb3b","#69f0ae","#ff6e40","#80d8ff",
    "#f48fb1","#b9f6ca",
]

# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODEL  (thread-safe)
# ═══════════════════════════════════════════════════════════════════════════════
class LiveData:
    def __init__(self):
        self._lock = threading.Lock()

        # Latest scalars
        self.vrms           = 0.0
        self.irms           = 0.0
        self.freq_v         = 50.0
        self.freq_i         = 50.0
        self.active_power   = 0.0
        self.apparent_power = 0.0
        self.power_factor   = 0.0
        self.phase_angle    = 0.0
        self.thd_v          = 0.0
        self.thd_i          = 0.0
        self.reactive_power = 0.0   # computed: Q = sqrt(S²-P²)

        # Latest harmonic amplitudes
        self.harm_v = [0.0] * NUM_HARMONICS
        self.harm_i = [0.0] * NUM_HARMONICS

        # Rolling history deques
        N = HISTORY_LEN
        self.hist_t     = collections.deque(maxlen=N)
        self.hist_vrms  = collections.deque(maxlen=N)
        self.hist_irms  = collections.deque(maxlen=N)
        self.hist_pwr   = collections.deque(maxlen=N)
        self.hist_pf    = collections.deque(maxlen=N)
        self.hist_thd_v = collections.deque(maxlen=N)
        self.hist_thd_i = collections.deque(maxlen=N)

        # Meta
        self.frames     = 0
        self.port       = "—"
        self.last_ts    = None
        self.connected  = False

    def push(self, snap: dict):
        with self._lock:
            for k, v in snap.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            # derive reactive power
            s = self.apparent_power
            p = self.active_power
            self.reactive_power = float(np.sqrt(max(0.0, s*s - p*p)))

            t = time.monotonic()
            self.hist_t.append(t)
            self.hist_vrms.append(self.vrms)
            self.hist_irms.append(self.irms)
            self.hist_pwr.append(self.active_power)
            self.hist_pf.append(self.power_factor)
            self.hist_thd_v.append(self.thd_v)
            self.hist_thd_i.append(self.thd_i)
            self.frames += 1
            self.last_ts = datetime.now()

    def get(self):
        with self._lock:
            d = {}
            for k, v in self.__dict__.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, collections.deque):
                    d[k] = list(v)
                elif isinstance(v, list):
                    d[k] = list(v)
                else:
                    d[k] = v
            return d


# ═══════════════════════════════════════════════════════════════════════════════
# SERIAL PARSER  — exact regex for your firmware's printf format
# ═══════════════════════════════════════════════════════════════════════════════
class FrameParser:
    # ── compiled patterns ────────────────────────────────────────────────────
    P_V_SUM  = re.compile(
        r"\[V\].*?Freq:([\d.]+)Hz\s+VRMS:([\d.]+)V")
    P_I_SUM  = re.compile(
        r"\[I\].*?Freq:([\d.]+)Hz\s+\|\s+IRMS:([\d.]+)A")
    P_HV     = re.compile(
        r"H(\d{2})\s+\(\s*\d+Hz\):\s+([\d.]+)V\s+\[bin")
    P_HI     = re.compile(
        r"H(\d{2})\s+\(\s*\d+Hz\):\s+([\d.]+)A\s+rms\s+\[bin")
    P_THD    = re.compile(r"THD:\s*([\d.]+)%")
    P_PWR    = re.compile(
        r"\[PWR\]\s+Active:\s*([-\d.]+)\s+W\s+\|\s+Apparent:\s*([\d.]+)\s+VA"
        r"\s+\|\s+PF:\s*([-\d.]+)\s+\|\s+Phase:\s*([\d.]+)\s+deg")
    P_SEP    = re.compile(r"^-{20,}\s*$")

    def __init__(self, live: LiveData):
        self._live = live
        self._reset()

    def _reset(self):
        self._f    = {}
        self._hv   = {}
        self._hi   = {}
        self._thds = []

    def feed(self, line: str):
        line = line.rstrip("\r\n")

        m = self.P_V_SUM.search(line)
        if m:
            self._f["freq_v"] = float(m.group(1))
            self._f["vrms"]   = float(m.group(2))
            return

        m = self.P_I_SUM.search(line)
        if m:
            self._f["freq_i"] = float(m.group(1))
            self._f["irms"]   = float(m.group(2))
            return

        m = self.P_HV.search(line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < NUM_HARMONICS:
                self._hv[idx] = float(m.group(2))
            return

        m = self.P_HI.search(line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < NUM_HARMONICS:
                self._hi[idx] = float(m.group(2))
            return

        m = self.P_THD.search(line)
        if m:
            self._thds.append(float(m.group(1)))
            return

        m = self.P_PWR.search(line)
        if m:
            self._f["active_power"]   = float(m.group(1))
            self._f["apparent_power"] = float(m.group(2))
            self._f["power_factor"]   = float(m.group(3))
            self._f["phase_angle"]    = float(m.group(4))
            return

        if self.P_SEP.match(line):
            # ── commit frame ──────────────────────────────────────────────
            if self._hv:
                self._f["harm_v"] = [self._hv.get(i, 0.0)
                                     for i in range(NUM_HARMONICS)]
            if self._hi:
                self._f["harm_i"] = [self._hi.get(i, 0.0)
                                     for i in range(NUM_HARMONICS)]
            if len(self._thds) >= 1:
                self._f["thd_v"] = self._thds[0]
            if len(self._thds) >= 2:
                self._f["thd_i"] = self._thds[1]

            if len(self._f) >= 4:        # need at least some real data
                self._live.push(self._f)

            self._reset()


# ═══════════════════════════════════════════════════════════════════════════════
# SERIAL READER THREAD
# ═══════════════════════════════════════════════════════════════════════════════
class SerialThread(threading.Thread):
    def __init__(self, port: str, live: LiveData):
        super().__init__(daemon=True)
        self.port   = port
        self.live   = live
        self.parser = FrameParser(live)
        self.alive  = True
        self.error  = None

    def run(self):
        try:
            with serial.Serial(self.port, BAUD_RATE, timeout=2.0) as ser:
                self.live.port      = self.port
                self.live.connected = True
                print(f"[serial] ✓  {self.port}  @  {BAUD_RATE} baud")
                while self.alive:
                    raw = ser.readline()
                    if raw:
                        try:
                            self.parser.feed(raw.decode("ascii", errors="replace"))
                        except Exception:
                            pass
        except serial.SerialException as e:
            self.error = str(e)
            self.live.connected = False
            print(f"[serial] ✗  {e}")

    def stop(self):
        self.alive = False


# ═══════════════════════════════════════════════════════════════════════════════
# PORT AUTO-DETECT
# ═══════════════════════════════════════════════════════════════════════════════
PREFER_KEYWORDS = ("STM", "NUCLEO", "STLINK", "USB", "CH340",
                   "CP210", "FTDI", "SILABS", "UART", "SERIAL")

def pick_port() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]

    ports = list(serial.tools.list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found. Connect your STM32 and retry.")

    scored = []
    for p in ports:
        score = sum(k in p.description.upper() for k in PREFER_KEYWORDS)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored[0][0] > 0 and len([s for s in scored if s[0] == scored[0][0]]) == 1:
        p = scored[0][1]
        print(f"[port]   Auto-selected  {p.device}  ({p.description})")
        return p.device

    print("\nAvailable serial ports:")
    for i, (_, p) in enumerate(scored):
        print(f"  [{i}]  {p.device:<16}  {p.description}")
    choice = input("\nSelect port [0]: ").strip()
    idx = int(choice) if choice.isdigit() else 0
    return scored[min(idx, len(scored)-1)][1].device


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════
class Dashboard:
    def __init__(self, live: LiveData):
        self.live = live
        self._build()

    # ──────────────────────────────────────────────────────────────────────────
    # Figure & axes construction
    # ──────────────────────────────────────────────────────────────────────────
    def _build(self):
        plt.rcParams.update({
            "font.family":       "monospace",
            "axes.facecolor":    C["panel"],
            "figure.facecolor":  C["bg"],
            "axes.edgecolor":    C["border"],
            "axes.labelcolor":   C["text_mid"],
            "xtick.color":       C["text_mid"],
            "ytick.color":       C["text_mid"],
            "grid.color":        C["grid"],
            "grid.linestyle":    "--",
            "grid.linewidth":    0.5,
            "text.color":        C["text_hi"],
        })

        self.fig = plt.figure(figsize=(20, 12), facecolor=C["bg"])
        self.fig.canvas.manager.set_window_title(
            "STM32 G474RE — CZT Power Analyser  |  Live Dashboard")

        # ── master grid 4 rows × 6 cols ───────────────────────────────────
        gs = gridspec.GridSpec(
            4, 6,
            figure=self.fig,
            left=0.04, right=0.98,
            top=0.91,  bottom=0.06,
            hspace=0.60, wspace=0.42,
        )

        # Row 0:  status | Vrms | Irms | P | S | Q
        self.ax_status = self.fig.add_subplot(gs[0, 0])
        self.ax_vrms   = self.fig.add_subplot(gs[0, 1])
        self.ax_irms   = self.fig.add_subplot(gs[0, 2])
        self.ax_pact   = self.fig.add_subplot(gs[0, 3])
        self.ax_papp   = self.fig.add_subplot(gs[0, 4])
        self.ax_preac  = self.fig.add_subplot(gs[0, 5])

        # Row 1:  Vrms trend (3 cols) | Irms trend (3 cols)
        self.ax_vtrend = self.fig.add_subplot(gs[1, 0:3])
        self.ax_itrend = self.fig.add_subplot(gs[1, 3:6])

        # Row 2:  Power trend (4 cols) | PF gauge (1 col) | PF card (1 col)
        self.ax_ptrend = self.fig.add_subplot(gs[2, 0:4])
        self.ax_gauge  = self.fig.add_subplot(gs[2, 4], polar=True)
        self.ax_pfcard = self.fig.add_subplot(gs[2, 5])

        # Row 3:  Voltage harmonics (3 cols) | Current harmonics (3 cols)
        self.ax_hv     = self.fig.add_subplot(gs[3, 0:3])
        self.ax_hi     = self.fig.add_subplot(gs[3, 3:6])

        self._style_axes()
        self._build_status()
        self._build_kpis()
        self._build_trends()
        self._build_harmonics()
        self._build_gauge()
        self._build_pfcard()
        self._build_title()

    # ──────────────────────────────────────────────────────────────────────────
    def _style_axes(self):
        for ax in self.fig.get_axes():
            ax.set_facecolor(C["panel"])
            for sp in ax.spines.values():
                sp.set_edgecolor(C["border"])
                sp.set_linewidth(0.8)

    # ──────────────────────────────────────────────────────────────────────────
    def _build_title(self):
        self.fig.text(
            0.5, 0.965,
            "◈  CZT HARMONIC POWER ANALYSER  ◈",
            ha="center", va="top",
            fontsize=15, fontweight="bold",
            color=C["glow"],
            fontfamily="monospace",
        )
        self.fig.text(
            0.5, 0.945,
            "STM32G474RE  ·  ZMPT101B + WCS1700  ·  fs=10 kHz  ·  N=1000  ·  fR=0.5 Hz",
            ha="center", va="top",
            fontsize=7.5,
            color=C["text_mid"],
            fontfamily="monospace",
        )
        # Decorative horizontal separator
        self.fig.add_artist(
            plt.Line2D([0.04, 0.96], [0.935, 0.935],
                       transform=self.fig.transFigure,
                       color=C["sep"], linewidth=0.7, linestyle="--")
        )

    # ──────────────────────────────────────────────────────────────────────────
    def _build_status(self):
        ax = self.ax_status
        ax.axis("off")
        # Phosphor-green border
        rect = FancyBboxPatch(
            (0.04, 0.04), 0.92, 0.92,
            boxstyle="round,pad=0.015",
            linewidth=1.2, edgecolor=C["glow"],
            facecolor=C["panel2"], alpha=0.6,
            transform=ax.transAxes, clip_on=False)
        ax.add_patch(rect)

        kw = dict(transform=ax.transAxes, fontfamily="monospace", fontsize=7.5)
        ax.text(0.08, 0.93, "◉ SYSTEM STATUS", color=C["glow"],
                fontsize=8, fontweight="bold", **{k:v for k,v in kw.items()
                if k != "fontsize"}, va="top")

        self._st_conn   = ax.text(0.08, 0.78, "PORT  : —",
                                   color=C["text_mid"], va="top", **kw)
        self._st_baud   = ax.text(0.08, 0.64, f"BAUD  : {BAUD_RATE}",
                                   color=C["text_lo"], va="top", **kw)
        self._st_frames = ax.text(0.08, 0.50, "FRAMES: 0",
                                   color=C["text_mid"], va="top", **kw)
        self._st_time   = ax.text(0.08, 0.36, "TIME  : —",
                                   color=C["text_lo"], va="top", **kw)
        self._st_thd_v  = ax.text(0.08, 0.22, "THD_V : —%",
                                   color=C["ok"], va="top", **kw)
        self._st_thd_i  = ax.text(0.08, 0.08, "THD_I : —%",
                                   color=C["ok"], va="top", **kw)

    # ──────────────────────────────────────────────────────────────────────────
    def _kpi_ax(self, ax, label: str, unit: str, color: str):
        ax.axis("off")
        rect = FancyBboxPatch(
            (0.04, 0.04), 0.92, 0.92,
            boxstyle="round,pad=0.015",
            linewidth=1.2, edgecolor=color,
            facecolor=C["panel2"], alpha=0.5,
            transform=ax.transAxes, clip_on=False)
        ax.add_patch(rect)
        ax.text(0.5, 0.90, label, ha="center", va="top",
                color=color, fontsize=7.5, fontweight="bold",
                fontfamily="monospace", transform=ax.transAxes)
        val = ax.text(0.5, 0.52, "——", ha="center", va="center",
                      color=color, fontsize=22, fontweight="bold",
                      fontfamily="monospace", transform=ax.transAxes)
        ax.text(0.5, 0.10, unit, ha="center", va="bottom",
                color=C["text_lo"], fontsize=8,
                fontfamily="monospace", transform=ax.transAxes)
        return val

    def _build_kpis(self):
        self._kv_vrms  = self._kpi_ax(self.ax_vrms,  "VRMS",       "V",   C["volt"])
        self._kv_irms  = self._kpi_ax(self.ax_irms,  "IRMS",       "A",   C["curr"])
        self._kv_pact  = self._kpi_ax(self.ax_pact,  "ACTIVE PWR", "W",   C["pwr"])
        self._kv_papp  = self._kpi_ax(self.ax_papp,  "APPARENT",   "VA",  C["text_mid"])
        self._kv_preac = self._kpi_ax(self.ax_preac, "REACTIVE",   "VAR", C["pf"])

    # ──────────────────────────────────────────────────────────────────────────
    def _style_trend(self, ax, ylabel, color):
        ax.set_facecolor(C["panel"])
        ax.set_ylabel(ylabel, fontsize=7.5, color=color, labelpad=3)
        ax.tick_params(labelsize=6.5)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, prune="both"))
        ax.grid(True)
        ax.set_xlabel("seconds ago", fontsize=6, color=C["text_lo"])

    def _build_trends(self):
        self._style_trend(self.ax_vtrend, "VRMS (V)",       C["volt"])
        self._style_trend(self.ax_itrend, "IRMS (A)",       C["curr"])
        self._style_trend(self.ax_ptrend, "Active Power (W)", C["pwr"])

        self.ax_vtrend.set_title("Voltage RMS Trend", fontsize=8,
                                  color=C["volt"], pad=3)
        self.ax_itrend.set_title("Current RMS Trend", fontsize=8,
                                  color=C["curr"], pad=3)
        self.ax_ptrend.set_title("Active Power Trend", fontsize=8,
                                  color=C["pwr"], pad=3)

        lw = 1.5
        self._ln_v, = self.ax_vtrend.plot([], [], color=C["volt"], lw=lw)
        self._ln_i, = self.ax_itrend.plot([], [], color=C["curr"], lw=lw)
        self._ln_p, = self.ax_ptrend.plot([], [], color=C["pwr"],  lw=lw)
        self._fill_v = self._fill_i = self._fill_p = None

    # ──────────────────────────────────────────────────────────────────────────
    def _build_harmonics(self):
        x = np.arange(NUM_HARMONICS)
        labels = [f"H{i+1}" for i in range(NUM_HARMONICS)]

        for ax, title, col in [
            (self.ax_hv, "VOLTAGE HARMONICS  (V peak)", C["volt"]),
            (self.ax_hi, "CURRENT HARMONICS  (A rms)",  C["curr"]),
        ]:
            ax.set_title(title, fontsize=8, color=col, pad=3)
            ax.set_xticks(x)
            ax.set_xticklabels(labels, fontsize=7, color=C["text_mid"])
            ax.tick_params(labelsize=7)
            ax.grid(axis="y")
            ax.set_xlim(-0.6, NUM_HARMONICS - 0.4)

        self._bars_v = self.ax_hv.bar(
            x, [0]*NUM_HARMONICS,
            color=HARM_PALETTE, width=0.65,
            edgecolor=C["bg"], linewidth=0.5, alpha=0.85)

        self._bars_i = self.ax_hi.bar(
            x, [0]*NUM_HARMONICS,
            color=HARM_PALETTE, width=0.65,
            edgecolor=C["bg"], linewidth=0.5, alpha=0.85)

        # H1 label annotations (updated live)
        self._hv_anns = [
            self.ax_hv.text(i, 0, "", ha="center", va="bottom",
                            fontsize=6, color=C["text_hi"], fontfamily="monospace")
            for i in range(NUM_HARMONICS)
        ]
        self._hi_anns = [
            self.ax_hi.text(i, 0, "", ha="center", va="bottom",
                            fontsize=6, color=C["text_hi"], fontfamily="monospace")
            for i in range(NUM_HARMONICS)
        ]

    # ──────────────────────────────────────────────────────────────────────────
    def _build_gauge(self):
        ax = self.ax_gauge
        ax.set_facecolor(C["panel"])
        ax.set_thetamin(0)
        ax.set_thetamax(180)
        ax.set_ylim(0, 1.0)
        ax.set_yticks([])
        # Custom tick labels: -1 .. +1
        ticks = np.linspace(0, np.pi, 9)
        ax.set_xticks(ticks)
        ax.set_xticklabels(
            [f"{v:.2f}" for v in np.linspace(-1, 1, 9)[::-1]],
            fontsize=5.5, color=C["text_mid"])
        ax.tick_params(pad=1)
        ax.grid(color=C["grid"], linewidth=0.4)
        ax.set_title("POWER FACTOR", fontsize=7.5, color=C["pf"], pad=4)

        # Coloured arcs: green zone (PF>0.9), yellow, red
        for (t1, t2, col) in [
            (0,          np.pi*0.05, C["warn"]),
            (np.pi*0.05, np.pi*0.15, C["pwr"]),
            (np.pi*0.15, np.pi*0.85, C["ok"]),
            (np.pi*0.85, np.pi*0.95, C["pwr"]),
            (np.pi*0.95, np.pi,      C["warn"]),
        ]:
            th = np.linspace(t1, t2, 40)
            ax.fill_between(th, 0.88, 0.95,
                            color=col, alpha=0.35, zorder=2)

        self._needle,  = ax.plot([], [], color=C["pf"],
                                  lw=2.8, zorder=5,
                                  solid_capstyle="round")
        self._ndot,    = ax.plot([], [], "o", color=C["pf"],
                                  markersize=7, zorder=6)

    # ──────────────────────────────────────────────────────────────────────────
    def _build_pfcard(self):
        ax = self.ax_pfcard
        ax.axis("off")
        rect = FancyBboxPatch(
            (0.04, 0.04), 0.92, 0.92,
            boxstyle="round,pad=0.015",
            linewidth=1.2, edgecolor=C["pf"],
            facecolor=C["panel2"], alpha=0.5,
            transform=ax.transAxes, clip_on=False)
        ax.add_patch(rect)

        kw = dict(transform=ax.transAxes, fontfamily="monospace",
                  ha="center", va="center")
        ax.text(0.5, 0.90, "PF  /  PHASE", color=C["pf"],
                fontsize=8, fontweight="bold", va="top", **{k:v for k,v in kw.items()
                if k not in ("va",)})

        self._pf_big   = ax.text(0.5, 0.68, "—", color=C["pf"],
                                  fontsize=22, fontweight="bold", **kw)
        self._ph_big   = ax.text(0.5, 0.44, "φ = —°", color=C["text_mid"],
                                  fontsize=13, **kw)
        self._freq_big = ax.text(0.5, 0.26, "f = —Hz", color=C["text_lo"],
                                  fontsize=9, **kw)
        self._app_big  = ax.text(0.5, 0.10, "S = — VA", color=C["text_lo"],
                                  fontsize=8, **kw)

    # ══════════════════════════════════════════════════════════════════════════
    # ANIMATION UPDATE
    # ══════════════════════════════════════════════════════════════════════════
    def _update(self, _frame):
        d = self.live.get()

        # ── Status panel ─────────────────────────────────────────────────────
        col_conn = C["glow"] if d["connected"] else C["warn"]
        self._st_conn.set_text(f"PORT  : {d['port']}")
        self._st_conn.set_color(col_conn)
        self._st_frames.set_text(f"FRAMES: {d['frames']}")
        if d["last_ts"]:
            self._st_time.set_text("TIME  : " + d["last_ts"].strftime("%H:%M:%S"))

        thd_v_col = C["warn"] if d["thd_v"] > 5 else C["ok"]
        thd_i_col = C["warn"] if d["thd_i"] > 5 else C["ok"]
        self._st_thd_v.set_text(f"THD_V : {d['thd_v']:.2f}%")
        self._st_thd_i.set_text(f"THD_I : {d['thd_i']:.2f}%")
        self._st_thd_v.set_color(thd_v_col)
        self._st_thd_i.set_color(thd_i_col)

        # ── KPI cards ─────────────────────────────────────────────────────────
        self._kv_vrms.set_text( f"{d['vrms']:.2f}")
        self._kv_irms.set_text( f"{d['irms']:.4f}")
        self._kv_pact.set_text( f"{d['active_power']:.1f}")
        self._kv_papp.set_text( f"{d['apparent_power']:.1f}")
        self._kv_preac.set_text(f"{d['reactive_power']:.1f}")

        # ── Trends ────────────────────────────────────────────────────────────
        ts = np.array(d["hist_t"])
        if len(ts) > 1:
            t_rel = ts - ts[-1]    # negative: seconds ago

            for ln, fill_attr, ax, hist, col in [
                (self._ln_v, "_fill_v", self.ax_vtrend, d["hist_vrms"],  C["volt"]),
                (self._ln_i, "_fill_i", self.ax_itrend, d["hist_irms"],  C["curr"]),
                (self._ln_p, "_fill_p", self.ax_ptrend, d["hist_pwr"],   C["pwr"]),
            ]:
                ya = np.array(hist)
                ln.set_data(t_rel, ya)
                ax.set_xlim(t_rel[0], 0.5)
                if ya.ptp() > 1e-9:
                    pad = ya.ptp() * 0.2
                    ax.set_ylim(ya.min() - pad, ya.max() + pad)
                # Refresh fill
                old = getattr(self, fill_attr)
                if old:
                    try: old.remove()
                    except: pass
                setattr(self, fill_attr,
                        ax.fill_between(t_rel, ya, alpha=0.10, color=col))

        # ── Harmonic bars ─────────────────────────────────────────────────────
        for bars, anns, vals, ax in [
            (self._bars_v, self._hv_anns, d["harm_v"], self.ax_hv),
            (self._bars_i, self._hi_anns, d["harm_i"], self.ax_hi),
        ]:
            top = max(max(vals), 1e-6)
            ax.set_ylim(0, top * 1.22)
            for bar, ann, val in zip(bars, anns, vals):
                bar.set_height(val)
                ann.set_y(val + top * 0.02)
                ann.set_text(f"{val:.3f}" if val > 0.001 else "")

        # ── PF gauge needle ───────────────────────────────────────────────────
        pf = float(np.clip(d["power_factor"], -1.0, 1.0))
        # PF +1  →  angle=0,  PF -1  →  angle=π
        angle = np.arccos(pf)
        self._needle.set_data([angle, angle], [0, 0.82])
        self._ndot.set_data([angle], [0.82])

        # ── PF card ───────────────────────────────────────────────────────────
        self._pf_big.set_text(f"{pf:+.3f}")
        self._ph_big.set_text(f"φ = {d['phase_angle']:.1f}°")
        self._freq_big.set_text(f"f = {d['freq_v']:.2f} Hz")
        self._app_big.set_text(f"S = {d['apparent_power']:.1f} VA")

        return []

    # ──────────────────────────────────────────────────────────────────────────
    def run(self):
        self._ani = animation.FuncAnimation(
            self.fig,
            self._update,
            interval=REFRESH_MS,
            blit=False,
            cache_frame_data=False,
        )
        plt.tight_layout(rect=[0, 0, 1, 0.935])
        plt.show()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    port = pick_port()

    live   = LiveData()
    thread = SerialThread(port, live)
    thread.start()

    time.sleep(0.4)   # let serial open cleanly
    if thread.error:
        print(f"\n[ERROR] Failed to open port: {thread.error}")
        sys.exit(1)

    dash = Dashboard(live)
    try:
        dash.run()
    except KeyboardInterrupt:
        print("\n[dashboard] Interrupted by user.")
    finally:
        thread.stop()
        print("[dashboard] Closed.")


if __name__ == "__main__":
    main()