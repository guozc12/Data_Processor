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

Layout is rebuilt in `ScanPlotApplet._rebuild_main_layout()` (overrides generated `Ui_MainWindow` grid).

```
┌─ Top toolbar ─────────────────────────────────────────────────────────┐
│ Data: Import | Clear scans | Merge | Global                           │
│ View: X combo | Show legend | 2D plot | Export | Copy▾                │
│ Fit:  fitfunc | Fit | Clear fit                                       │
│ Analyze: Heatmap | Batch fit                                          │
├──────────────────────────────┬────────────────────────────────────────┤
│ plot_tabs                    │ Right panel (splitter)                 │
│  [Curves] [Export] [2D] …    │  Experiments (list + Name + Save/Load) │
│  Curves = pyqtgraph pw_plot  │  Loaded scans (multi-select)           │
│  + scrollbar + lb_fit        │  ROI multi-select                      │
└──────────────────────────────┴────────────────────────────────────────┘
```

### Plot tabs

- Tab 0 **Curves** is permanent (close button removed).
- Analysis plots open as **closable** tabs via `show_plot_tab(title, widget)`.
- Style must match Curves: dark pyqtgraph axes (`_style_plot_item`), not matplotlib popups.
- Images (2D / Heatmap): `_add_image_with_colorbar` → `viridis` LUT + `ColorBarItem` in col=1.

### Multi-scan overlay

- `self.scans`: list of loaded `ScanDataset`
- Selection in `list_scans` × `list_roi` × scrollbar slice → `draw_plot()`
- Legend: auto if ≤4 series, or forced by **Show legend**
- Colors: `OVERLAY_COLORS`

`primary_scan()` = first selected scan (or scans[0]). Many analyze paths historically used primary only; Heatmap now has an explicit scan picker.

### Merge vs Global (do not confuse)

| Action | Scope | Effect |
|--------|--------|--------|
| **Merge** | ≥2 selected scans | Concatenate `points`+`data` into a new `merged_…` scan; originals kept. Requires matching `key_sets`, `function`, ROI count. |
| **Global** | Primary scan | Sum all ROIs into one channel; **rewrites** that scan’s `data` (destructive). Renames function to `Global …`. |

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

- **Export**: binned curve + optional fit → new tab + TSV clipboard.
- **Copy▾**: image (Ctrl+C) or raw x,y (Ctrl+A).

### Fit

- `Fitter.func_dict`: Gauss, Lorentz, Sine, DampSine, Exp_decay, polynomials, …
- `perform_fit` on applet: fits **all selected scan×ROI** overlays.
- Right-click Fit → `FittingDialog` (advanced / fixed params).
- Guard: `Fitter.perform_fit` requires ≥2 finite points.

---

## Experiments (save / load)

Right panel **Experiments** (above Loaded scans):

- List = `experiments/*.zip`
- Name field + **Save** / **Load** / **Refresh**
- Double-click list item → Load

### Package format (`experiments/<name>.zip`)

Contains single file `experiment.json`:

```json
{
  "version": 1,
  "name": "...",
  "saved_at": "...",
  "ui": {
    "x_index": 0,
    "fitfunc": "Gauss",
    "slice": 0,
    "legend": false,
    "scan_sel_names": ["2026-10-10\\scan000148"],
    "roi_sel": [7, 11]
  },
  "scans": [
    { "name": "...", "path": "...", "metadata": {...}, "data": [...] }
  ],
  "tabs": [ /* tab recipes */ ]
}
```

- Scans are **fully embedded** (portable zip; does not require original `data/` tree).
- Analysis tabs are **not** serialized as widgets; stored as **recipes** and replayed by `_replay_tab_recipe` (`export` / `2d` / `heatmap` / `batch`).
- `self.tab_recipes` is updated when opening analysis tabs; closing a tab pops the matching recipe index (`tab_index - 1`).

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

All secondary plots must use pyqtgraph widgets in `plot_tabs`. Matplotlib may still appear as a transitive dep; Prefer not to add `plt.show()` / `FigureCanvas` popups.

### Generated UI dead widgets

`Ui_MainWindow` still creates `cob_action` (old More menu). It is **hidden** and unused. Prefer not wiring it again; remove only if doing a deliberate UI cleanup.

---

## Important symbols (navigation)

| Symbol | Purpose |
|--------|---------|
| `ScanPlotApplet` | Main window |
| `_rebuild_main_layout` | Toolbar + tabs + right panel |
| `draw_plot` / `create_new_plot` / `set_x` | Curves rendering |
| `import_data` / `ScanPickerDialog` | Load scans from `FOLDER` |
| `batch_fit_selected` / `BatchAnalyzeDialog` | Unified analyze |
| `generate_heatmap` / `HeatmapDialog` | ROI grid heatmap |
| `two_d_plot` | Slice stack image |
| `save_experiment` / `open_experiment` / `refresh_experiment_list` | Experiment zip I/O |
| `_record_tab_recipe` / `_replay_tab_recipe` | Tab persistence |
| `_add_image_with_colorbar` | Shared image + viridis bar |
| `Fitter` | Curve fitting library |

Unused / legacy still in file (safe to delete in a cleanup PR): `ParameterDialog` (superseded by Batch analyze modes).

---

## Suggested future work (for agents)

Prioritize only what the user requests; these are known gaps:

1. **2D / Export multi-scan**: currently primary-centric; may need scan picker like Heatmap.
2. **Experiment UX**: delete zip from UI; export/import zip from arbitrary path; progress bar on large embeds.
3. **Global undo**: Global is destructive; soft-copy before mutate.
4. **Performance**: batch-fitting many scans×ROIs is serial; consider progress dialog / skip empty ROIs earlier.
5. **Split module**: extract loaders / Fitter / dialogs only if file size blocks work.
6. **Tests**: no automated GUI suite; smoke via headless `QApplication` + mocked dialogs (see past agent sessions).
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
