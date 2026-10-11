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
| `_smoke_test_universal.py` | Offscreen regression for Encoding / Derive / Average, Loaded scans delete/refresh, journal widths, figure export, and Oct 10–11 load timing |
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

- Survival → `get_survival_rate` (Wilson 68%, one `proportion_confint` call per series) + asymmetric errors
- Else → `get_errorbars` (mean ± SEM), bins via a sort, not a mask per unique X
- Empty / all-NaN bins skipped when drawing
- Rectangular scans are grouped into slice lists inside `build_plots_for_scan`. Ragged ROI rows still use `Plots.add_point`.

---

## UI architecture

Layout: `ScanPlotApplet._rebuild_main_layout()`.

- **No global Analyze toolbar.** Rename docks by **double-clicking the title bar**.
- **Browser**: current experiment's **Loaded scans** (Import / Delete / Refresh) plus saved **Experiments**.
- **Every plot** uses the same left sidebar template (Curves and Derive/Average docks). Export / 2D / heatmap / batch docks use a control bar instead (journal, axis labels, Refresh, Copy).

Mental model: **Loaded scans is the current experiment.** If nothing has been saved, that experiment is temporary (`Unsaved`). Import and Delete change membership only. Plots stay as they were until **Refresh**. One plot is one view of that membership (or of a derived copy). Derive ops open a **new** same-template plot, **tabified with the source plot**.

1. **Encoding**: Type (`XY` | `2D` | `ROI grid`) / axes / Dimensions
2. **Derive**: Fit param… / Average… (new data → new plot)
3. **Style**: Legend, Error bars, Mode, Point size, Journal, Column, X/Y labels
4. **Analyze**: Fit / Clear fit / Refresh / Export / Copy / Clear data…

