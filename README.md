# Data Processor / Scan Plot

Standalone PyQt6 + pyqtgraph GUI for loading lab **scan folders**, overlaying curves, fitting, and saving named **experiment packages**.

**Primary entry:** [`scan_plot_standalone.py`](scan_plot_standalone.py) (~4k lines, single-file app).

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

---

## Repository layout

| Path | Role |
|------|------|
| `scan_plot_standalone.py` | Entire app: UI, loaders, fitters, experiments |
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

- One axis index as **X** (`cob_x` on Curves dock).
- Remaining axes → **slice keys** → scrollbar pages.
- Each slice: one `Plot` per ROI with raw `x`, `y`, optional fit + params.

Plotting:

- Survival → `get_survival_rate` (Wilson-style) + asymmetric errors
- Else → `get_errorbars` (mean ± SEM)
- Empty / all-NaN bins skipped when drawing

---

## UI architecture (current)

Layout: `ScanPlotApplet._rebuild_main_layout()`.

- **No global Analyze toolbar.** Rename docks by **double-clicking the title bar**.
- **Browser**: Import, Refresh all, Merge/Global/Clear, Experiments. (No Loaded-scans / ROI lists — both live in the plot Encoding panel.)
- **View / Style / Fit / analyze** are per-plot, in the left sidebar of Curves.

Mental model: after Import, selected scans form a **cube** (native scan axes × **ROI** × **scan**). Each plot dock is a **universal plotter**:

1. **Encoding** (left): Type (XY) / X / Y / Dimensions (One·Multi·All·Mean)
2. **Style** (left): Legend, Error bars, Mode
3. **Analyze** (left): Fit, Derive, legacy 2D/Batch/ROI grid/Export, Copy

```
┌─ Curves ──────────────────────────────┬─ Browser ─────────────┤
│ Encoding │ plot                         │ Data: Import          │
│ Style    │                              │       Refresh all     │
│ Analyze  │                              │       Merge/Global    │
│          │                              │ Experiments           │
└────────────────────────────────────────┴───────────────────────┘
```

- Plot **encoding** (not Browser ROI) is the source of truth for what is drawn.
- **Refresh all** re-runs legacy analysis docks from recipes with current context. Experiment Load still restores frozen context.

### Per-dock control strip

| Dock kind | Controls |
|-----------|----------|
| `curves` | `EncodingPanel` (Type/X/Dimensions incl. ROI+scan) + Legend, Style, Fit, Clear, **Derive**, 2D, Batch fit, ROI grid, Export, Copy |
| Derive (fit-param) | Same `EncodingPanel` (Y = one fit param) + Style/Fit/Copy |
| `derived_merge`, `derived_global` | Style/Fit/Copy (+ legacy encoding path may remain) |
| `export`, `batch` | Legend, Error bars, Style, Fit, Clear fit, Copy |
| `2d`, `heatmap` (ROI grid) | Copy only |

Handlers bind the **dock object** explicitly (`lambda …, d=dock: …`).

### Defaults (`default_plot_style` / `PlotViewState`)

- Style mode: **Scatter**
- Legend: **on**
- Error bars: **on**
- Line width: **fixed 2** (no Width spinner in UI)

### `PlotViewState` (on each `PlotDock.state`)

| Field | Role |
|-------|------|
| `kind` | `curves` \| `export` \| `2d` \| `heatmap` \| `batch` \| `derived_merge` \| `derived_global` |
| `style` | `{mode, linewidth=2, symbol_size, show_errorbars}` |
| `show_legend` / `show_errorbars` | View toggles |
| `fitfunc` | Per-dock fit function name |
| `cached_xy` | Snapshot curve `(x, y, heights, y_centers)` for Export / some Batch |
| `fit_overlays` | Snapshot fit curves `{x,y,color}` — survive Style redraw |
| `panel_series` | Batch vs-time multi-panel series (+ optional `fit_x`/`fit_y`) |
| `derived_scan` | Non-destructive Merge/Global dataset |
| `encoding` | Universal plotter state: `{plot_type, x_dim, y_channel, dims}` |
| `derived_points` | Fit-param points for Derive docks |
| `encoding_dim_state` | Legacy bridge for older derived docks |
| `recipe` | Experiment persistence blob |

