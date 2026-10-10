# Data Processor / Scan Plot

Standalone PyQt6 + pyqtgraph GUI for loading lab **scan folders**, overlaying curves, fitting, and saving named **experiment packages**.

Primary entry point: [`scan_plot_standalone.py`](scan_plot_standalone.py) (~2.7k lines, single-file app).

This README is written for **future AI agents** continuing optimization. Prefer small, localized changes; preserve existing data semantics and Qt slot patterns described below.

---

## Quick start

```bash
# Recommended: conda env (pip-only deps to avoid Qt5/Qt6 conflicts on Windows)
conda env create -f environment.yml
conda activate lab_data_processor

python scan_plot_standalone.py
```

Deps also listed in `requirements.txt` (same packages). **Windows** is assumed (`pywin32` clipboard DIB).

Env name: `lab_data_processor`. Python 3.11.

---

## Repository layout

| Path | Role |
|------|------|
| `scan_plot_standalone.py` | Entire app UI + loaders + fitters + experiments |
| `data/YYYY-MM-DD/scanHHMMSS/` | Raw scans (`log.csv` + `logResults.csv`) |
| `experiments/*.zip` | Saved experiment packages (portable) |
| `environment.yml` / `requirements.txt` | Runtime deps |

There is no multi-package Python layout yet. Do not invent a framework refactor unless the user asks.

---

## Data model

### On-disk scan

Each scan folder must contain:

- `log.csv` — scan axis header + one row of parameter points per shot
- `logResults.csv` — result function name on first line, then values (scalar or per-ROI lists)

Loader: `load_scan_pair(log_path, results_path)` → `ScanDataset`.

**Incomplete scans:** if `#points ≠ #results`, truncate to `min` length and log a warning (planned points written before results finish).

### `ScanDataset`

```python
@dataclass
class ScanDataset:
    name: str          # usually relative path under data/, e.g. "2026-10-10\\scan000148"
    path: Path         # scan folder
    metadata: dict     # key_sets, points, function
    data: list         # per-point results (float or list-of-ROI)
    plots: object      # Plots instance after create_new_plot / set_x
```

### `metadata`

- `key_sets`: list of parameter name paths (from log header `Module:...`)
- `points`: list of tuples — one scan-parameter vector per shot
- `function`: result label (e.g. `"Survival probability"`)

### `Plots`

Built by `build_plots_for_scan(scan, x_index)`:

- Chooses one axis index as **X** (`cob_x`).
- Groups remaining axes into **slice keys** → scrollbar pages.
- Each slice has one `Plot` per ROI with raw `x`, `y`, optional fit curve + params.

Survival vs non-survival plotting:

- Survival: Wilson-style rates via `get_survival_rate` + asymmetric errors
- Else: mean ± SEM via `get_errorbars`

Empty / all-NaN ROI bins are skipped when drawing (avoid All-NaN crashes).

---

## UI architecture

Layout is rebuilt in `ScanPlotApplet._rebuild_main_layout()`. All plot panels are **`PlotDock` (`QDockWidget`)** — including Curves (C2). Browser (Experiments / Loaded scans / ROI) is also a dock.

```
┌─ QToolBar: Data (Import) | Analyze (Heatmap, Batch fit) ───────────────────────────────────┐
├─ PlotDocks (each has its own View/Style/Fit strip) ───────────────┬─ Browser dock ────────┤
│ Curves (permanent, closable=False; shown after Import)             │ Experiments           │
│   [X | Legend | Error bars | Style | Fit | 2D | Export | Copy]     │ Loaded scans (RMB)    │
│ Export / Batch / Derived — same strip (floats with the dock)       │ ROI multi-select      │
│ 2D / Heatmap — Copy only                                           │                       │
└────────────────────────────────────────────────────────────────────┴───────────────────────┘
```

### Plot docks

