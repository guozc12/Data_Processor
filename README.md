# Data Processor / Scan Plot

Standalone PyQt6 + pyqtgraph GUI for loading lab **scan folders**, overlaying curves, fitting, deriving reduced plots, and saving named **experiment packages**.

**Primary entry:** [`scan_plot_standalone.py`](scan_plot_standalone.py) (single-file app).

This README is the handoff doc for **future AI agents**. Prefer small, localized changes; preserve data semantics and Qt slot patterns below. Do not invent a multi-package refactor unless the user asks.

---

## Quick start

```bash
conda env create -f environment.yml
conda activate lab_data_processor
python scan_plot_standalone.py
```

- Env: `lab_data_processor` (Python 3.11). Also in `requirements.txt`.
- **Windows** assumed (`pywin32` clipboard DIB).
- After code edits: **restart the GUI** (no hot reload).

Headless regression:

```bash
conda activate lab_data_processor
set QT_QPA_PLATFORM=offscreen
python _smoke_test_universal.py
```

---

## Repository layout

| Path | Role |
|------|------|
| `scan_plot_standalone.py` | Entire app: UI, loaders, fitters, experiments |
| `_smoke_test_universal.py` | Offscreen regression for Encoding / Derive / Average |
| `data/YYYY-MM-DD/scanHHMMSS/` | Raw scans (`log.csv` + `logResults.csv`) |
| `experiments/*.zip` | Saved experiment packages (portable) |
| `environment.yml` / `requirements.txt` | Runtime deps |

---

## Data model

### On-disk scan

Each scan folder must contain:

- `log.csv` — axis header + one row of parameter points per shot
- `logResults.csv` — result function name, then values (scalar or per-ROI lists)

Loader: `load_scan_pair(log_path, results_path)` → `ScanDataset`.

**Incomplete scans:** if `#points ≠ #results`, truncate to `min` length and log a warning.

### `ScanDataset`

```python
@dataclass
class ScanDataset:
    name: str          # e.g. "2026-10-10\\scan000148"
    path: Path
    metadata: dict     # key_sets, points, function
    data: list         # per-point results (float or list-of-ROI)
    plots: object      # Plots after build_plots_for_scan / set_x
```

### `Plots`

Built by `build_plots_for_scan(scan, x_index)`:

- One axis index as **X**.
- Remaining axes → **slice keys** → scrollbar pages (Curves).
- Each slice: one `Plot` per ROI with raw `x`, `y`, optional fit + params.

Plotting:

- Survival → `get_survival_rate` (Wilson-style) + asymmetric errors
- Else → `get_errorbars` (mean ± SEM)
- Empty / all-NaN bins skipped when drawing

---

## UI architecture

Layout: `ScanPlotApplet._rebuild_main_layout()`.

- **No global Analyze toolbar.** Rename docks by **double-clicking the title bar**.
- **Browser**: Import + Experiments only.
- **Every plot** uses the same left sidebar template (Curves and Derive/Average docks).

Mental model: **one plot = one multidimensional dataset**. Import → Curves uses the raw cube. Same-plot ops change the view; Derive ops open a **new** same-template plot, **tabified with the source plot**.

1. **Encoding**: Type (`XY` | `2D` | `ROI grid`) / axes / Dimensions
2. **Derive**: Fit param… / Average… (new data → new plot)
3. **Style**: Legend, Error bars, Mode
4. **Analyze**: Fit / Clear fit / Export / Copy / Clear data…

```
┌─ Curves (or any plot) ────────────────┬─ Browser ─────────────┤
│ Encoding │ plot                         │ Data: Import          │
│ Derive   │                              │ Experiments           │
│ Style    │                              │                       │
│ Analyze  │                              │                       │
└────────────────────────────────────────┴───────────────────────┘
```

- **Same plot**: Fit overlay, Style, Encoding Type/dims (no new dataset).
- **New plot**: Derive → Fit param… or Average… via `_open_universal_plotter` + `_attach_plot_dock_to_source` (tabify with source).

### Encoding Type

| Type | What it draws | Dim rules |
|------|---------------|-----------|
| `XY` | Y vs one X dim | Other dims One/Multi/All/Mean |
| `2D` | Heatmap of current Y vs **any two** dims (axis / ROI / scan) | Remaining dims **One** only |
| `ROI grid` | Current Y on ROI rows×cols | Other dims **One** only |

