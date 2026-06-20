#!/usr/bin/env python3
"""
UTV EV Platform — Live Telemetry Dashboard
Reads JSON frames from Arduino Mega over USB Serial and renders on 19" display.

Setup (Ubuntu):
    pip3 install dash plotly pyserial

Find your Arduino port:
    ls /dev/ttyACM* /dev/ttyUSB*

Run:
    python3 dashboard.py --port /dev/ttyACM0

Open in Chromium fullscreen (19" monitor):
    chromium-browser --kiosk http://localhost:8050
"""

import argparse
import json
import math
import threading
import time
import serial
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------
SERIAL_PORT    = "/dev/ttyACM0"
SERIAL_BAUD    = 115200
UPDATE_MS      = 500          # 2 Hz — lighter on 4GB Ubuntu
RATED_RANGE_KM = 70

ACS_LABELS = ["Reverse", "Brake Light", "Headlight", "Hazard", "Turn Signal", "Horn"]
CELL_COUNT = 19

# ---------------------------------------------------------------------------
#  Shared state — written by serial thread, read by Dash callbacks
# ---------------------------------------------------------------------------
_lock  = threading.Lock()
_data  = {
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

# ---------------------------------------------------------------------------
#  Serial reader thread
# ---------------------------------------------------------------------------
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
#  Dash app
# ---------------------------------------------------------------------------
app = dash.Dash(__name__, title="UTV EV Telemetry")
app.config.suppress_callback_exceptions = True

# ---- Colour palette ----
BG      = "#0d1117"
CARD    = "#161b22"
BORDER  = "#30363d"
GREEN   = "#3fb950"
YELLOW  = "#d29922"
RED     = "#f85149"
BLUE    = "#58a6ff"
WHITE   = "#e6edf3"
GREY    = "#8b949e"
TRACK   = "#21262d"

def card(children, style=None):
    base = {
        "background": CARD,
        "border": f"1px solid {BORDER}",
        "borderRadius": "8px",
        "padding": "12px",
    }
    if style:
        base.update(style)
    return html.Div(children, style=base)

def label(text):
    return html.Div(text, style={
        "color": WHITE, "fontSize": "11px",
        "textTransform": "uppercase", "letterSpacing": "1px",
        "marginBottom": "4px",
    })

# ---------------------------------------------------------------------------
#  SVG Arc Gauge — native Dash SVG components, always renders
# ---------------------------------------------------------------------------
def make_svg_gauge(value, vmin, vmax, disp_str, color):
    """Semicircle arc gauge using html.Svg — no Plotly, no raw HTML injection."""
    cx, cy, r, sw = 100, 80, 66, 13

    value = max(vmin, min(vmax, float(value)))
    span  = float(vmax - vmin)
    f     = (value - vmin) / span if span != 0 else 0.0
    f     = max(0.0, min(1.0, f))

    lx, ly = cx - r, cy
    rx, ry = cx + r, cy

    track_d = f"M {lx} {ly} A {r} {r} 0 0 1 {rx} {ry}"

    def fmt_lim(v):
        return str(int(v)) if v == int(v) else f"{v:.1f}"

    elements = [
        html.Path(d=track_d, fill="none", stroke=TRACK,
                  **{"stroke-width": str(sw), "stroke-linecap": "round"}),
    ]

    if f > 0.005:
        if f >= 0.995:
            vx, vy = rx - 0.1, float(ry)
        else:
            a  = math.radians(180.0 * (1.0 - f))
            vx = cx + r * math.cos(a)
            vy = cy - r * math.sin(a)
        val_d = f"M {lx} {ly} A {r} {r} 0 0 1 {vx:.2f} {vy:.2f}"
        elements.append(
            html.Path(d=val_d, fill="none", stroke=color,
                      **{"stroke-width": str(sw), "stroke-linecap": "round"})
        )

    elements += [
        html.Text(disp_str,
                  x=str(cx), y=str(cy + 26), fill="#ffffff",
                  **{"text-anchor": "middle", "font-size": "24",
                     "font-weight": "bold", "font-family": "monospace"}),
        html.Text(fmt_lim(vmin),
                  x=str(lx + 2), y=str(cy + 16), fill=GREY,
                  **{"text-anchor": "start", "font-size": "9",
                     "font-family": "monospace"}),
        html.Text(fmt_lim(vmax),
                  x=str(rx - 2), y=str(cy + 16), fill=GREY,
                  **{"text-anchor": "end", "font-size": "9",
                     "font-family": "monospace"}),
    ]

    return html.Svg(elements,
                    viewBox="0 0 200 115",
                    style={"width": "100%", "height": "145px", "display": "block"})

# ---------------------------------------------------------------------------
#  Layout
# ---------------------------------------------------------------------------
app.layout = html.Div([
    dcc.Interval(id="tick", interval=UPDATE_MS, n_intervals=0),

    # ---- Header ----
    html.Div([
        html.Span("UTV EV Platform", style={
            "fontSize": "20px", "fontWeight": "bold", "color": WHITE,
        }),
        html.Span(" — Live Telemetry", style={"color": GREY, "fontSize": "16px"}),
        html.Span(id="status-badge", style={
            "float": "right", "fontSize": "12px",
            "padding": "3px 10px", "borderRadius": "12px",
        }),
    ], style={
        "background": CARD, "border": f"1px solid {BORDER}",
        "borderRadius": "8px", "padding": "10px 16px", "marginBottom": "10px",
    }),

    # ---- Fault banner ----
    html.Div(id="fault-banner"),

    # ---- Row 1: SVG Arc Gauges ----
    html.Div([
        card([label("State of Charge"), html.Div(id="gauge-soc")], style={"flex": "1"}),
        card([label("Pack Voltage"),    html.Div(id="gauge-pv")],  style={"flex": "1"}),
        card([label("Pack Current"),    html.Div(id="gauge-pa")],  style={"flex": "1"}),
        card([label("Power"),           html.Div(id="gauge-pw")],  style={"flex": "1"}),
        card([
            label("Est. Range"),
            html.Div(id="range-val", style={
                "fontSize": "52px", "fontWeight": "bold", "color": GREEN,
                "textAlign": "center", "paddingTop": "28px",
            }),
        ], style={"flex": "0.7"}),
    ], style={"display": "flex", "gap": "8px", "marginBottom": "8px"}),

    # ---- Row 2: Cell voltages + Temperatures ----
    html.Div([
        card([
            label("Cell Voltages (19 cells)"),
            dcc.Graph(id="chart-cells", style={"height": "180px"},
                      config={"displayModeBar": False}),
        ], style={"flex": "3"}),
        card([
            label("Temperatures"),
            dcc.Graph(id="chart-temps", style={"height": "180px"},
                      config={"displayModeBar": False}),
        ], style={"flex": "1"}),
    ], style={"display": "flex", "gap": "8px", "marginBottom": "8px"}),

    # ---- Row 3: ACS currents + BMS status ----
    html.Div([
        card([
            label("Circuit Currents (ACS712)"),
            dcc.Graph(id="chart-acs", style={"height": "180px"},
                      config={"displayModeBar": False}),
        ], style={"flex": "3"}),
        card([
            label("BMS Status"),
            html.Div(id="bms-status", style={"marginTop": "8px"}),
        ], style={"flex": "1"}),
    ], style={"display": "flex", "gap": "8px"}),

], style={
    "background": BG, "minHeight": "100vh",
    "padding": "10px", "fontFamily": "monospace",
})

# ---------------------------------------------------------------------------
#  Callback
# ---------------------------------------------------------------------------
@app.callback(
    Output("status-badge", "children"),
    Output("status-badge", "style"),
    Output("fault-banner", "children"),
    Output("gauge-soc",    "children"),
    Output("gauge-pv",     "children"),
    Output("gauge-pa",     "children"),
    Output("gauge-pw",     "children"),
    Output("range-val",    "children"),
    Output("chart-cells",  "figure"),
    Output("chart-temps",  "figure"),
    Output("chart-acs",    "figure"),
    Output("bms-status",   "children"),
    Input("tick", "n_intervals"),
)
def update(_):
    d = get_data()

    # ---- Connection badge ----
    stale     = (time.time() - d["last_rx"]) > 3.0
    connected = d["connected"] and not stale
    badge_text  = "● LIVE" if connected else "● NO SIGNAL"
    badge_style = {
        "float": "right", "fontSize": "12px",
        "padding": "3px 10px", "borderRadius": "12px",
        "background": GREEN if connected else RED,
        "color": "#000" if connected else WHITE,
    }

    # ---- Fault banner ----
    fault_div = html.Div(
        "⚠  BMS FAULT DETECTED",
        style={
            "background": RED, "color": WHITE, "fontWeight": "bold",
            "textAlign": "center", "padding": "8px",
            "borderRadius": "6px", "marginBottom": "8px", "fontSize": "16px",
        }
    ) if d["flt"] else html.Div()

    # ---- SVG gauges ----
    soc_color = GREEN if d["soc"] > 20 else (YELLOW if d["soc"] > 10 else RED)
    pa_color  = YELLOW if abs(d["pa"]) > 80 else BLUE

    g_soc = make_svg_gauge(d["soc"],          0,   100, f"{d['soc']:.0f} %",        soc_color)
    g_pv  = make_svg_gauge(d["pv"],          40,    75, f"{d['pv']:.1f} V",         BLUE)
    g_pa  = make_svg_gauge(d["pa"],         -20,   120, f"{d['pa']:.1f} A",         pa_color)
    g_pw  = make_svg_gauge(d["pw"] / 1000,    0,     5, f"{d['pw']/1000:.2f} kW",   GREEN)

    # ---- Range estimate ----
    range_km  = round(d["soc"] / 100.0 * RATED_RANGE_KM, 1)
    range_txt = f"{range_km}\nkm"

    # ---- Cell voltages bar chart ----
    cv = d["cv"]
    cell_colors = []
    for v in cv:
        if   v < 3.0:  cell_colors.append(RED)
        elif v < 3.2:  cell_colors.append(YELLOW)
        elif v > 3.65: cell_colors.append(RED)
        else:          cell_colors.append(GREEN)

    fig_cells = go.Figure(go.Bar(
        x=[f"C{i+1}" for i in range(CELL_COUNT)],
        y=cv,
        marker_color=cell_colors,
        text=[f"{v:.3f}" for v in cv],
        textposition="outside",
        textfont={"size": 8, "color": WHITE},
    ))
    fig_cells.update_layout(
        margin=dict(l=5, r=5, t=5, b=20),
        paper_bgcolor=CARD, plot_bgcolor=CARD,
        yaxis=dict(range=[3.0, 3.8], gridcolor=BORDER,
                   tickcolor=GREY, tickfont={"color": GREY, "size": 9}),
        xaxis=dict(tickfont={"color": GREY, "size": 9}),
        height=180, showlegend=False,
    )
    fig_cells.add_hline(y=3.0,  line_dash="dot", line_color=RED,    line_width=1)
    fig_cells.add_hline(y=3.65, line_dash="dot", line_color=RED,    line_width=1)
    fig_cells.add_hline(y=3.2,  line_dash="dot", line_color=YELLOW, line_width=1)

    # ---- Temperatures bar chart ----
    temp_labels = ["NTC 1", "NTC 2", "NTC 3", "NTC 4"]
    temp_vals   = [d[k] for k in ("t0", "t1", "t2", "t3")]

    fig_temps = go.Figure(go.Bar(
        x=temp_labels,
        y=[v if v != -99 else 0 for v in temp_vals],
        marker_color=[RED if v > 45 else YELLOW if v > 35 else BLUE for v in temp_vals],
        text=[f"{v}°C" if v != -99 else "N/A" for v in temp_vals],
        textposition="outside",
        textfont={"size": 10, "color": WHITE},
    ))
    fig_temps.update_layout(
        margin=dict(l=5, r=5, t=5, b=20),
        paper_bgcolor=CARD, plot_bgcolor=CARD,
        yaxis=dict(range=[0, 60], gridcolor=BORDER,
                   tickcolor=GREY, tickfont={"color": GREY, "size": 9},
                   ticksuffix="°C"),
        xaxis=dict(tickfont={"color": GREY, "size": 9}),
        height=180, showlegend=False,
    )

    # ---- ACS currents bar chart ----
    ac = d["ac"]
    fig_acs = go.Figure(go.Bar(
        x=ACS_LABELS,
        y=ac,
        marker_color=[GREEN if v < 5 else YELLOW if v < 10 else RED for v in ac],
        text=[f"{v:.2f}A" for v in ac],
        textposition="outside",
        textfont={"size": 10, "color": WHITE},
    ))
    fig_acs.update_layout(
        margin=dict(l=5, r=5, t=5, b=40),
        paper_bgcolor=CARD, plot_bgcolor=CARD,
        yaxis=dict(range=[0, 15], gridcolor=BORDER,
                   tickcolor=GREY, tickfont={"color": GREY, "size": 9},
                   ticksuffix=" A"),
        xaxis=dict(tickfont={"color": GREY, "size": 9}, tickangle=-15),
        height=180, showlegend=False,
    )

    # ---- BMS status panel ----
    def stat_row(lbl, val, col=WHITE):
        return html.Div([
            html.Span(lbl, style={"color": GREY, "fontSize": "11px",
                                  "width": "90px", "display": "inline-block"}),
            html.Span(val, style={"color": col, "fontSize": "13px",
                                  "fontWeight": "bold"}),
        ], style={"marginBottom": "6px"})

    mos_color = GREEN if d["mos"] == 1 else YELLOW
    bms_panel = html.Div([
        stat_row("Pack V",     f"{d['pv']:.1f} V"),
        stat_row("Cell Min",   f"{d['cmin']:.3f} V  [#{d['cmni']}]",
                 RED if d["cmin"] < 3.0 else YELLOW if d["cmin"] < 3.2 else GREEN),
        stat_row("Cell Max",   f"{d['cmax']:.3f} V  [#{d['cmxi']}]",
                 RED if d["cmax"] > 3.65 else GREEN),
        stat_row("Cycles",     str(d["cyc"])),
        stat_row("Charge MOS", "ON" if d["mos"] == 1 else "OFF", mos_color),
        stat_row("Faults",     "NONE" if not d["flt"] else "ACTIVE",
                 GREEN if not d["flt"] else RED),
        stat_row("BMS Data",   "Valid" if d["vld"] else "Waiting",
                 GREEN if d["vld"] else YELLOW),
    ])

    return (badge_text, badge_style, fault_div,
            g_soc, g_pv, g_pa, g_pw, range_txt,
            fig_cells, fig_temps, fig_acs, bms_panel)

# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",         default=SERIAL_PORT,
                        help="Serial port e.g. /dev/ttyACM0")
    parser.add_argument("--baud",         default=SERIAL_BAUD, type=int)
    parser.add_argument("--host",         default="0.0.0.0",
                        help="Host to serve dashboard (0.0.0.0 = all interfaces)")
    parser.add_argument("--browser-port", default=8050, type=int)
    args = parser.parse_args()

    t = threading.Thread(target=serial_reader, args=(args.port, args.baud), daemon=True)
    t.start()

    print(f"\n  Dashboard: http://localhost:{args.browser_port}")
    print(f"  Serial:    {args.port} @ {args.baud} baud")
    print(f"\n  Fullscreen: chromium-browser --kiosk http://localhost:{args.browser_port}\n")

    app.run(host=args.host, port=args.browser_port, debug=False)
