---
name: dashboard-lightweight-ui-redesign
description: Replace heavy Plotly Indicator gauge charts with SVG arc gauges in dashboard.py for reliable rendering on 4GB Ubuntu system
metadata:
  type: project
---

# Dashboard Lightweight UI Redesign

## Problem
Plotly `go.Indicator` gauge charts render as white empty boxes on the Ubuntu Mini PC (4GB RAM, Chromium kiosk). The bar charts (cell voltages, ACS, temps) render correctly. The bottleneck is the SVG-heavy Indicator component type.

## Goal
Replace the 5 gauge chart components with pure SVG arc gauges. Keep all bar charts unchanged. Result must look graphical, presentable, and user-friendly.

## Scope
Single file change: `dashboard.py`

---

## Design

### Row 1 — SVG Arc Gauges (replaces Plotly Indicator)

Five cards, same layout as today:

| Card | Range | Color logic |
|------|-------|-------------|
| SOC | 0–100 % | Green >20%, Yellow >10%, Red ≤10% |
| Pack V | 40–75 V | Blue always |
| Pack A | -20–120 A | Blue <80A, Yellow ≥80A |
| Power | 0–5 kW | Green always |
| Est. Range | — | Large text only (no arc needed) |

**SVG arc spec:**
- Semicircle (180° sweep), rendered as two SVG `<path>` elements
- Track arc: dark gray (`#21262d`)
- Value arc: colored, length proportional to `(value - min) / (max - min)`
- Large centered number below arc with unit suffix
- Min/max tick labels at arc ends
- Generated server-side in Python as an `html.Div` containing an inline SVG string
- No external JS library — pure SVG injected via `dangerously_allow_html=True`

### Row 2 — Cell Voltages + Temperatures
No change. `go.Bar` charts render correctly.

### Row 3 — ACS Currents + BMS Status
No change.

### Performance
- `dcc.Interval` update rate: **500 ms** (from 250 ms) — halves callback frequency, still feels live
- No new dependencies

---

## Implementation

One new helper function `make_svg_gauge(value, vmin, vmax, unit, color, label)` returns an `html.Div` with inline SVG. Replace the 4 `dcc.Graph(id="gauge-*")` elements and their callback outputs with `html.Div(id="gauge-*")`. The `make_gauge()` Plotly function is removed entirely.

## What Does Not Change
- Serial reader thread and `_data` store
- All bar charts and their callbacks
- BMS status panel
- Fault banner
- Connection badge
- CLI arguments
- Dark theme colors