### Dimensions catalog (source of truth)

Each dock’s Encoding sidebar stores `encoding_panel._dim_catalog`.

- Curves: built from `_cube_dim_catalog()` (axes + ROI + scan).
- After **Average…** / **Fit param…**: new plot gets a **trimmed** catalog (`out_cat`). Collapsed dims (e.g. ROI after Avg ROI) must **not** reappear.
- Dialogs (Average / Fit New X) and nested Average read `_dock_dim_catalog(dock)` — the sidebar catalog — **not** a rebuild from raw `scan.data` (that would resurrect ROI when only one ROI remains).

### Derive box

| Action | Effect |
|--------|--------|
| **Fit param…** | Fit along current X (any Analyze model) → Y = one fit param → new `derived_fit` plot. Fit-along dim is dropped from Dimensions. |
| **Average…** | Multi-select dims → mean collapse → new plot. Works on **measurement cubes** (`derived_avg`) and on **Fit-param point plots** (average over ROI / scan / remaining axes; stays `derived_fit`). |

Nested Average (e.g. ROI then scan) keeps only dims still present on the source catalog; axes come from the reduced cube’s `key_sets`.

### Defaults (`default_plot_style` / `PlotViewState`)

- Style mode: **Scatter**
- Legend: **on**
- Error bars: **on**
- Line width: **fixed 2**

### `PlotViewState` (on each `PlotDock.state`)

| Field | Role |
|-------|------|
| `kind` | `curves` \| `derived_fit` \| `derived_avg` \| `export` \| `2d` \| `heatmap` \| `batch` |
| `style` / `show_legend` / `show_errorbars` | View toggles |
| `fitfunc` | Per-dock fit function name |
| `cached_xy` / `fit_overlays` | Export / snapshot redraw |
| `panel_series` | Batch vs-time multi-panel (+ optional `fit_x`/`fit_y`) |
| `derived_scan` / `derived_scans` | Reduced measurement cube(s) from Average… |
| `derived_points` | Fit-param points (`axis_vals` for later Average grouping) |
| `encoding` | `{plot_type, x_dim, y_dim?, y_channel, dims}` |
| `recipe` | Experiment persistence blob |

### Curves dock

- `closable=False` — cannot be dismissed from UI.
- Starts **hidden**; import → `_show_curves_dock()` + `_relayout_main_docks()`.
- Central widget is 0×0 so Left/Right docks own the client area.

### Multi-scan overlay

- `self.scans`: loaded scans only (derived docks are **not** listed).
- Colors: `OVERLAY_COLORS`.
- `primary_scan()` = first selected (or `scans[0]`).

### Clear data…

Analyze → **Clear data…**: confirm → wipe loaded scans and close analysis docks.

Import: `ScanPickerDialog` with filter + sort (Name/Date).

### Theme

- `APP_STYLESHEET` + `_style_plot_item`
- Images: viridis + `ColorBarItem`
- Legend click toggles curve **and** linked `ErrorBarItem` (`link_curve_errorbar`)

---

## Analyze features

### Fit (per dock)

- Fit / Clear / function combo on **that** dock only (`_dock_fit` → `perform_fit(..., dock=)`).
- Right-click Fit → `FittingDialog` (guess/fixed params).
- Guards: ≥2 finite points; `sanity_check_fit`; clipped fit y for autoRange.

### Export / Copy

- **Export**: binned curve → Export dock + TSV; copies style/fit prefs.
- **Copy ▾**: image (Ctrl+C) / raw data (Ctrl+A) via `_dock_copy_raw`.

### Legacy recipe-only paths (no main UI)

Still reachable from **experiment zip replay** and/or **Ctrl+Q** (2D dialog):

- `two_d_plot` + `TwoDPlotDialog`
- `generate_heatmap` + `HeatmapDialog`
- `batch_fit_selected` + `BatchAnalyzeDialog`

Prefer Encoding Type=`2D` / `ROI grid` and Derive Fit param… for new work. Do not delete these until recipe migration is explicit.

---

## Experiments (save / load)

Browser: list `experiments/*.zip`; Save / Load / Refresh; double-click load; RMB Open / Duplicate / Delete / Reveal.