### Curves dock

- `closable=False` — **no close button**; cannot be dismissed from UI.
- Starts **hidden**; `import_data` → `_show_curves_dock()` + `_relayout_main_docks()`.
- Central widget is 0×0 so Left/Right docks own the client area (avoids Curves as a thin left sliver).

### Multi-scan overlay

- `self.scans`: loaded scans only (derived docks are **not** listed).
- `list_scans` × `list_roi` × scrollbar → `draw_plot()` on Curves.
- Colors: `OVERLAY_COLORS`.
- `primary_scan()` = first selected (or `scans[0]`).

### Merge / Global / Clear (Loaded scans RMB)

| Action | Effect |
|--------|--------|
| **Merge** (≥2) | Floating `derived_merge` dock; originals untouched; not in Loaded scans |
| **Global** | Floating `derived_global` (sum ROIs); non-destructive |
| **Clear all…** | Confirm → clear scans + close analysis docks |

Import: `ScanPickerDialog` with filter + sort (Name/Date).

### Theme

- `APP_STYLESHEET` + `_style_plot_item`
- Images: `_add_image_with_colorbar` → viridis + `ColorBarItem`
- Legend click toggles curve **and** linked `ErrorBarItem` (`link_curve_errorbar`)

---

## Analyze features

Entry points live on the **Curves** dock (not a global toolbar). Fitfunc / Curve axis come from Curves.

### Batch fit (`BatchAnalyzeDialog`)

Choose **outer dimension** (replaces old mode radios):

| Outer | Behavior |
|------|----------|
| `scan time` | Fit selected scans×selected ROIs @ slice; params vs time (`panel_series`) |
| `ROI (all)` | One scan; fit **all** ROIs; param vs ROI index |
| scan axis A | One scan; fit along Curve axis; param vs axis A (other axes fixed to current slice) |

### ROI grid (kind `heatmap`)

- Same reduce as Batch outer=ROI, displayed as rows×cols color grid (preserves array layout).
- Always **all ROIs**; needs `n_rois ≥ 2`.
- Failed ROI fits → NaN cells.

### 2D plot (scan axes)

- Measurement value (same binning as Curves) vs **two user-chosen scan axes**.
- One scan × one ROI; axes beyond X/Y fixed to the current Curves slice.
- Not a fit-parameter map (that is ROI grid).

### Export / Copy

- **Export**: binned curve → new Export dock + TSV; copies Curves style/fit prefs; `cached_xy` + optional `fit_overlays`.
- **Copy ▾**:
  - Copy image (Ctrl+C) — grabs dock widget
  - Copy raw data (Ctrl+A) — TSV via `_dock_copy_raw`:
    - snapshot → `x, y, yerr`
    - Curves → all selected scan×ROI unbinned series
    - derived → that dock’s scan
    - Batch panels → side-by-side columns
  - Status bar reports success/failure

### Fit (per dock)

- Fit / Clear / function combo on **that** dock only (`_dock_fit` → `perform_fit(..., dock=)`).
- Curves: fits live on `scan.plots`; redraw re-attaches.
- Export/snapshot: `fit_overlays` reapplied after Style/Legend/Errorbars.
- Derived: fits on `derived_scan.plots`; `_redraw_derived_dock` re-attaches.
- Batch vs-time: `fit_x`/`fit_y` on `panel_series`; `_redraw_batch_panels`.
- Right-click Fit → `FittingDialog` (guess/fixed params).
- Guards: ≥2 finite points; `sanity_check_fit`; clipped fit y for autoRange.
- `curve_fit` may still emit `OptimizeWarning` (covariance) on short/noisy series — data issue, not wiring.

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
- Recipes replayed by `_replay_tab_recipe` (`export` / `2d` / `heatmap` / `batch`). **Derived merge/global not restored.**
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