- **Curves**: permanent (`closable=False`); hidden until Import, then always present. Rename any dock by double-clicking its title.
- Analysis / derived: `show_plot_dock(title, widget, kind=…)` (alias `show_plot_tab` kept).
- Per-dock `PlotViewState`: style (`Scatter` / `Line` / `Line+marker`; line width fixed at 2), legend, errorbars, fit function.
- **View / Style / Fit controls live inside each plot dock** (`_build_dock_control_bar`) and move with float/dock — not on the main toolbar.
- Dark theme: `APP_STYLESHEET` + `_style_plot_item` (subtle grid; teal/orange/sky/lavender).
- Images (2D / Heatmap): `_add_image_with_colorbar` → `viridis` + `ColorBarItem`.
- Legend click toggles curve **and** linked `ErrorBarItem` (`link_curve_errorbar`).

### Multi-scan overlay

- `self.scans`: loaded `ScanDataset` only (derived results are **not** listed here)
- Selection in `list_scans` × `list_roi` × scrollbar → `draw_plot()` on Curves dock
- Colors: `OVERLAY_COLORS`

`primary_scan()` = first selected scan (or scans[0]).

### Merge vs Global (Loaded scans context menu)

| Action | Scope | Effect |
|--------|--------|--------|
| **Merge** | ≥2 selected scans | Concatenate into a temporary dataset; open floating `derived_merge` dock. Originals untouched; **not** added to Loaded scans. |
| **Global** | Primary scan | Deep-copy + sum ROIs; open floating `derived_global` dock. **Non-destructive**. |
| **Clear all…** | All loaded | Confirm, then clear scans + close analysis docks. |

Import dialog: filter substring + sort by Name/Date.

---

## Analyze features

### Batch fit (`BatchAnalyzeDialog`)

Replaces the old separate **Fit param** button. Modes:

| Mode | Behavior |
|------|----------|
| `vs_time` | Fit every selected scan × selected ROI at current slice; plot params vs scan time (`DateAxisItem` when timestamps parse). |
| `vs_axis` | One target scan; fit along secondary scan axis (scrollbar slices); scatter of one param. |
| `vs_ROI` | One target scan; current slice; param vs selected ROIs. |

Implementation: `batch_fit_selected` → `_plot_param_scatter` for axis/ROI modes.

### Heatmap

- Dialog picks **which selected scan**, fit parameter, rows×cols (must equal that scan’s ROI count).
- Fits **all ROIs** of that scan at current slice; failed ROIs → NaN cells (no crash).

### 2D plot

- For **≥2D scans**: stack scrollbar slices into an image.
- X = current `cob_x`; Y = remaining scan key (or slice index if not a single secondary key).
- Uses current **primary** selection for data (`get_current_data` path). Multi-scan 2D is not implemented.

### Export / Copy

- **Export**: binned curve + optional fit → new **dock** + TSV clipboard (`cached_xy` for re-style/fit).
- **Copy▾**: focused dock image (Ctrl+C) or raw x,y (Ctrl+A).

### Fit

- `Fitter.func_dict`: Gauss, Lorentz, Sine, DampSine, Exp_decay, polynomials, …
- Fit button / Clear fit / function combo on **that dock’s** control strip; acts on that dock only.
- Image docks (2D/Heatmap): no Fit controls (Copy only).
- Right-click Fit → `FittingDialog` (advanced / fixed params).
- Guards: ≥2 finite points; `sanity_check_fit` rejects extreme amp/width/freq; fit y clipped so autoRange is not blown out.

---

## Experiments (save / load)

Browser dock **Experiments**:

- List = `experiments/*.zip`
- Name field + **Save** / **Load** / **Refresh**
- Double-click → Load
- Right-click: Open / Duplicate / Delete / Reveal in folder / Refresh

### Package format (`experiments/<name>.zip`)

Contains single file `experiment.json`:

```json
{
  "version": 2,
  "name": "...",
  "saved_at": "...",
  "ui": {
    "x_index": 0,
    "fitfunc": "Gauss",
    "slice": 0,
    "legend": false,
    "style": {"mode": "Line+marker", "linewidth": 1.5},
    "scan_sel_names": ["2026-10-10\\scan000148"],
    "roi_sel": [7, 11]
  },
  "scans": [
    { "name": "...", "path": "...", "metadata": {...}, "data": [...] }
  ],
  "tabs": [ /* dock recipes */ ],
  "dock_layout": "<base64 QMainWindow.saveState>"
}
```