### Package (`experiment.json` inside zip)

```json
{
  "version": 2,
  "name": "...",
  "saved_at": "...",
  "ui": {
    "x_index": 0,
    "fitfunc": "Gauss",
    "slice": 0,
    "legend": true,
    "style": {"mode": "Scatter", "linewidth": 2.0},
    "scan_sel_names": ["..."],
    "roi_sel": [0, 1]
  },
  "scans": [ { "name", "path", "metadata", "data" } ],
  "tabs": [ /* dock recipes */ ],
  "dock_layout": "<base64 QMainWindow.saveState>"
}
```

- Scans fully embedded (portable).
- Recipes replayed by `_replay_tab_recipe` (`export` / `2d` / `heatmap` / `batch`). Old `derived_merge` / `derived_global` recipes are skipped.
- v1 zips still load (no `dock_layout`).
- Closing an analysis dock drops its recipe from `self.tab_recipes`.

---

## Critical Qt gotchas

### `clicked(bool)` vs `recipe=None`

```python
def batch_fit_selected(self, recipe=None):
def generate_heatmap(self, recipe=None):
```

`QPushButton.clicked` passes a bool → `recipe` becomes `False`. **Always:**

```python
if not isinstance(recipe, dict):
    recipe = None
```

### Focus / ZOrderChange

Do **not** listen to `ZOrderChange` on docks (focus storm). Focus via MouseButtonPress / WindowActivate + explicit `set_focused_dock`.

### Lambdas on dock controls

Use default-arg capture: `lambda _=False, d=dock: self._dock_fit(d)`.

### No matplotlib plot windows

Secondary plots = pyqtgraph in `PlotDock`s only.

### Legacy UI chrome

`Ui_MainWindow` still builds some hidden widgets (`cob_x`, `cob_curves_x`, …) used as a **bridge** from Encoding → Curves draw (`_sync_legacy_from_encoding`). Do not delete those without rewiring draw paths.

### New derived docks

`_attach_plot_dock_to_source` tabifies with the creating dock (or Curves). Prefer this over floating Right placement for Fit param / Average.

---

## Important symbols

| Symbol | Purpose |
|--------|---------|
| `ScanPlotApplet` | Main window |
| `PlotDock` / `PlotViewState` | Per-panel dock + state |
| `EncodingPanel` / `DerivePanel` / `StylePanel` / `AnalyzePanel` | Sidebar template |
| `_open_universal_plotter` / `_attach_plot_dock_to_source` | New Derive/Average docks |
| `_dock_dim_catalog` | Dimensions available on a dock |
| `derive_fit_param_plot` / `_compute_derive_points` | Fit-param Derive |
| `average_cube_dims` / `_average_scans_over_dims` / `_average_derived_points_over_dims` | Average… |
| `draw_plot` / `_redraw_universal_dock` / `_redraw_derived_dock` | Rendering |
| `import_data` / `ScanPickerDialog` | Load scans |
| `save_experiment` / `open_experiment` | Experiment zip I/O (v2) |
| `Fitter` / `sanity_check_fit` | Curve fitting |

---

## Known gaps / good next work

Only do these if the user asks:

1. Embed Derive/Average docks fully in experiment zip (points + reduced cubes).
2. Promote derived → Loaded scans (explicit action).
3. Full encoding shelf (explicit Color/Y channels).
4. Import dialog virtualization (slow with ~10k scans).
5. Drop recipe+Ctrl+Q for legacy `two_d_plot` / heatmap / batch once unused.
6. Optional module split if file size blocks work.
7. Title rename UX polish.

---

## Verification habits

1. Restart GUI after edits.
2. Smoke: Import → Encoding XY Fit/Style → Type 2D → Type ROI grid → Fit param… → Average… (incl. on Fit plot) → nested Average → Export → Clear data → Load exp.
3. Watch `TypeError: takes N positional arguments but M were given` on new button slots.
4. Prefer `logging` with scan name + ROI; status bar for user-facing results.
5. Run `_smoke_test_universal.py` offscreen after Derive/Average/Encoding changes.

---

## Origin

Related historically to ARTIQ scan applets. This tool analyzes exported CSV pairs offline for lab presentation and multi-day comparison.
