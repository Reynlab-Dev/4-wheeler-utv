#!/usr/bin/env python3
"""
UTV EV Platform — Live Telemetry Dashboard (Native PyQt5 App)
Reads JSON from Arduino Mega over USB Serial.

Setup (Ubuntu):
    pip3 install PyQt5 pyqtgraph pyserial numpy

Run:
    python3 dashboard.py --port /dev/ttyACM0

Fullscreen:
    python3 dashboard.py --port /dev/ttyACM0 --fullscreen
"""

import sys
import argparse
import json
import threading
import time
import serial

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QFrame, QGridLayout
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

import pyqtgraph as pg
import numpy as np

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------
SERIAL_PORT    = "/dev/ttyACM0"
SERIAL_BAUD    = 115200
UPDATE_MS      = 500
RATED_RANGE_KM = 70
CELL_COUNT     = 19

ACS_LABELS  = ["Reverse", "Brake\nLight", "Head\nLight", "Hazard", "Turn\nSig", "Horn"]
TEMP_LABELS = ["NTC 1", "NTC 2", "NTC 3", "NTC 4"]

# ---------------------------------------------------------------------------
#  Colours
# ---------------------------------------------------------------------------
BG     = "#0d1117"
CARD   = "#161b22"
BORDER = "#30363d"
GREEN  = "#3fb950"
YELLOW = "#d29922"
RED    = "#f85149"
BLUE   = "#58a6ff"
WHITE  = "#ffffff"
GREY   = "#8b949e"
TRACK  = "#21262d"

def hex_to_pg(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

# ---------------------------------------------------------------------------
#  Shared state — serial thread writes, Qt timer reads
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_data = {
    "pv": 0.0, "pa": 0.0, "soc": 0.0, "pw": 0.0,
    "t0": -99, "t1": -99, "t2": -99, "t3": -99,
    "cmin": 0.0, "cmax": 0.0, "cmni": 0, "cmxi": 0,
    "cyc": 0, "mos": 0, "flt": 0, "vld": 0,
    "cv": [0.0] * CELL_COUNT,
    "ac": [0.0] * 6,
    "connected": False,
    "last_rx": 0.0,
}

def get_data():
    with _lock:
        return dict(_data)

def serial_reader(port: str, baud: int):
    global _data
    while True:
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                print(f"[serial] Connected to {port}")
                with _lock:
                    _data["connected"] = True
                while True:
                    raw = ser.readline().decode("ascii", errors="ignore").strip()
                    if not raw.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(raw)
                        with _lock:
                            _data.update(parsed)
                            _data["connected"] = True
                            _data["last_rx"]   = time.time()
                    except json.JSONDecodeError:
                        pass
        except serial.SerialException as e:
            print(f"[serial] {e} — retrying in 3 s")
            with _lock:
                _data["connected"] = False
            time.sleep(3)

# ---------------------------------------------------------------------------
#  Metric Card widget
# ---------------------------------------------------------------------------
CARD_STYLE = f"""
    QFrame#MetricCard {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 8px;
    }}
    QFrame#MetricCard QLabel {{
        border: none;
        background: transparent;
    }}
    QFrame#MetricCard QProgressBar {{
        border: none;
        background: {TRACK};
        border-radius: 3px;
    }}
    QFrame#MetricCard QProgressBar::chunk {{
        border-radius: 3px;
    }}
"""

class MetricCard(QFrame):
    def __init__(self, title, unit="", vmin=0.0, vmax=100.0, color=BLUE):
        super().__init__()
        self.setObjectName("MetricCard")
        self.vmin  = float(vmin)
        self.vmax  = float(vmax)
        self.color = color
        self.unit  = unit
        self.setStyleSheet(CARD_STYLE)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(),
            self.sizePolicy().verticalPolicy()
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)

        self.title_lbl = QLabel(title.upper())
        self.title_lbl.setAlignment(Qt.AlignCenter)
        self.title_lbl.setStyleSheet(f"color:{GREY};font-size:10px;letter-spacing:1px;")
        lay.addWidget(self.title_lbl)

        self.val_lbl = QLabel("—")
        self.val_lbl.setAlignment(Qt.AlignCenter)
        self.val_lbl.setFont(QFont("monospace", 28, QFont.Bold))
        self.val_lbl.setStyleSheet(f"color:{WHITE};")
        lay.addWidget(self.val_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar)

        rl = QHBoxLayout()
        rl.setContentsMargins(0, 0, 0, 0)
        self.min_lbl = QLabel(self._fmt(vmin))
        self.max_lbl = QLabel(self._fmt(vmax))
        for lb in (self.min_lbl, self.max_lbl):
            lb.setStyleSheet(f"color:{GREY};font-size:9px;")
        self.min_lbl.setAlignment(Qt.AlignLeft)
        self.max_lbl.setAlignment(Qt.AlignRight)
        rl.addWidget(self.min_lbl)
        rl.addWidget(self.max_lbl)
        lay.addLayout(rl)

    def _fmt(self, v):
        return str(int(v)) if v == int(v) else f"{v:.1f}"

    def update_value(self, value, text=None, color=None):
        c = color or self.color
        span = self.vmax - self.vmin
        f = max(0.0, min(1.0, (value - self.vmin) / span)) if span else 0.0
        self.val_lbl.setText(text or f"{value:.1f} {self.unit}")
        self.val_lbl.setStyleSheet(f"color:{WHITE};")
        self.bar.setValue(int(f * 1000))
        self.bar.setStyleSheet(
            f"QProgressBar{{background:{TRACK};border-radius:3px;border:none;}}"
            f"QProgressBar::chunk{{background:{c};border-radius:3px;}}"
        )