Do **not** listen to `ZOrderChange` on docks (focus storm). Focus via MouseButtonPress / WindowActivate + explicit `set_focused_dock` in dock control handlers.

### Lambdas on dock controls

Use default-arg capture: `lambda _=False, d=dock: self._dock_fit(d)` — never close over loop variable without default.

### No matplotlib plot windows

Secondary plots = pyqtgraph in `PlotDock`s only.

### Legacy UI chrome

`Ui_MainWindow` still builds `cob_fit`, `bt_fit`, `cob_action`, etc. They are **hidden/unused**. Per-dock controls are the real UI. Prefer not re-wiring legacy widgets.

### Floating dock placement

`_place_floating_dock` centers new floating docks over the main window (deferred with `QTimer.singleShot(0, …)`).

---

## Important symbols

| Symbol | Purpose |
|--------|---------|
| `ScanPlotApplet` | Main window |
| `PlotDock` / `PlotViewState` | Per-panel dock + state |
| `_build_dock_control_bar` | Per-dock View/Style/Fit/Copy strip |
| `_dock_fit` / `_dock_copy_raw` / `_redraw_dock` | Dock-scoped actions |
| `_rebuild_main_layout` | Toolbar + docks + browser |
| `show_plot_dock` | Open analysis/derived dock (`show_plot_tab` alias) |
| `draw_plot` / `set_x` / `create_new_plot` | Curves rendering |
| `import_data` / `ScanPickerDialog` | Load scans |
| `batch_fit_selected` / `BatchAnalyzeDialog` | Batch analyze |
| `generate_heatmap` / `HeatmapDialog` | ROI heatmap |
| `two_d_plot` | Slice stack image |
| `merge_selected_scans` / `globalify` / `_open_derived_dock` | Derived docks |
| `save_experiment` / `open_experiment` | Experiment zip I/O (v2) |
| `_record_tab_recipe` / `_replay_tab_recipe` | Dock recipe persistence |
| `Fitter` / `sanity_check_fit` | Curve fitting |

Legacy still in file (cleanup candidates): `ParameterDialog`, hidden `cob_action` / old fit buttons.

---

## Known gaps / good next work

Only do these if the user asks:

1. Derived docks: no slice scrollbar; merge/global not embedded in experiment zip; Batch/2D/ROI grid not yet on derived docks.
2. Promote derived → Loaded scans (explicit action).
3. Full encoding shelf (explicit Color/Y channels) — currently simplified presets.
4. Batch vs-time: richer per-panel style; progress dialog for large fits.
5. Import dialog virtualization (slow with ~10k scans).
6. Title rename UX polish (double-click can feel flaky on some docks).
7. Dead UI cleanup: `ParameterDialog`, unused `Ui_MainWindow` chrome.
8. Optional module split if file size blocks work.

---

## Verification habits

1. Restart GUI after edits.
2. Smoke: Import → Curves Fit/Style → Export Fit + restyle (fit must stay) → Copy raw → Batch (3 modes) → Heatmap → Merge/Global → Save exp → Clear → Load exp.
3. Watch `TypeError: takes N positional arguments but M were given` on new button slots.
4. Prefer `logging` with scan name + ROI; status bar for user-facing copy/fit results.

Offscreen smoke pattern:

```bash
# conda activate lab_data_processor
python -c "import os,sys; os.environ['QT_QPA_PLATFORM']='offscreen'; ..."
```

---

## Origin

Related historically to ARTIQ scan applets (commented `SimpleApplet` / RPC). This tool analyzes exported CSV pairs offline for lab presentation and multi-day comparison.