```
┌─ Curves (or any plot) ────────────────┬─ Browser ─────────────────┤
│ Encoding │ plot                         │ Unsaved *                 │
│ Derive   │                              │ Loaded scans              │
│ Style    │                              │ Import / Delete / Refresh │
│ Analyze  │                              │ Experiments               │
└────────────────────────────────────────┴───────────────────────────┘
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
| **Fit param…** | Fit along current X (any Analyze model) → Y = one fit param → new `derived_fit` plot. Fit-along dim is dropped from Dimensions. Points that differ on a non-X dim (usually ROI) are separate series: legend `ROI 7`, Wong color, and a distinct marker. The Y name stays on the axis, not in the legend. |
| **Average…** | Multi-select dims → mean collapse → new plot. Works on **measurement cubes** (`derived_avg`) and on **Fit-param point plots** (average over ROI / scan / remaining axes; stays `derived_fit`). |

Nested Average (e.g. ROI then scan) keeps only dims still present on the source catalog; axes come from the reduced cube’s `key_sets`.

### Defaults (`default_plot_style` / `PlotViewState`)

- Style mode: **Scatter**
- Legend: **on**
- Error bars: **on**
- Journal: **App** (dark pyqtgraph). Line width **2 px**.
- Journal presets store `linewidth` in **points** and scale it to the widget so a 600 dpi export matches the physical size. See `JOURNAL_SPECS`.

### `PlotViewState` (on each `PlotDock.state`)

| Field | Role |
|-------|------|
| `kind` | `curves` \| `derived_fit` \| `derived_avg` \| `export` \| `2d` \| `heatmap` \| `batch` |
| `style` | `mode`, `journal` (`App`/`PRL`/`Nature`/`Optica`), `column`, `linewidth`. Optional `marker_mm` (journal) or `symbol_size` (App px) |
| `show_legend` / `show_errorbars` | View toggles |
| `xlabel` / `ylabel` | Blank = automatic axis title. Non-blank disables pyqtgraph SI prefix on that axis |
| `fitfunc` | Per-dock fit function name |
| `cached_xy` / `fit_overlays` | Export / snapshot redraw |
| `panel_series` | Batch vs-time multi-panel (+ optional `fit_x`/`fit_y`) |
| `derived_scan` / `derived_scans` | Reduced measurement cube(s) from Average… |
| `derived_points` | Fit-param points (`axis_vals` for later Average grouping) |
| `encoding` | `{plot_type, x_dim, y_dim?, y_channel, dims}` |
| `recipe` | Experiment persistence blob |

### Curves dock

- `closable=False` — cannot be dismissed from UI.
- Starts **hidden**. **Refresh** (or Load experiment) calls `_show_curves_dock()` + `_relayout_main_docks()`. Import alone does not.
- Central widget is 0×0 so Left/Right docks own the client area.

### Multi-scan overlay

- `self.scans`: members of the current experiment (derived docks are **not** listed).
- `selected_scan_indices()` is **every** loaded scan. Plot filtering is the Encoding scan dim, not the list selection. List selection is only for Delete.
- Colors: `OVERLAY_COLORS` for App. Journal presets use `WONG_COLORS` plus distinct markers and dash patterns.
- `primary_scan()` = `scans[0]`.

### Loaded scans (current experiment)

`list_scans` is visible in the Browser. The header is the zip name, or `Unsaved`. A trailing `*` means membership differs from the last Save/Load (`_membership_dirty` vs `_clean_scan_names`).

| Action | Effect |
|--------|--------|
| **Import** | Append scans. Does **not** redraw. Status bar says to Refresh. |
| **Delete** | Remove selected rows from `self.scans` only (disk files stay). Remap Encoding `dims["scan"]["values"]`. Does **not** redraw. Delete-all calls `clear_scans`. |
| **Refresh** (list) | `refresh_all_plots`: redraw Curves, then each other dock in place. |
| **Refresh** (plot) | `refresh_plot_dock`: that dock only. Keeps its Encoding, journal, axis labels, and fit function. Re-fits if a fit was already present. |

Curves refresh does **not** call `create_new_plot` (that resets Encoding). It clamps scan indices, rebuilds plots for the current X, then `draw_plot`.

Recipe docks (`export` / `2d` / `heatmap` / `batch`) refresh by replaying the recipe into the **same** dock (`_refreshing_dock` makes `show_plot_dock` swap the widget and `_record_tab_recipe` skip a new entry). A recipe whose `scan_name` / `scan_sel_names` are all gone is skipped with a status message. Derived docks redraw their stored copy; they are not recomputed from the new membership.

### Clear data…

Analyze → **Clear data…**: confirm → wipe loaded scans and close analysis docks.

Import picker: `ScanPickerDialog` with filter + sort (Name/Date).

- `discover_scan_dirs` is cached for the process (`_scan_dir_cache`). The dialog paints first, then fills. A second Import in the same process does not walk `data/` again.
- The picker's **Refresh** passes `refresh=True` and is the way to see scans written after the cache was filled. That is not the Loaded-scans Refresh.
- The filter waits 200 ms (`_filter_timer`) before rebuilding the list. Do not connect `textChanged` straight to `apply_filter_sort`.
- First open still walks the whole tree on the UI thread (about 2 s for ~10k folders). The list is a normal `QListWidget`, not a virtual model.

### Theme

- `APP_STYLESHEET` + `_style_plot_item`
- Images: viridis + `ColorBarItem`
- Legend click toggles curve **and** linked `ErrorBarItem` (`link_curve_errorbar`)
- Curves redraws (`_draw_plot_vs_axis` / `_vs_roi` / `_vs_time`) add every series inside `_batch_plot_items`: legend `updateSize` and view auto-range run **once** after the batch, then `ViewBox.autoRange()`.

`create_new_plot` and `_apply_curves_encoding_from_panel` call `draw_plot` only when `_sync_legacy_from_encoding` did not already draw via `set_x`. `set_x` returns whether a draw ran. Do not add a second unconditional `draw_plot()` after that bridge.

---

## Analyze features

### Fit (per dock)

- Fit / Clear / function combo on **that** dock only (`_dock_fit` → `perform_fit(..., dock=)`).
- Right-click Fit → `FittingDialog` (guess/fixed params).
- Guards: ≥2 finite points; `sanity_check_fit`; clipped fit y for autoRange.

### Style / axis labels

`StylePanel` (and the export/2D control bar) writes `style["journal"]`, `style["column"]`, `xlabel`, `ylabel`. Point size is on the Style panel only: millimetres for a journal (`style["marker_mm"]`), pixels for App (`style["symbol_size"]`, default 6). Changing the journal resets the spin to that journal's `marker_mm`. `marker_size_px` is what both the draw and the resize restyle use.

| Journal | Font | Text | Curve | Axis / ticks | Marker | Columns |
|---------|------|------|-------|--------------|--------|---------|
| PRL | Times New Roman | 9 pt (APS lettering ≥ 2 mm) | 1.0 pt | 0.75 pt frame, 4 pt ticks | 2.2 mm (≥ 2 mm) | 3.375 in; 2-col **7.0 in DERIVED** (REVTeX text width) |
| Nature | Arial | 7 pt (allowed 5–7) | 0.75 pt | 0.5 pt frame, 2.5 pt ticks | 1.8 mm | 89 mm, 130 mm (1.5), 183 mm |
| Optica | Arial | 8 pt | 1.0 pt | 0.75 pt frame, 3.5 pt ticks | 2.0 mm | 8.3 cm; 2-col **17.1 cm DERIVED** |

`App` stays the dark theme. On screen the dock still fills the window; `journal_paint_px` scales fonts, the frame, curves, and markers from the **view widget** width (not a stale `PlotItem.width()`), so a resize and a 600 dpi export land on those physical sizes. Journal ticks are major-only and point **inward** (`tickLength` negative); the frame and tick pens are non-cosmetic `FlatCap` (a round cap reads as a second tick). Curve and error-bar pens stay cosmetic on screen; Save figure boosts them by the export scale. Do not store journal line width as raw pixels. `AxisItem.setStyle` requires `tickLength` and `tickTextOffset` to be Python `int`.

Blank X/Y label keeps the automatic title (`_set_plot_label`). A typed label calls `enableAutoSIPrefix(False)`.

### Export / Copy / Save figure

- **Export**: binned curve → Export dock + TSV; copies style, axis labels, and fit prefs. Still means "snapshot", not a file.
- **Copy ▾**: image (Ctrl+C) / raw data (Ctrl+A) / **Save figure…**.
- **Copy image** (`_copy_plot_image`) writes the **plot** through the same `ImageExporter` width as Save figure, then puts that image on the clipboard. It does not grab the dock page (that included the Encoding sidebar).
- **Save figure…** (`save_figure` → `_write_figure`): PNG and LZW TIFF at column width × 600 dpi (`ImageExporter`); SVG (pyqtgraph, then Qt `QSvgGenerator` if that exporter throws); PDF via `QPdfWriter` + `widget.render`. App style uses the widget's pixel size, not a journal column.
- The legend ignores the export transform. `_boost_export_strokes` grows label px and the sample cell by `export_px / widget_px`, then `updateSize()`, so the rows do not pile up. `_place_legend_for_export` then puts the corner back: position is scaled by the exporter, the legend body is not, so subtracting the boosted width from the on-screen view box slides a bottom/right legend into the data. On-screen position is restored when the export undo runs.

### Legacy recipe-only paths (no main UI)

Still reachable from **experiment zip replay** and/or **Ctrl+Q** (2D dialog):

- `two_d_plot` + `TwoDPlotDialog`
- `generate_heatmap` + `HeatmapDialog`
- `batch_fit_selected` + `BatchAnalyzeDialog`

Prefer Encoding Type=`2D` / `ROI grid` and Derive Fit param… for new work. Do not delete these until recipe migration is explicit.

---

## Experiments (save / load)

Browser bottom: list `experiments/*.zip`; Save / Load / Refresh; double-click load; RMB Open / Duplicate / Delete / Reveal.

**Load** calls `open_experiment`. If `_membership_dirty()`, it asks before replacing the current list. Cancel leaves the unsaved scans in place.

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
    "style": {"mode": "Scatter", "linewidth": 2.0, "journal": "App", "column": "1-col"},
    "xlabel": "",
    "ylabel": "",
    "scan_sel_names": ["..."],
    "roi_sel": [0, 1]
  },
  "scans": [ { "name", "path", "metadata", "data" } ],
  "tabs": [ /* dock recipes */ ],
  "dock_layout": "<base64 QMainWindow.saveState>"
}
```

- Scans fully embedded (portable). `scan_sel_names` is the full membership at save time.
- Save drops recipes whose `scan_name` / `scan_sel_names` no longer match `self.scans`, and stamps each remaining recipe with that dock's `style`, `xlabel`, and `ylabel`.
- Recipes replayed by `_replay_tab_recipe` (`export` / `2d` / `heatmap` / `batch` / `derive`). A `derive` tab is rebuilt by `_replay_derive_recipe`: refit from `source_encoding`, `fitfunc`, `param`, and `x_dim` (points are not stored in the zip). `average` is skipped (the reduced cube is not in the zip). Old `derived_merge` / `derived_global` recipes are skipped.
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
| `import_data` / `ScanPickerDialog` / `discover_scan_dirs` | Append scans; directory list cached until the picker's Refresh |
| `_delete_loaded_scans` / `refresh_all_plots` / `refresh_plot_dock` | Membership edit and manual plot sync |
| `JOURNAL_SPECS` / `journal_paint_px` / `marker_size_px` / `_set_plot_label` | Journal figure geometry, point size, axis-label override |
| `save_figure` / `_write_figure` / `_copy_plot_image` | PNG, TIFF, SVG, PDF; Copy image uses the same raster |
| `_boost_export_strokes` / `_place_legend_for_export` | Export line width, legend row height, and corner anchor |
| `_replay_derive_recipe` | Restore a Fit-param dock from a saved `derive` recipe |
| `get_survival_rate` / `get_errorbars` / `_roi_data_totals` | Binned Y and which ROIs have data |
| `_batch_plot_items` | One legend layout + one auto-range per Curves redraw |
| `save_experiment` / `open_experiment` | Experiment zip I/O (v2); Load confirms if membership is dirty |
| `Fitter` / `sanity_check_fit` | Curve fitting |

---

## Known gaps / good next work

Only do these if the user asks:

1. Embed Average docks in the experiment zip. Fit-param docks already reload by refitting (`derive` recipe); the points themselves are still not stored.
2. Promote derived → Loaded scans (explicit action).
3. Full encoding shelf (explicit Color/Y channels).
4. Import dialog virtualization. Cache and filter debounce are in place; the **first** Import of ~10k folders is still a UI-thread walk (~2 s). A virtual list only matters if that first fill or scrolling the full list becomes the problem.
5. Drop recipe+Ctrl+Q for legacy `two_d_plot` / heatmap / batch once unused.
6. Optional module split if file size blocks work.
7. Title rename UX polish.

---

## Verification habits

1. Restart GUI after edits.
2. Smoke: Import → Refresh → Encoding XY Fit/Style → Journal + Point size + axis label → Type 2D → Type ROI grid → Fit param… (one legend entry per ROI) → Average… (incl. on Fit plot) → nested Average → Export → Copy image (plot only) → Save figure (legend stays in its corner) → Delete a scan → Refresh → Clear data → Load exp (confirm if dirty; a `derive` tab comes back).
3. Watch `TypeError: takes N positional arguments but M were given` on new button slots.
4. Prefer `logging` with scan name + ROI; status bar for user-facing results.
5. Run `_smoke_test_universal.py` offscreen after Derive/Average/Encoding, Loaded-scans, or figure-export changes. It also times Import cache (second open must not walk) and `create_new_plot` on every scan under `data/2026-10-10` and `data/2026-10-11` (one `draw_plot`). Wilson values must stay identical to a single `proportion_confint(..., method="wilson")` per bin. Later cases check journal column widths, that Delete does not call `draw_plot`, axis-label overrides, a PNG write, and that Load with a dirty list can be cancelled.

---

## Origin

Related historically to ARTIQ scan applets. This tool analyzes exported CSV pairs offline for lab presentation and multi-day comparison.