# ---------------------------------------------------------------------------
#  Bar chart widget (pyqtgraph)
# ---------------------------------------------------------------------------
class BarChart(QWidget):
    def __init__(self, labels, yrange=(0, 15), ylabel=""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background=CARD)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=False, y=True, alpha=0.2)
        self.plot.getAxis("left").setTextPen(GREY)
        self.plot.getAxis("bottom").setTextPen(GREY)
        self.plot.getAxis("left").setPen(BORDER)
        self.plot.getAxis("bottom").setPen(BORDER)
        self.plot.setYRange(*yrange, padding=0.1)
        self.plot.setLabel("left", ylabel, color=GREY)

        n = len(labels)
        ticks = [[(i, labels[i]) for i in range(n)]]
        self.plot.getAxis("bottom").setTicks(ticks)
        self.plot.getAxis("bottom").setStyle(tickFont=QFont("monospace", 8))

        xs = np.arange(n, dtype=float)
        self.bars = pg.BarGraphItem(
            x=xs, height=np.zeros(n), width=0.6,
            brushes=[hex_to_pg(BLUE)] * n
        )
        self.plot.addItem(self.bars)
        lay.addWidget(self.plot)

    def update_bars(self, heights, colors):
        brushes = [pg.mkBrush(*hex_to_pg(c)) for c in colors]
        self.bars.setOpts(height=np.array(heights, dtype=float), brushes=brushes)

