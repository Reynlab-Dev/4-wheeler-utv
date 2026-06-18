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
UPDATE_MS      = 250          # dashboard refresh interval
RATED_RANGE_KM = 70           # from spec: 60-80 km range

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

# Suppress callback exceptions for dynamic layout
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
        "color": GREY, "fontSize": "11px",
        "textTransform": "uppercase", "letterSpacing": "1px",
        "marginBottom": "4px",
    })

app.layout = html.Div([
    dcc.Interval(id="tick", interval=UPDATE_MS, n_intervals=0),

    # ---- Header ----
    html.Div([
        html.Span("UTV EV Platform", style={
            "fontSize": "20px", "fontWeight": "bold", "color": WHITE
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

    # ---- Row 1: Big numbers ----
    html.Div([
        card([label("State of Charge"), dcc.Graph(id="gauge-soc", style={"height": "160px"}, config={"displayModeBar": False})],
             style={"flex": "1"}),
        card([label("Pack Voltage"), dcc.Graph(id="gauge-pv",  style={"height": "160px"}, config={"displayModeBar": False})],
             style={"flex": "1"}),
        card([label("Pack Current"), dcc.Graph(id="gauge-pa",  style={"height": "160px"}, config={"displayModeBar": False})],
             style={"flex": "1"}),
        card([label("Power"), dcc.Graph(id="gauge-pw",  style={"height": "160px"}, config={"displayModeBar": False})],
             style={"flex": "1"}),
        card([label("Est. Range"), html.Div(id="range-val", style={
            "fontSize": "52px", "fontWeight": "bold", "color": GREEN,
            "textAlign": "center", "lineHeight": "120px",
        })], style={"flex": "0.7"}),
    ], style={"display": "flex", "gap": "8px", "marginBottom": "8px"}),

    # ---- Row 2: Cell voltages + Temperatures ----
    html.Div([
        card([
            label("Cell Voltages (19 cells)"),
            dcc.Graph(id="chart-cells", style={"height": "180px"}, config={"displayModeBar": False}),
        ], style={"flex": "3"}),
        card([
            label("Temperatures"),
            dcc.Graph(id="chart-temps", style={"height": "180px"}, config={"displayModeBar": False}),
        ], style={"flex": "1"}),
    ], style={"display": "flex", "gap": "8px", "marginBottom": "8px"}),

    # ---- Row 3: ACS currents + BMS status ----
    html.Div([
        card([
            label("Circuit Currents (ACS712)"),
            dcc.Graph(id="chart-acs", style={"height": "180px"}, config={"displayModeBar": False}),
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
#  Callbacks
# ---------------------------------------------------------------------------
def make_gauge(value, vmin, vmax, unit, color=BLUE, threshold_warn=None, threshold_crit=None):
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        number={"suffix": f" {unit}", "font": {"color": WHITE, "size": 22}},
        gauge={
            "axis": {"range": [vmin, vmax], "tickcolor": GREY,
                     "tickfont": {"color": GREY, "size": 9}},
            "bar": {"color": color},
            "bgcolor": BG,
            "bordercolor": BORDER,
            "steps": [{"range": [vmin, vmax], "color": "#21262d"}],
            "threshold": {
                "line": {"color": RED, "width": 2},
                "thickness": 0.75,
                "value": threshold_crit or vmax,
            } if threshold_crit else {},
        },
    ))
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor=CARD, plot_bgcolor=CARD, font_color=WHITE,
        height=160,
    )
    return fig

@app.callback(
    Output("status-badge", "children"),
    Output("status-badge", "style"),
    Output("fault-banner", "children"),
    Output("gauge-soc",    "figure"),
    Output("gauge-pv",     "figure"),
    Output("gauge-pa",     "figure"),
    Output("gauge-pw",     "figure"),
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
    stale = (time.time() - d["last_rx"]) > 3.0
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
            "borderRadius": "6px", "marginBottom": "8px",
            "fontSize": "16px",
        }
    ) if d["flt"] else html.Div()

    # ---- Gauges ----
    soc_color = GREEN if d["soc"] > 20 else (YELLOW if d["soc"] > 10 else RED)
    g_soc = make_gauge(d["soc"],  0, 100, "%",  soc_color)
    g_pv  = make_gauge(d["pv"],  40,  75, "V",  BLUE)
    g_pa  = make_gauge(d["pa"], -20, 120, "A",  YELLOW if d["pa"] > 80 else BLUE)
    g_pw  = make_gauge(d["pw"] / 1000, 0, 5, "kW", GREEN)

    # ---- Range estimate ----
    range_km  = round(d["soc"] / 100.0 * RATED_RANGE_KM, 1)
    range_txt = f"{range_km} km"

    # ---- Cell voltages bar chart ----
    cv = d["cv"]
    cell_colors = []
    for v in cv:
        if   v < 3.0:   cell_colors.append(RED)
        elif v < 3.2:   cell_colors.append(YELLOW)
        elif v > 3.65:  cell_colors.append(RED)
        else:           cell_colors.append(GREEN)

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
    # Min/max reference lines
    fig_cells.add_hline(y=3.0,  line_dash="dot", line_color=RED,    line_width=1)
    fig_cells.add_hline(y=3.65, line_dash="dot", line_color=RED,    line_width=1)
    fig_cells.add_hline(y=3.2,  line_dash="dot", line_color=YELLOW, line_width=1)

    # ---- Temperatures bar chart ----
    temp_labels = ["NTC 1", "NTC 2", "NTC 3", "NTC 4"]
    temp_vals   = [d[k] for k in ("t0", "t1", "t2", "t3")]
    temp_valid  = [v for v in temp_vals if v != -99]
    temp_colors = [RED if v > 45 else YELLOW if v > 35 else GREEN
                   for v in temp_vals]

    fig_temps = go.Figure(go.Bar(
        x=temp_labels,
        y=[v if v != -99 else 0 for v in temp_vals],
        marker_color=[RED if v > 45 else YELLOW if v > 35 else BLUE
                      for v in temp_vals],
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
        xaxis=dict(tickfont={"color": GREY, "size": 9},
                   tickangle=-15),
        height=180, showlegend=False,
    )

    # ---- BMS status panel ----
    def stat_row(label_txt, value_txt, val_color=WHITE):
        return html.Div([
            html.Span(label_txt, style={"color": GREY, "fontSize": "11px", "width": "90px", "display": "inline-block"}),
            html.Span(value_txt, style={"color": val_color, "fontSize": "13px", "fontWeight": "bold"}),
        ], style={"marginBottom": "6px"})

    mos_color = GREEN if d["mos"] == 1 else YELLOW
    bms_panel = html.Div([
        stat_row("Pack V",    f"{d['pv']:.1f} V"),
        stat_row("Cell Min",  f"{d['cmin']:.3f} V  [#{d['cmni']}]",
                 RED if d["cmin"] < 3.0 else YELLOW if d["cmin"] < 3.2 else GREEN),
        stat_row("Cell Max",  f"{d['cmax']:.3f} V  [#{d['cmxi']}]",
                 RED if d["cmax"] > 3.65 else GREEN),
        stat_row("Cycles",    str(d["cyc"])),
        stat_row("Charge MOS", "ON" if d["mos"] == 1 else "OFF", mos_color),
        stat_row("Faults",    "NONE" if not d["flt"] else "ACTIVE",
                 GREEN if not d["flt"] else RED),
        stat_row("BMS Data",  "Valid" if d["vld"] else "Waiting",
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
    parser.add_argument("--port", default=SERIAL_PORT,
                        help="Serial port e.g. /dev/ttyACM0")
    parser.add_argument("--baud", default=SERIAL_BAUD, type=int)
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to serve dashboard (0.0.0.0 = all interfaces)")
    parser.add_argument("--browser-port", default=8050, type=int)
    args = parser.parse_args()

    t = threading.Thread(target=serial_reader, args=(args.port, args.baud),
                         daemon=True)
    t.start()

    print(f"\n  Dashboard: http://localhost:{args.browser_port}")
    print(f"  Serial:    {args.port} @ {args.baud} baud")
    print(f"\n  Fullscreen: chromium-browser --kiosk http://localhost:{args.browser_port}\n")

    app.run(host=args.host, port=args.browser_port, debug=False)