- Scans are **fully embedded** (portable zip; does not require original `data/` tree).
- Analysis docks stored as **recipes** and replayed by `_replay_tab_recipe` (`export` / `2d` / `heatmap` / `batch`). Derived merge/global recipes are skipped on load (not embedded).
- `dock_layout` restores dock geometry when present (v2). **v1** zips still load (recipes only, default layout).
- Closing an analysis dock removes its recipe from `self.tab_recipes`.

After Load, window title becomes `Scan Plot — <name>`.

---

## Critical Qt gotchas (read before editing slots)

### `clicked(bool)` vs optional `recipe=None`

If a slot is defined as:

```python
def batch_fit_selected(self, recipe=None):
def generate_heatmap(self, recipe=None):
```

and connected to `QPushButton.clicked`, PyQt **passes the checked bool**. Then `recipe` becomes `False`, `if recipe is None` fails, and `recipe.get(...)` crashes.

**Required pattern** (already applied):

```python
if not isinstance(recipe, dict):
    recipe = None
```

Same applies to any new slot with optional non-bool first args connected to signals with payloads.

Narrow `(self)`-only slots are fine; PyQt drops extras when the signature has no room.

### Do not reintroduce matplotlib plot windows

All secondary plots must use pyqtgraph widgets in `PlotDock`s. Matplotlib may still appear as a transitive dep; Prefer not to add `plt.show()` / `FigureCanvas` popups.

### Generated UI dead widgets

`Ui_MainWindow` still creates `cob_action` (old More menu). It is **hidden** and unused. Prefer not wiring it again; remove only if doing a deliberate UI cleanup.

---

## Important symbols (navigation)

| Symbol | Purpose |
|--------|---------|
| `ScanPlotApplet` | Main window |
| `PlotDock` / `PlotViewState` | Per-panel dock + style/fit state |
| `_rebuild_main_layout` | Toolbar + docks + browser |
| `show_plot_dock` | Open analysis/derived dock |
| `draw_plot` / `create_new_plot` / `set_x` | Curves rendering |
| `import_data` / `ScanPickerDialog` | Load scans (filter/sort) |
| `batch_fit_selected` / `BatchAnalyzeDialog` | Unified analyze |
| `generate_heatmap` / `HeatmapDialog` | ROI grid heatmap |
| `two_d_plot` | Slice stack image |
| `merge_selected_scans` / `globalify` | Derived floating docks |
| `save_experiment` / `open_experiment` | Experiment zip I/O (v2) |
| `_record_tab_recipe` / `_replay_tab_recipe` | Dock recipe persistence |
| `_add_image_with_colorbar` | Shared image + viridis bar |
| `Fitter` / `sanity_check_fit` | Curve fitting + extreme reject |

Unused / legacy still in file (safe to delete in a cleanup PR): `ParameterDialog` (superseded by Batch analyze modes).

---

## Suggested future work (for agents)

Prioritize only what the user requests; these are known gaps:

1. **2D / Export multi-scan**: currently primary-centric; may need scan picker like Heatmap.
2. **Embed derived docks** in experiment zip (merge/global currently skipped on load).
3. **Promote derived → Loaded scans** (explicit user action).
4. **Performance**: batch-fitting many scans×ROIs is serial; progress dialog.
5. **Split module**: extract loaders / Fitter / dialogs only if file size blocks work.
6. **Tests**: expand offscreen smoke beyond init + Gauss fit.
7. **Remove dead UI**: `cob_action`, unused `ParameterDialog`, leftover matplotlib if unused.
8. **Heatmap grid**: auto-suggest rows/cols from ROI count; validate before fit loop.

---

## Verification habits

After behavioral changes:

1. Restart the GUI process (code is not hot-reloaded).
2. Smoke: Import → overlay Fit → Batch fit (all 3 modes) → Heatmap (pick scan) → Save exp → Clear → Load exp.
3. Watch for `TypeError: takes N positional arguments but M were given` on new buttons (Qt signal arity).
4. Prefer `logging.warning/error` with scan name + ROI in messages.

---

## Origin / product context

Originally related to ARTIQ scan applets (commented `SimpleApplet` / RPC). This standalone tool analyzes exported CSV pairs offline for lab presentation and multi-day comparison.