# ---------------------------------------------------------------------------
#  BMS Status panel
# ---------------------------------------------------------------------------
class BMSStatus(QFrame):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(f"""
            QFrame {{ background:{CARD}; border:1px solid {BORDER}; border-radius:8px; }}
            QLabel {{ border:none; background:transparent; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        hdr = QLabel("BMS STATUS")
        hdr.setStyleSheet(f"color:{WHITE};font-size:10px;letter-spacing:1px;font-weight:bold;")
        lay.addWidget(hdr)

        self.rows = {}
        for key in ("Pack V", "Cell Min", "Cell Max", "Cycles", "Charge MOS", "Faults", "BMS Data"):
            row = QHBoxLayout()
            lbl = QLabel(key)
            lbl.setStyleSheet(f"color:{GREY};font-size:11px;")
            lbl.setFixedWidth(90)
            val = QLabel("—")
            val.setStyleSheet(f"color:{WHITE};font-size:13px;font-weight:bold;")
            row.addWidget(lbl)
            row.addWidget(val)
            self.rows[key] = val
            lay.addLayout(row)

        lay.addStretch()

    def set_row(self, key, text, color=WHITE):
        self.rows[key].setText(text)
        self.rows[key].setStyleSheet(f"color:{color};font-size:13px;font-weight:bold;")

# ---------------------------------------------------------------------------
#  Main window
# ---------------------------------------------------------------------------
class Dashboard(QMainWindow):
    def __init__(self, port, fullscreen=False):
        super().__init__()
        self.setWindowTitle("UTV EV Platform — Live Telemetry")
        self.setStyleSheet(f"QMainWindow {{ background:{BG}; }} QWidget {{ background:{BG}; }}")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---- Header ----
        header = QFrame()
        header.setStyleSheet(f"QFrame{{background:{CARD};border:1px solid {BORDER};border-radius:8px;}} QLabel{{border:none;background:transparent;}}")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 6, 12, 6)
        title = QLabel("UTV EV Platform")
        title.setFont(QFont("monospace", 14, QFont.Bold))
        title.setStyleSheet(f"color:{WHITE};")
        sub = QLabel(" — Live Telemetry")
        sub.setStyleSheet(f"color:{GREY};font-size:13px;")
        self.badge = QLabel("● NO SIGNAL")
        self.badge.setStyleSheet(f"background:{RED};color:{WHITE};padding:2px 10px;border-radius:10px;font-size:11px;")
        hl.addWidget(title)
        hl.addWidget(sub)
        hl.addStretch()
        hl.addWidget(self.badge)
        root.addWidget(header)

        # ---- Fault banner ----
        self.fault_banner = QLabel("⚠  BMS FAULT DETECTED")
        self.fault_banner.setAlignment(Qt.AlignCenter)
        self.fault_banner.setStyleSheet(f"background:{RED};color:{WHITE};font-weight:bold;font-size:14px;border-radius:6px;padding:6px;")
        self.fault_banner.hide()
        root.addWidget(self.fault_banner)

        # ---- Row 1: Metric cards ----
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        self.card_soc   = MetricCard("State of Charge", "%",   0,   100, GREEN)
        self.card_pv    = MetricCard("Pack Voltage",    "V",  40,    75, BLUE)
        self.card_pa    = MetricCard("Pack Current",    "A",   0,     6, BLUE)
        self.card_pw    = MetricCard("Power",           "kW",  0,     5, GREEN)
        self.card_range = MetricCard("Est. Range",      "km",  0,    70, GREEN)
        for c in (self.card_soc, self.card_pv, self.card_pa, self.card_pw, self.card_range):
            r1.addWidget(c)
        root.addLayout(r1)

        # ---- Row 2: Cell voltages + Temps ----
        r2 = QHBoxLayout()
        r2.setSpacing(6)

        cell_frame = QFrame()
        cell_frame.setStyleSheet(f"QFrame{{background:{CARD};border:1px solid {BORDER};border-radius:8px;}} QLabel{{border:none;background:transparent;}}")
        cf_lay = QVBoxLayout(cell_frame)
        cf_lay.setContentsMargins(8, 6, 8, 6)
        cf_hdr = QLabel("CELL VOLTAGES (19 CELLS)")
        cf_hdr.setStyleSheet(f"color:{WHITE};font-size:10px;letter-spacing:1px;")
        cf_lay.addWidget(cf_hdr)
        self.cell_chart = BarChart(
            [f"C{i+1}" for i in range(CELL_COUNT)],
            yrange=(3.0, 3.8), ylabel="V"
        )
        cf_lay.addWidget(self.cell_chart)

        temp_frame = QFrame()
        temp_frame.setStyleSheet(f"QFrame{{background:{CARD};border:1px solid {BORDER};border-radius:8px;}} QLabel{{border:none;background:transparent;}}")
        tf_lay = QVBoxLayout(temp_frame)
        tf_lay.setContentsMargins(8, 6, 8, 6)
        tf_hdr = QLabel("TEMPERATURES")
        tf_hdr.setStyleSheet(f"color:{WHITE};font-size:10px;letter-spacing:1px;")
        tf_lay.addWidget(tf_hdr)
        self.temp_chart = BarChart(TEMP_LABELS, yrange=(0, 60), ylabel="°C")
        tf_lay.addWidget(self.temp_chart)

        r2.addWidget(cell_frame, 3)
        r2.addWidget(temp_frame, 1)
        root.addLayout(r2)

        # ---- Row 3: ACS + BMS status ----
        r3 = QHBoxLayout()
        r3.setSpacing(6)

        acs_frame = QFrame()
        acs_frame.setStyleSheet(f"QFrame{{background:{CARD};border:1px solid {BORDER};border-radius:8px;}} QLabel{{border:none;background:transparent;}}")
        af_lay = QVBoxLayout(acs_frame)
        af_lay.setContentsMargins(8, 6, 8, 6)
        af_hdr = QLabel("CIRCUIT CURRENTS (ACS712)")
        af_hdr.setStyleSheet(f"color:{WHITE};font-size:10px;letter-spacing:1px;")
        af_lay.addWidget(af_hdr)
        self.acs_chart = BarChart(ACS_LABELS, yrange=(0, 15), ylabel="A")
        af_lay.addWidget(self.acs_chart)

        self.bms_status = BMSStatus()

        r3.addWidget(acs_frame, 3)
        r3.addWidget(self.bms_status, 1)
        root.addLayout(r3)

        # ---- Timer ----
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(UPDATE_MS)

        self.resize(1280, 720)
        if fullscreen:
            self.showFullScreen()
        else:
            self.show()

    def refresh(self):
        d = get_data()

        # Badge
        stale     = (time.time() - d["last_rx"]) > 3.0
        connected = d["connected"] and not stale
        if connected:
            self.badge.setText("● LIVE")
            self.badge.setStyleSheet(f"background:{GREEN};color:#000;padding:2px 10px;border-radius:10px;font-size:11px;font-weight:bold;")
        else:
            self.badge.setText("● NO SIGNAL")
            self.badge.setStyleSheet(f"background:{RED};color:{WHITE};padding:2px 10px;border-radius:10px;font-size:11px;")

        # Fault banner
        self.fault_banner.setVisible(bool(d["flt"]))

        # Metric cards
        soc_c = GREEN if d["soc"] > 20 else (YELLOW if d["soc"] > 10 else RED)
        pa_c  = RED if abs(d["pa"]) > 5.5 else YELLOW if abs(d["pa"]) > 4 else BLUE

        self.card_soc.update_value(d["soc"],  f"{d['soc']:.0f} %",        soc_c)
        self.card_pv.update_value(d["pv"],    f"{d['pv']:.1f} V",         BLUE)
        self.card_pa.update_value(d["pa"],    f"{d['pa']:.1f} A",          pa_c)
        self.card_pw.update_value(d["pw"] / 1000, f"{d['pw']/1000:.2f} kW", GREEN)
        rng = d["soc"] / 100.0 * RATED_RANGE_KM
        self.card_range.update_value(rng,     f"{rng:.1f} km",             GREEN)

        # Cell voltages
        cv = d["cv"]
        cell_colors = []
        for v in cv:
            if v < 3.0 or v > 3.65: cell_colors.append(RED)
            elif v < 3.2:            cell_colors.append(YELLOW)
            else:                    cell_colors.append(GREEN)
        self.cell_chart.update_bars(cv, cell_colors)

        # Temperatures
        tv = [d[k] if d[k] != -99 else 0 for k in ("t0","t1","t2","t3")]
        tc = [RED if v > 45 else YELLOW if v > 35 else BLUE
              for v in [d[k] for k in ("t0","t1","t2","t3")]]
        self.temp_chart.update_bars(tv, tc)

        # ACS currents
        ac = d["ac"]
        ac_c = [RED if v >= 10 else YELLOW if v >= 5 else GREEN for v in ac]
        self.acs_chart.update_bars(ac, ac_c)

        # BMS status
        self.bms_status.set_row("Pack V",     f"{d['pv']:.1f} V")
        cmin_c = RED if d["cmin"] < 3.0 else YELLOW if d["cmin"] < 3.2 else GREEN
        cmax_c = RED if d["cmax"] > 3.65 else GREEN
        self.bms_status.set_row("Cell Min",   f"{d['cmin']:.3f} V [#{d['cmni']}]", cmin_c)
        self.bms_status.set_row("Cell Max",   f"{d['cmax']:.3f} V [#{d['cmxi']}]", cmax_c)
        self.bms_status.set_row("Cycles",     str(d["cyc"]))
        self.bms_status.set_row("Charge MOS", "ON" if d["mos"] else "OFF",
                                GREEN if d["mos"] else YELLOW)
        self.bms_status.set_row("Faults",     "NONE" if not d["flt"] else "ACTIVE",
                                GREEN if not d["flt"] else RED)
        self.bms_status.set_row("BMS Data",   "Valid" if d["vld"] else "Waiting",
                                GREEN if d["vld"] else YELLOW)

# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",       default=SERIAL_PORT)
    parser.add_argument("--baud",       default=SERIAL_BAUD, type=int)
    parser.add_argument("--fullscreen", action="store_true")
    args = parser.parse_args()

    t = threading.Thread(target=serial_reader, args=(args.port, args.baud), daemon=True)
    t.start()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = Dashboard(args.port, fullscreen=args.fullscreen)
    sys.exit(app.exec_())
