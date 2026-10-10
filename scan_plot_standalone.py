"""
Data structures:
    self.metadata = {"key_sets": [], "points": [], "function": ""}
        dict containing the data comming in from the scan applet
        - keysets: list of names of the parameters that are being scanned by the scan applet
        used for labels on the x axis
        - points: list of tuples that contain the values of the scan parameters for each point in the scan
        used to sort the results into arrays where some parameters are constant in case of multi dimensional scan
        - function: name of the result function used to analyze the experiment result
    self.data
        list of floats or lists of floats (when rois are used) of the results of each scan point
        used for values on the y-axis
    self.plots:
        object of the Plots class
        contains a dict of Plot objects where the keys are tuples parameter values that are kept constant in the plot
        each entry of the dict is a list of plots for each roi
        Plot objects contain
            x: list of x data
            y: list of y data.
            fit: fit object from PlotWidget
            params: names of fit params
            popt: optimized values of fit parameters
"""

from PyQt6.QtWidgets import (
    QMainWindow, QDialog, QFileDialog, QApplication,
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QListWidget, QListWidgetItem, QAbstractItemView, QWidget,
    QSplitter, QCheckBox, QFrame, QSizePolicy, QTabWidget,
    QToolButton, QMenu, QInputDialog, QMessageBox, QRadioButton,
    QButtonGroup, QLineEdit, QDockWidget, QToolBar,
)
from PyQt6.QtGui import QFont, QImage, QAction
from PyQt6.QtCore import Qt, pyqtSignal, QByteArray, QTimer
from pyqtgraph import (
    ErrorBarItem, mkPen, mkBrush, PlotWidget, ImageItem, ColorBarItem,
    GraphicsLayoutWidget, DateAxisItem, colormap as pg_colormap,
)

import numpy as np
from scipy.stats import sem
from statsmodels.stats.proportion import proportion_confint
from scipy.cluster.hierarchy import fcluster, linkage

import logging
import io
import re
import copy
import json
import zipfile
import tempfile
import shutil
import base64
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import win32clipboard
from PIL import Image

# from artiq.applets.simple import SimpleApplet
# from sipyco.pc_rpc import Client

ROOT = Path(__file__).resolve().parent
FOLDER = ROOT / "data"
EXPERIMENTS_DIR = ROOT / "experiments"

THEME = {
    "bg": "#0b0e14",
    "panel": "#12161f",
    "grid": "#2a3344",
    "text": "#d7dce6",
    "muted": "#8b93a7",
    "accent": "#f0a15a",
}
font = {"font-size": "13pt", "color": THEME["text"]}
style_sheet = {
    "pen": None,
    "symbol": "o",
    "symbolSize": 7,
    "symbolPen": None,
    "symbolBrush": (86, 182, 194),
}

OVERLAY_COLORS = [
    (86, 182, 194),
    (240, 161, 90),
    (122, 162, 247),
    (187, 154, 247),
    (158, 206, 106),
    (247, 118, 142),
    (125, 207, 255),
    (224, 175, 104),
]

APP_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {THEME["bg"]};
    color: {THEME["text"]};
}}
QDockWidget::title {{
    background: {THEME["panel"]};
    padding: 4px 8px;
    border: 1px solid {THEME["grid"]};
}}
QToolBar {{
    background: {THEME["panel"]};
    border-bottom: 1px solid {THEME["grid"]};
    spacing: 4px;
    padding: 3px;
}}
QPushButton, QToolButton {{
    background: {THEME["panel"]};
    color: {THEME["text"]};
    border: 1px solid {THEME["grid"]};
    border-radius: 10px;
    padding: 4px 10px;
}}
QPushButton:hover, QToolButton:hover {{
    border-color: {THEME["muted"]};
}}
QComboBox, QLineEdit, QDoubleSpinBox, QListWidget, QScrollBar {{
    background: {THEME["panel"]};
    color: {THEME["text"]};
    border: 1px solid {THEME["grid"]};
    border-radius: 6px;
}}
QCheckBox, QLabel {{
    color: {THEME["text"]};
}}
QMenu {{
    background: {THEME["panel"]};
    color: {THEME["text"]};
    border: 1px solid {THEME["grid"]};
}}
QMenu::item:selected {{
    background: {THEME["grid"]};
}}
"""

STYLE_MODES = ("Scatter", "Line", "Line+marker")


def default_plot_style():
    return {
        "mode": "Scatter",
        "linewidth": 2.0,
        "symbol_size": 6,
        "show_errorbars": True,
    }


def plot_kwargs_from_style(color, style=None):
    style = style or default_plot_style()
    mode = style.get("mode", "Scatter")
    lw = float(style.get("linewidth", 2.0))
    ss = int(style.get("symbol_size", 6))
    pen = mkPen(color, width=lw) if mode in ("Line", "Line+marker") else None
    if mode == "Line":
        return {"pen": pen, "symbol": None}
    return {
        "pen": pen,
        "symbol": "o",
        "symbolSize": ss,
        "symbolPen": None,
        "symbolBrush": mkBrush(*color),
    }


def link_curve_errorbar(curve, error_bar):
    if curve is None or error_bar is None:
        return
    original = curve.setVisible

    def setVisible(visible, _orig=original, _eb=error_bar):
        _orig(visible)
        _eb.setVisible(visible)

    curve.setVisible = setVisible


def sanity_check_fit(fitfunc, popt, x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 2:
        return "not enough points"
    xspan = float(np.nanmax(x) - np.nanmin(x)) or 1.0
    yspan = float(np.nanmax(y) - np.nanmin(y)) or 1.0
    yspan = max(yspan, abs(float(np.nanmean(y))) * 0.1 + 1e-12)
    popt = np.asarray(popt, dtype=float)
    if not np.all(np.isfinite(popt)):
        return "non-finite parameters"
    if fitfunc in ("Gauss", "Lorentz"):
        amp, x0, width, _y0 = popt[:4]
        if abs(amp) > 50 * yspan:
            return "amplitude extreme"
        if width <= 0 or width > 5 * xspan:
            return "width extreme"
        if x0 < np.nanmin(x) - xspan or x0 > np.nanmax(x) + xspan:
            return "center outside data"
    elif fitfunc in ("Sine", "DampSine"):
        if abs(popt[0]) > 50 * yspan:
            return "amplitude extreme"
        if abs(popt[1]) > 50 / max(xspan, 1e-12):
            return "frequency extreme"
    elif fitfunc == "Exp_decay":
        if abs(popt[0]) > 50 * yspan:
            return "amplitude extreme"
        if abs(popt[1]) > 100 / max(xspan, 1e-12):
            return "decay extreme"
    return None

from numpy import array


@dataclass
class ScanDataset:
    name: str
    path: Path
    metadata: dict
    data: list
    plots: object = None


@dataclass
class PlotViewState:
    title: str
    kind: str  # curves|export|2d|heatmap|batch|derived_merge|derived_global
    style: dict = field(default_factory=default_plot_style)
    show_legend: bool = True
    show_errorbars: bool = True
    x_index: int | None = None
    slice: int | None = None
    fitfunc: str | None = None
    series_items: list = field(default_factory=list)
    recipe: dict | None = None
    derived_scan: object = None
    cached_xy: object = None  # (x, y, heights, y_centers) for snapshot docks
    # Snapshot-dock fit overlays: list of {x, y, color}
    fit_overlays: list = field(default_factory=list)
    # Multi-panel batch (vs time): list of {plot_item, x, y, yerr, label, fit_x?, fit_y?}
    panel_series: list = field(default_factory=list)


class PlotDock(QDockWidget):
    """Floatable/dockable plot panel with per-dock view state."""

    activated = pyqtSignal(object)
    closed = pyqtSignal(object)

    def __init__(self, title, kind, widget, parent=None, closable=True):
        super().__init__(title, parent)
        self.state = PlotViewState(title=title, kind=kind)
        self.setObjectName(f"PlotDock_{kind}_{id(self)}")
        self.setWidget(widget)
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        feats = (
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        if closable:
            feats |= QDockWidget.DockWidgetFeature.DockWidgetClosable
        self.setFeatures(feats)
        self._closable = closable
        self.plot_widget = widget

    def closeEvent(self, event):
        if not self._closable:
            self.hide()
            event.ignore()
            return
        self.closed.emit(self)
        super().closeEvent(event)


def discover_scan_dirs(root: Path):
    """Return dirs under root that contain both log.csv and logResults.csv."""
    root = Path(root)
    if not root.is_dir():
        return []
    found = []
    for dirpath, _dirnames, filenames in os_walk_safe(root):
        names = set(filenames)
        if "log.csv" in names and "logResults.csv" in names:
            found.append(Path(dirpath))
    return sorted(found)


def os_walk_safe(root: Path):
    import os
    return os.walk(root)


def parse_scan_timestamp(path: Path):
    """Parse YYYY-MM-DD + scanHHMMSS from path; return datetime or None."""
    parts = Path(path).parts
    date_str = None
    time_str = None
    for part in parts:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            date_str = part
        m = re.fullmatch(r"scan(\d{6})", part)
        if m:
            time_str = m.group(1)
    if date_str and time_str:
        try:
            return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M%S")
        except ValueError:
            return None
    return None


def parse_points(line):
    return [float(v) for v in line.split(", ")]


def parse_bracketed_points(line):
    points = []
    for chunk in line.split("],"):
        chunk = chunk.strip().lstrip("[").rstrip("]")
        if chunk:
            x, y = chunk.split(",")
            points.append([float(x), float(y)])
    return points


def load_scan_pair(log_path, results_path, name=None):
    """Load a log.csv + logResults.csv pair into a ScanDataset."""
    log_path = Path(log_path)
    results_path = Path(results_path)
    metadata = {"key_sets": [], "points": [], "function": ""}
    data = []

    with open(log_path, "r", encoding="utf-8") as f:
        header = f.readline()
        if "Module:" in header:
            metadata["key_sets"] = [s.strip().split(".") for s in header.strip().split(", ")]
            for line in f:
                line = line.strip()
                if line:
                    metadata["points"].append(parse_points(line))
        elif header.strip() == "":
            for line in f:
                line = line.strip()
                if line:
                    metadata["points"].append(parse_points(line))
        else:
            # unexpected: treat remaining as points without key_sets
            if header.strip():
                metadata["points"].append(parse_points(header.strip()))
            for line in f:
                line = line.strip()
                if line:
                    metadata["points"].append(parse_points(line))

    with open(results_path, "r", encoding="utf-8") as f:
        header = f.readline()
        metadata["function"] = header.strip()
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("["):
                data.append(parse_bracketed_points(line))
            else:
                data.append(parse_points(line))

    n_pts = len(metadata["points"])
    n_res = len(data)
    if n_res == 0:
        raise ValueError(f"No results in {results_path}")
    if n_pts != n_res:
        # Incomplete / in-progress scans write all planned points first; keep overlapping prefix
        n = min(n_pts, n_res)
        logging.warning(
            "Truncating %s to %s points (log had %s, results had %s)",
            log_path.parent, n, n_pts, n_res,
        )
        metadata["points"] = metadata["points"][:n]
        data = data[:n]

    if name is None:
        try:
            name = str(log_path.parent.relative_to(FOLDER))
        except ValueError:
            name = log_path.parent.name

    return ScanDataset(name=name, path=log_path.parent, metadata=metadata, data=data)


class ScanPickerDialog(QDialog):
    """Multi-select scan folders that contain log.csv + logResults.csv."""

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select scan folders")
        self.resize(560, 480)
        self.root = Path(root)
        self._all_dirs = []
        self._csv_pair = None

        layout = QVBoxLayout(self)
        path_row = QHBoxLayout()
        self.root_label = QLabel(str(self.root))
        path_row.addWidget(self.root_label, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self.browse_root)
        path_row.addWidget(browse_btn)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        path_row.addWidget(refresh_btn)
        layout.addLayout(path_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("path substring…")
        self.ed_filter.textChanged.connect(self.apply_filter_sort)
        filter_row.addWidget(self.ed_filter, stretch=1)
        filter_row.addWidget(QLabel("Sort:"))
        self.cob_sort = QComboBox()
        self.cob_sort.addItems(["Name ↑", "Name ↓", "Date ↑", "Date ↓"])
        self.cob_sort.setCurrentIndex(3)  # newest first
        self.cob_sort.currentIndexChanged.connect(self.apply_filter_sort)
        filter_row.addWidget(self.cob_sort)
        layout.addLayout(filter_row)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget)
        self.lb_count = QLabel("")
        layout.addWidget(self.lb_count)

        btn_row = QHBoxLayout()
        csv_btn = QPushButton("Pick CSV pair…")
        csv_btn.clicked.connect(self.pick_csv_pair)
        btn_row.addWidget(csv_btn)
        btn_row.addStretch(1)
        ok_btn = QPushButton("Load")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def browse_root(self):
        path = QFileDialog.getExistingDirectory(self, "Scan root folder", str(self.root))
        if path:
            self.root = Path(path)
            self.root_label.setText(str(self.root))
            self.refresh()

    def refresh(self):
        self._csv_pair = None
        self._all_dirs = discover_scan_dirs(self.root)
        self.apply_filter_sort()

    def apply_filter_sort(self):
        needle = self.ed_filter.text().strip().lower()
        rows = []
        for d in self._all_dirs:
            try:
                label = str(d.relative_to(self.root))
            except ValueError:
                label = str(d)
            if needle and needle not in label.lower() and needle not in str(d).lower():
                continue
            ts = parse_scan_timestamp(d)
            rows.append((label, d, ts))

        mode = self.cob_sort.currentIndex()
        if mode in (0, 1):
            rows.sort(key=lambda r: r[0].lower(), reverse=(mode == 1))
        else:
            # Date: missing timestamps sort last
            rows.sort(
                key=lambda r: r[2] or datetime.min,
                reverse=(mode == 3),
            )

        self.list_widget.clear()
        for label, d, _ts in rows:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(d))
            self.list_widget.addItem(item)
        self.lb_count.setText(f"{len(rows)} / {len(self._all_dirs)} scans")

    def pick_csv_pair(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Pick log and logResults file",
            str(self.root),
            "CSV Data File (*.csv)",
        )
        if len(files) != 2:
            logging.error("Exactly 2 files need to be selected: log.csv and logResults.csv")
            return
        self._csv_pair = files
        self.accept()

    def selected_scan_dirs(self):
        if self._csv_pair is not None:
            return None
        return [Path(item.data(Qt.ItemDataRole.UserRole)) for item in self.list_widget.selectedItems()]

    def selected_csv_pair(self):
        return self._csv_pair

class Plots:
    """"class to hold plot data, and plot objects"""
    def __init__(self, x):
        self.x_index = x
        self.n_rois = 1
        self.plots = {}
    
    def set_n_rois(self, n):
        self.n_rois = n

    def add_plot(self, point):
        t = self.create_tuple(point)
        if t not in self.plots:
            self.plots[t] = [self.Plot() for _ in range(self.n_rois)]
    
    def resort_keys(self):
        self.plots = dict(sorted(self.plots.items()))

    def create_tuple(self, point):
        return tuple(point[:self.x_index] + point[self.x_index+1:])

    def n_plots(self):
        return len(self.plots.keys())
    
    def add_point(self, point, y_vals):
        t = self.create_tuple(point)
        for i, y in enumerate(y_vals):
            self.plots[t][i].add_point(point[self.x_index], y)

    def get_data(self, index, roi):
        plot = list(self.plots.values())[index][roi]
        x = plot.x
        y = plot.y        
        return array(x), array(y)

    def add_fit(self, index, roi, fit, params, popt, perr):
        plot = list(self.plots.values())[index][roi]
        plot.add_fit(fit, params, popt, perr)

    def remove_fit(self, index, roi):
        plot = list(self.plots.values())[index][roi]
        plot.remove_fit()
    
    def get_fit(self, index, roi):
        plot = list(self.plots.values())[index][roi]
        return plot.get_fit()

    class Plot:
        def __init__(self, x=[], y=[]):
            self.y = x.copy()
            self.x = y.copy()

            #fittings
            self.fit = None
            self.params = None
            self.popt = None
            self.perr = None
        
        def add_point(self, x, y):
            self.y.append(y)
            self.x.append(x)

        def add_fit(self, fit, params, popt, perr):
            self.fit = fit
            self.params = params
            self.popt = popt
            self.perr = perr
        
        def remove_fit(self):
            self.fit = None
            self.params = None
            self.popt = None
            self.perr = None
        
        def get_fit(self):
            return self.fit, self.params, self.popt, self.perr
        

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QComboBox, QPushButton, QHBoxLayout
class ParameterDialog(QDialog):
    def __init__(self, fit_options, x_options, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Plot Fit Parameter")
        self.setModal(True)
        
        main_layout = QVBoxLayout()
        
        dropdown_layout = QHBoxLayout()
        dropdown_layout.addWidget(QLabel("Fit parameter:"))
        self.cob_fit = QComboBox()
        self.cob_fit.addItems(fit_options)
        dropdown_layout.addWidget(self.cob_fit)
        
        dropdown_layout.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.addItems(x_options)
        dropdown_layout.addWidget(self.x_combo)
        
        main_layout.addLayout(dropdown_layout)
        
        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_button)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)
        
        main_layout.addLayout(button_layout)
        
        self.setLayout(main_layout)
    
    def get_values(self):
        return self.cob_fit.currentIndex(), self.x_combo.currentIndex()


class BatchAnalyzeDialog(QDialog):
    """Batch fit / Fit-param modes: vs time, vs scan axis, vs ROI."""

    MODE_TIME = "vs_time"
    MODE_AXIS = "vs_axis"
    MODE_ROI = "vs_ROI"

    def __init__(self, param_names, scan_labels, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch analyze")
        self.setModal(True)
        self.resize(380, 420)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("X axis mode:"))
        self._mode_group = QButtonGroup(self)
        self.rb_time = QRadioButton("vs scan time (all selected scans)")
        self.rb_axis = QRadioButton("vs secondary scan axis (one scan)")
        self.rb_roi = QRadioButton("vs ROI (one scan, current slice)")
        self.rb_time.setChecked(True)
        for i, rb in enumerate((self.rb_time, self.rb_axis, self.rb_roi)):
            self._mode_group.addButton(rb, i)
            layout.addWidget(rb)

        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Target scan (axis / ROI modes):"))
        self.cob_scan = QComboBox()
        self.cob_scan.addItems(scan_labels or ["(none)"])
        self.cob_scan.setEnabled(bool(scan_labels))
        scan_row.addWidget(self.cob_scan, stretch=1)
        layout.addLayout(scan_row)

        def _sync_scan_enabled():
            self.cob_scan.setEnabled(
                (self.rb_axis.isChecked() or self.rb_roi.isChecked()) and bool(scan_labels)
            )
        self.rb_time.toggled.connect(_sync_scan_enabled)
        self.rb_axis.toggled.connect(_sync_scan_enabled)
        self.rb_roi.toggled.connect(_sync_scan_enabled)
        _sync_scan_enabled()

        layout.addWidget(QLabel("Parameters to plot:"))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for name in param_names:
            item = QListWidgetItem(name)
            self.list_widget.addItem(item)
            item.setSelected(True)
        layout.addWidget(self.list_widget)

        sel_row = QHBoxLayout()
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(self.select_all)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.list_widget.clearSelection)
        sel_row.addWidget(all_btn)
        sel_row.addWidget(clear_btn)
        sel_row.addStretch(1)
        layout.addLayout(sel_row)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def select_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setSelected(True)

    def selected_params(self):
        return [item.text() for item in self.list_widget.selectedItems()]

    def selected_mode(self):
        if self.rb_axis.isChecked():
            return self.MODE_AXIS
        if self.rb_roi.isChecked():
            return self.MODE_ROI
        return self.MODE_TIME

    def selected_scan_index(self):
        """Index into the scan_labels list passed to __init__ (0-based)."""
        return max(0, self.cob_scan.currentIndex())


from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtGui import QIntValidator


class HeatmapDialog(QDialog):
    def __init__(self, fit_options, n_rois, scan_labels=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Heatmap")
        self.setModal(True)

        main_layout = QVBoxLayout()

        scan_labels = scan_labels or []
        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Scan:"))
        self.cob_scan = QComboBox()
        self.cob_scan.addItems(scan_labels if scan_labels else ["(none)"])
        self.cob_scan.setEnabled(bool(scan_labels))
        scan_row.addWidget(self.cob_scan, stretch=1)
        main_layout.addLayout(scan_row)

        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("Fit parameter:"))
        self.cob_fit = QComboBox()
        self.cob_fit.addItems(fit_options)
        top_layout.addWidget(self.cob_fit)
        top_layout.addWidget(QLabel("N rois:"))
        self.n_rois_label = QLabel(str(n_rois))
        top_layout.addWidget(self.n_rois_label)
        main_layout.addLayout(top_layout)

        grid_layout = QHBoxLayout()
        int_validator = QIntValidator(0, 10**9, self)
        grid_layout.addWidget(QLabel("Rows:"))
        self.rows_edit = QLineEdit()
        self.rows_edit.setValidator(int_validator)
        self.rows_edit.setText(str(max(1, int(n_rois**0.5))))
        grid_layout.addWidget(self.rows_edit)
        grid_layout.addWidget(QLabel("Cols:"))
        self.cols_edit = QLineEdit()
        self.cols_edit.setValidator(int_validator)
        self.cols_edit.setText(str(max(1, int(np.ceil(n_rois / max(1, int(n_rois**0.5)))))))
        grid_layout.addWidget(self.cols_edit)
        main_layout.addLayout(grid_layout)

        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)
        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

    def get_values(self):
        return (
            self.cob_fit.currentIndex(),
            int(self.rows_edit.text()) if self.rows_edit.text() else None,
            int(self.cols_edit.text()) if self.cols_edit.text() else None,
            max(0, self.cob_scan.currentIndex()),
        )

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QGridLayout, QLabel, QLineEdit,
    QCheckBox, QComboBox, QDialogButtonBox, QPushButton,
    QHBoxLayout, QWidget
)
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtCore import QRegularExpression

class FittingDialog(QDialog):
    def __init__(self, func_dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fit Parameters")
        self.func_dict = func_dict

        self.main_layout = QVBoxLayout(self)

        # Dropdown for function selection
        self.func_selector = QComboBox()
        self.func_selector.addItems(list(self.func_dict.keys()) + ['Custom'])
        self.func_selector.currentTextChanged.connect(self.update_params_layout)
        self.main_layout.addWidget(self.func_selector)

        # === Custom function widgets ===
        self.custom_widget = QWidget()
        self.custom_layout = QVBoxLayout(self.custom_widget)

        # Function input field
        func_input_layout = QHBoxLayout()
        self.custom_func_label = QLabel("f(x) =")
        self.custom_func_input = QLineEdit()
        self.custom_func_input.setPlaceholderText("e.g. p0 * np.sin(p1 * x + p2) + p3")
        func_input_layout.addWidget(self.custom_func_label)
        func_input_layout.addWidget(self.custom_func_input)
        self.custom_layout.addLayout(func_input_layout)

        # Add parameter button
        self.add_param_button = QPushButton("Add Parameter")
        self.add_param_button.clicked.connect(self.add_custom_param)
        self.custom_layout.addWidget(self.add_param_button)

        self.main_layout.addWidget(self.custom_widget)

        # === Parameter grid ===
        self.param_grid = QGridLayout()
        self.main_layout.addLayout(self.param_grid)

        # Dialog buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.fit_button = self.button_box.addButton("Fit", QDialogButtonBox.ButtonRole.AcceptRole)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.main_layout.addWidget(self.button_box)

        # Storage
        self.line_edits = {}
        self.checkboxes = {}

        # Initial load
        self.update_params_layout(self.func_selector.currentText())

    def update_params_layout(self, func_name):
        # Clear old parameter rows
        while self.param_grid.count():
            item = self.param_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        self.line_edits.clear()
        self.checkboxes.clear()

        # Header row
        self.param_grid.addWidget(QLabel("Parameter"), 0, 0)
        self.param_grid.addWidget(QLabel("Guess Value"), 0, 1)
        self.param_grid.addWidget(QLabel("Fix"), 0, 2)

        if func_name == "Custom":
            self.custom_widget.show()
        else:
            self.custom_widget.hide()
            params = self.func_dict[func_name]["params"]
            for i, param in enumerate(params, start=1):
                self._add_param_row(i, param, placeholder="auto")

    def _add_param_row(self, row, param_name, placeholder="auto"):
        label = QLabel(param_name)
        line_edit = QLineEdit()
        line_edit.setPlaceholderText(placeholder)

        # Allow float-like values with optional exponent and sign
        regex = QRegularExpression(r"^-?\d*\.?\d+([eE][-+]?\d+)?$|^$")
        validator = QRegularExpressionValidator(regex)
        line_edit.setValidator(validator)

        checkbox = QCheckBox()
        checkbox.toggled.connect(lambda checked, le=line_edit, ph=placeholder: self._update_placeholder_on_fix(checked, le, ph))

        self.param_grid.addWidget(label, row, 0)
        self.param_grid.addWidget(line_edit, row, 1)
        self.param_grid.addWidget(checkbox, row, 2)

        self.line_edits[param_name] = line_edit
        self.checkboxes[param_name] = checkbox
    
    def _update_placeholder_on_fix(self, checked, line_edit, original_placeholder):
        if checked:
            line_edit.setPlaceholderText("0")
        else:
            line_edit.setPlaceholderText(original_placeholder)

    def add_custom_param(self):
        new_param = f"p{len(self.line_edits)}"
        row = self.param_grid.rowCount()
        self._add_param_row(row, new_param, placeholder="1")

    def get_results(self):
        func = self.func_selector.currentText()
        values = [float(self.line_edits[param].text()) if self.line_edits[param].text() else 
                  0 if self.checkboxes[param].isChecked() else None for param in self.line_edits]
        fixed = [self.checkboxes[param].isChecked() for param in self.checkboxes]
        custom_expr = self.custom_func_input.text() if func == "Custom" else None
        return func, values, fixed, custom_expr

import numpy as np
from scipy.optimize import curve_fit
from skimage.feature import blob_log

def two_d_gaussian(xy, amplitude, xo, yo, sigma_x, sigma_y, theta, offset):
    """2d gaussian (angled) used for fitting the MOT

    Args:
        xy (tuple of arrays): x,y coordinates
        amplitude (float): 
        xo (float): center x
        yo (float): center y
        sigma_x (float): 
        sigma_y (float): 
        theta (float): angle with y axis
        offset (float): background

    Returns:
        1d array: gaussian raveled to 1d
    """

    x, y = xy
    xo = float(xo)
    yo = float(yo)  

    # compute 2d gaussian fuction  
    a = (np.cos(theta)**2)/(2*sigma_x**2) + (np.sin(theta)**2)/(2*sigma_y**2)
    b = -(np.sin(2*theta))/(4*sigma_x**2) + (np.sin(2*theta))/(4*sigma_y**2)
    c = (np.sin(theta)**2)/(2*sigma_x**2) + (np.cos(theta)**2)/(2*sigma_y**2)
    g = offset + amplitude*np.exp(-(a*((x-xo)**2) + 2*b*(x-xo)*(y-yo) + c*((y-yo)**2)))

    # return raveled for fit
    return g.ravel()

def donut(xy, amplitude, xo, yo, sigma, r, offset):

    x, y = xy
    xo = float(xo)
    yo = float(yo)  

    g = offset + amplitude*np.exp(-(np.sqrt((x-xo)**2 + (y-yo)**2)-r)**2/sigma**2)

    return g.ravel()

def elliptical_donut(xy, amplitude, xo, yo, sigma, r_x, r_y, theta, offset):

    x, y = xy
    xo = float(xo)
    yo = float(yo)

    x_shift = x - xo
    y_shift = y - yo

    cos_t = np.cos(theta)
    sin_t = np.sin(theta)

    x_rot =  cos_t * x_shift + sin_t * y_shift
    y_rot = -sin_t * x_shift + cos_t * y_shift

    r_ell = np.sqrt((x_rot / r_x)**2 + (y_rot / r_y)**2)

    g = offset + amplitude * np.exp(-((r_ell - 1)**2) / sigma**2)

    return g.ravel()

def gaussian(x, a, b, c, d):
    """amp * exp(-(x - x0)^2 / (2 * sig^2)) + y0"""
    return a * np.exp(-(x - b)**2 / (2 * c**2)) + d

def lorentz(x, a, b, c, d):
    """amp * HWHM^2 / ((x - x0)^2 + HWHM^2) + y0"""
    return a * c**2 / ((x - b)**2 + c**2) + d

def sine(t, a, f, phi, d):
    """amp * sin(2 * pi * freq * x + phi0) + y0"""
    return a * np.sin(2*np.pi*f*t + phi) + d

def exp_decay(t, a, gamma, d):
    """amp * exp(-gamma * x) + y0"""
    return a * np.exp(-gamma * t) + d

def damp_sine(t, a, f, phi, d, gamma):
    """amp * exp(-gamma * x) * sin(2 * pi * freq * x + phi0) + y0"""
    return sine(t, 0.5, f, phi, 0) * exp_decay(t, a, gamma, 0) + d

def polynomial(x, *args):
    """sum(p_i * x^i)"""
    return sum(coeff * (x ** i) for i, coeff in enumerate(args))

class Fitter():
    def __init__(self):
        self.p0 = []
        self.popt = []

        self.func_dict = {
            'Gauss' : {'f': gaussian, 'params' : ['amp', 'x0', 'sig', 'y0'], "guess": self.guess_peak},
            'Lorentz' : {'f': lorentz, 'params' : ['amp', 'x0', 'HWHM', 'y0'], "guess": self.guess_peak},
            'Sine' : {'f': sine, 'params': ['amp','freq','phi0','y0'], "guess": self.guess_oscillation},
            'DampSine' : {'f': damp_sine, 'params':['amp','freq','phi0','y0','gamma'], "guess": self.guess_oscillation},
            'Exp_decay' : {'f': exp_decay, 'params':['amp','gamma','y0'], "guess": self.guess_decay},
            'Average': {'f': polynomial, 'params' : ['p0'], "guess": self.guess_poly(0)},
            'Linear' : {'f': polynomial, 'params' : ['p0', 'p1'], "guess": self.guess_poly(1)},
            'Quadratic' : {'f': polynomial, 'params': ['p0', 'p1', 'p2'], "guess": self.guess_poly(2)},
            'Cubic': {'f': polynomial, 'params': ['p0', 'p1', 'p2', 'p3'], "guess": self.guess_poly(3)}
            }

    def guess_peak(self, x, y):
        #amplitude a, peak position b, peak width (sigma) c, offset d
        #find out the most probable sign of the peak
        xrange = x.max() - x.min()
        yrange = y.max() - y.min()
        if y.mean() > yrange/2:
            #most of the data are above the half range, so probably a is negative
            d = y.max()
            ib = np.argmin(y)
            b = x[ib]
            a = -1*yrange
            c = xrange/10 #assuming a decent range around the peak
        else:
            d = y.min()
            ib = np.argmax(y)
            b = x[ib]
            a = yrange
            c = xrange/10
        return [a, b, c, d]

    def guess_oscillation_fft(self, x, y):
        # guess the parameters for an oscillatory signal: 
        # amplitude a, freuency f, phase phi, and offset d\
        x_space = min(np.diff(np.sort(x)))# don't assume uniform spacing
        ff = np.fft.fftfreq(len(x), x_space)   
        # ff = np.fft.fftfreq(len(self.xdata), (self.xdata[1]-self.xdata[0]))   # assume uniform spacing
        Fyy = abs(np.fft.fft(y))
        Fyy[ff > 1/x_space] = 0 #exclude everything bigger than the sampling rate
        f = abs(ff[np.argmax(Fyy[1:])+1])   # excluding the zero frequency "peak", which is related to offset
        a = np.std(y) * 2.**0.5
        d = np.mean(y)
        return [a, 2.*np.pi*f, 0., d]

    def guess_oscillation(self, x, y):
        # guess the parameters for an oscillatory signal: 
        # amplitude a, freuency f, phase phi, and offset d
        yrng = y.max() - y.min()
        xrng = x.max() - x.min()

        a = yrng/2*1.5
        d = 0.5*(y.max() + y.min())
        x_max = x[y.argmax()]
        x_min = x[y.argmin()]
        if x_max < x_min:
            #we first encounter a peak, guess phase of 0
            phi = 0
        else:
            #we first encounter a valley, guess phase of pi
            phi = np.pi
        #find the number of zero crossings:
        crossing = []
        for i in y:
            if abs(i-d) < 0.2*a:
                crossing.append(1)
            else:
                crossing.append(0)
        crossing = np.array(crossing)
        # find rising edges by looking where the diff of crossing equals +1, count the number of these edges and divide by 2
        N_periods = np.sum(np.diff(crossing) == 1)/2
        f = N_periods/xrng
        #assemble the fit guess
        return [a, f, phi, d]

    def guess_decay(self, x, y):
        # we have to guess a start value, t0, asymptotic value, and decay rate
        yrng = y.max() - y.min()
        xrng = x.max() - x.min()
        i0 = x.argmin()
        i_end = x.argmax()
        y0 = y[i_end] #assumes the x parameter represents time and decay happens towards the high x values!
        if y[i0] > y0:
            amp = yrng
        else:
            amp = -1*yrng
        gamma = 2/xrng #guess half the range for the decay time
        return [amp, gamma, y0]

    def guess_poly(self, n):
        def guess(x, y):
            p = np.polyfit(x, y, deg=n)
            return list(p)
        return guess

    # def guess_fidelity(self):
    #     # we have to guess a start value, t0, asymptotic value, and decay rate
    #     yrng = self.ydata.max() - self.ydata.min()
    #     xrng = self.xdata.max() - self.xdata.min()
    #     i0 = self.xdata.argmin()
    #     i_end = self.xdata.argmax()
    #     amp = yrng
    #     gamma = 2/xrng #guess half the range for the decay time
    #     x0 = self.xdata.min()
    #     self.p0 = [0.05,1]

    def fix_params(self, func, fixed_params):
        def wrapper(x, *free_params):
            full_params = []
            free_i = 0
            total_params = len(fixed_params) + len(free_params)

            for i in range(total_params):
                if i in fixed_params:
                    full_params.append(fixed_params[i])
                else:
                    full_params.append(free_params[free_i])
                    free_i += 1

            return func(x, *full_params)

        return wrapper

    def perform_fit(self, x, y, fitfunc, yerr=None, guess=None, fixed=None):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]
        if len(x) < 2:
            raise ValueError(f"need ≥2 finite points for fit, got {len(x)}")
        p0 = self.func_dict[fitfunc]['guess'](x, y)

        #meh
        if fitfunc == "DampSine":
            p0.append(0)

        # apply manual guesses
        if guess is not None:
            for i in range(len(guess)):
                if guess[i] is not None:
                    p0[i] = guess[i]

        #fix certain parameters
        func = self.func_dict[fitfunc]['f'] 
        if fixed is not None:
            p0 = np.array(p0)[[not b for b in fixed]]
            fixed_params = {i: val for i, (val, is_fixed) in enumerate(zip(guess, fixed)) if is_fixed}
            func = self.fix_params(func, fixed_params)        
        
        # execute fit
        if len(p0) == 0:
            popt, perr = [], []
        else:
            try:
                popt, pcov = curve_fit(func, x, y, p0, sigma=yerr)
            except RuntimeError:
                try:
                    popt, pcov = curve_fit(func, x, y, [1] * len(p0), sigma=yerr)
                except Exception as exc:
                    raise ValueError(f"curve_fit failed: {exc}") from exc
            perr = np.sqrt(np.diag(pcov))

        # insert fixed parameters back
        if fixed is not None:
            for i in range(len(fixed)):
                if fixed[i]:
                    popt = np.insert(popt, i, guess[i])
                    perr = np.insert(perr, i, 0)

        reason = sanity_check_fit(fitfunc, popt, x, y)
        if reason:
            raise ValueError(f"rejected extreme fit ({reason})")

        xi = np.linspace(x.min(), x.max(), 200)
        yi = self.func_dict[fitfunc]['f'](xi, *popt)
        # Keep fit curve within a padded data y-range so autoRange is not blown out
        ypad = 0.05 * (float(np.nanmax(y) - np.nanmin(y)) or 1.0)
        ymin, ymax = float(np.nanmin(y)) - ypad, float(np.nanmax(y)) + ypad
        yi = np.clip(yi, ymin - 10 * ypad, ymax + 10 * ypad)
        return xi, yi, popt, perr

# Form implementation generated from reading ui file 'scan_plot.ui'
#
# Created by: PyQt6 UI code generator 6.11.0
#
# WARNING: Any manual changes made to this file will be lost when pyuic6 is
# run again.  Do not edit this file unless you know what you are doing.


from PyQt6 import QtCore, QtGui, QtWidgets
from pyqtgraph import PlotWidget

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        MainWindow.resize(663, 459)
        self.centralwidget = QtWidgets.QWidget(parent=MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        self.gridLayout = QtWidgets.QGridLayout(self.centralwidget)
        self.gridLayout.setObjectName("gridLayout")
        self.pw_plot = PlotWidget(parent=self.centralwidget)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Expanding)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.pw_plot.sizePolicy().hasHeightForWidth())
        self.pw_plot.setSizePolicy(sizePolicy)
        self.pw_plot.setObjectName("pw_plot")
        self.gridLayout.addWidget(self.pw_plot, 2, 0, 1, 5)
        self.cob_action = QtWidgets.QComboBox(parent=self.centralwidget)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.cob_action.sizePolicy().hasHeightForWidth())
        self.cob_action.setSizePolicy(sizePolicy)
        self.cob_action.setObjectName("cob_action")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.cob_action.addItem("")
        self.gridLayout.addWidget(self.cob_action, 5, 0, 1, 1)
        self.scrollbar = QtWidgets.QScrollBar(parent=self.centralwidget)
        self.scrollbar.setEnabled(True)
        self.scrollbar.setPageStep(1)
        self.scrollbar.setOrientation(QtCore.Qt.Orientation.Horizontal)
        self.scrollbar.setObjectName("scrollbar")
        self.gridLayout.addWidget(self.scrollbar, 0, 0, 1, 5)
        self.lb_fit = QtWidgets.QLabel(parent=self.centralwidget)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Preferred)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.lb_fit.sizePolicy().hasHeightForWidth())
        self.lb_fit.setSizePolicy(sizePolicy)
        self.lb_fit.setFrameShape(QtWidgets.QFrame.Shape.Panel)
        self.lb_fit.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        self.lb_fit.setText("")
        self.lb_fit.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse|QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lb_fit.setObjectName("lb_fit")
        self.gridLayout.addWidget(self.lb_fit, 6, 0, 1, 5)
        self.lb_x = QtWidgets.QLabel(parent=self.centralwidget)
        self.lb_x.setObjectName("lb_x")
        self.gridLayout.addWidget(self.lb_x, 3, 0, 1, 1, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.sb_roi = QtWidgets.QSpinBox(parent=self.centralwidget)
        self.sb_roi.setMaximum(0)
        self.sb_roi.setObjectName("sb_roi")
        self.gridLayout.addWidget(self.sb_roi, 4, 1, 1, 1)
        self.cob_x = QtWidgets.QComboBox(parent=self.centralwidget)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.cob_x.sizePolicy().hasHeightForWidth())
        self.cob_x.setSizePolicy(sizePolicy)
        self.cob_x.setToolTip("")
        self.cob_x.setObjectName("cob_x")
        self.gridLayout.addWidget(self.cob_x, 4, 0, 1, 1)
        self.lb_roi = QtWidgets.QLabel(parent=self.centralwidget)
        self.lb_roi.setObjectName("lb_roi")
        self.gridLayout.addWidget(self.lb_roi, 3, 1, 1, 1, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.cob_fit = QtWidgets.QComboBox(parent=self.centralwidget)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.cob_fit.sizePolicy().hasHeightForWidth())
        self.cob_fit.setSizePolicy(sizePolicy)
        self.cob_fit.setObjectName("cob_fit")
        self.gridLayout.addWidget(self.cob_fit, 4, 2, 1, 1)
        self.lb_fitfunc = QtWidgets.QLabel(parent=self.centralwidget)
        self.lb_fitfunc.setObjectName("lb_fitfunc")
        self.gridLayout.addWidget(self.lb_fitfunc, 3, 2, 1, 1, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.bt_fit = QtWidgets.QPushButton(parent=self.centralwidget)
        self.bt_fit.setObjectName("bt_fit")
        self.gridLayout.addWidget(self.bt_fit, 4, 3, 1, 1)
        self.bt_export = QtWidgets.QPushButton(parent=self.centralwidget)
        self.bt_export.setObjectName("bt_export")
        self.gridLayout.addWidget(self.bt_export, 5, 2, 1, 1)
        self.bt_global = QtWidgets.QPushButton(parent=self.centralwidget)
        self.bt_global.setObjectName("bt_global")
        self.gridLayout.addWidget(self.bt_global, 5, 4, 1, 1)
        self.bt_import = QtWidgets.QPushButton(parent=self.centralwidget)
        self.bt_import.setObjectName("bt_import")
        self.gridLayout.addWidget(self.bt_import, 4, 4, 1, 1)
        self.bt_clear = QtWidgets.QPushButton(parent=self.centralwidget)
        self.bt_clear.setObjectName("bt_clear")
        self.gridLayout.addWidget(self.bt_clear, 5, 3, 1, 1)
        MainWindow.setCentralWidget(self.centralwidget)
        self.menubar = QtWidgets.QMenuBar(parent=MainWindow)
        self.menubar.setGeometry(QtCore.QRect(0, 0, 663, 21))
        self.menubar.setObjectName("menubar")
        MainWindow.setMenuBar(self.menubar)
        self.statusbar = QtWidgets.QStatusBar(parent=MainWindow)
        self.statusbar.setObjectName("statusbar")
        MainWindow.setStatusBar(self.statusbar)

        self.retranslateUi(MainWindow)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

    def retranslateUi(self, MainWindow):
        _translate = QtCore.QCoreApplication.translate
        MainWindow.setWindowTitle(_translate("MainWindow", "MainWindow"))
        self.cob_action.setItemText(0, _translate("MainWindow", "-- Choose action --"))
        self.cob_action.setItemText(1, _translate("MainWindow", "Copy image"))
        self.cob_action.setItemText(2, _translate("MainWindow", "Copy raw data"))
        self.cob_action.setItemText(3, _translate("MainWindow", "2D plot"))
        self.cob_action.setItemText(4, _translate("MainWindow", "Plot fit parameter"))
        self.cob_action.setItemText(5, _translate("MainWindow", "Generate heatmap"))
        self.cob_action.setItemText(6, _translate("MainWindow", "Merge selected scans"))
        self.cob_action.setItemText(7, _translate("MainWindow", "Batch fit selected"))
        self.lb_fit.setToolTip(_translate("MainWindow", "Fit results"))
        self.lb_x.setText(_translate("MainWindow", "X - Axis"))
        self.sb_roi.setToolTip(_translate("MainWindow", "Data of ROI to be displayed"))
        self.lb_roi.setText(_translate("MainWindow", "ROI (multi-select)"))
        self.lb_fitfunc.setText(_translate("MainWindow", "Choose Fit Function"))
        self.bt_fit.setText(_translate("MainWindow", "Perform Fit"))
        self.bt_export.setToolTip(_translate("MainWindow", "Create pyplot of currently displayed data and copies raw data to clipboard"))
        self.bt_export.setText(_translate("MainWindow", "Export Current Plot"))
        self.bt_global.setText(_translate("MainWindow", "Global Plot"))
        self.bt_import.setText(_translate("MainWindow", "Import Data"))
        self.bt_clear.setText(_translate("MainWindow", "Clear Fit"))


class ScanPlotApplet(QMainWindow, Ui_MainWindow):
    def __init__(self, args, req):
        super().__init__()

        app = QApplication.instance()
        app.setQuitOnLastWindowClosed(False)
        app.setStyleSheet(APP_STYLESHEET)

        self.data_name = args.data
        self.setupUi(self)
        self.plot_docks = []  # all PlotDock instances except browser
        self._focused_dock = None
        self._updating_style_ui = False
        self.tab_recipes = []  # dock recipes for experiment save/restore
        self.scans = []
        self.metadata = {"key_sets": [], "points": [], "function": ""}
        self.data = []
        self.plots = Plots(0)
        self.fitter = Fitter()
        self.n_rois = 1
        self.skip = True

        self._rebuild_main_layout()
        self.cob_x.currentIndexChanged.connect(self.set_x)
        self.scrollbar.valueChanged.connect(self.change_plot)
        self.bt_import.clicked.connect(self.import_data)
        self.bt_heatmap.clicked.connect(self.generate_heatmap)
        self.bt_batch_fit.clicked.connect(self.batch_fit_selected)

        self._style_plot_item(self.pw_plot.getPlotItem())
        self.setWindowTitle("Scan Plot")
        self.set_focused_dock(self.curves_dock)
        self.skip = False

    def _toolbar_sep(self):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _rebuild_main_layout(self):
        """Toolbar + dockable Curves / analysis / browser panels."""
        while self.gridLayout.count():
            self.gridLayout.takeAt(0)

        self.sb_roi.hide()
        for w in (self.lb_x, self.lb_roi, self.lb_fitfunc, self.cob_action):
            w.hide()
        self.bt_global.hide()

        self.list_roi = QListWidget()
        self.list_roi.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_roi.itemSelectionChanged.connect(self.draw_plot)

        self.list_scans = QListWidget()
        self.list_scans.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_scans.itemSelectionChanged.connect(self._on_scan_selection_changed)
        self.list_scans.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_scans.customContextMenuRequested.connect(self._scans_context_menu)

        self.bt_heatmap = QPushButton("Heatmap")
        self.bt_heatmap.setToolTip("Fit all ROIs of one scan; show a parameter as a color grid.")
        self.bt_batch_fit = QPushButton("Batch fit")
        self.bt_batch_fit.setToolTip("Fit params vs time / secondary axis / ROI.")
        self.bt_import.setText("Import")
        # View/Style/Fit live on each plot dock — hide unused legacy Ui chrome
        for w in (self.bt_fit, self.bt_clear, self.bt_export, self.cob_fit):
            w.hide()

        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setFloatable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" Data:"))
        tb.addWidget(self.bt_import)
        tb.addSeparator()
        tb.addWidget(QLabel(" Analyze:"))
        tb.addWidget(self.bt_heatmap)
        tb.addWidget(self.bt_batch_fit)

        # Zero-size central widget so Left/Right docks own the client area
        placeholder = QWidget()
        placeholder.setFixedSize(0, 0)
        placeholder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setCentralWidget(placeholder)
        self.setDockNestingEnabled(True)
        self.setDockOptions(
            self.dockOptions()
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
        )

        # Curves page + dock (controls attached after dock exists)
        curves_page = QWidget()
        curves_col = QVBoxLayout(curves_page)
        curves_col.setContentsMargins(2, 2, 2, 2)
        self.curves_dock = PlotDock("Curves", "curves", curves_page, parent=self, closable=False)
        self.curves_dock.activated.connect(self.set_focused_dock)
        curves_col.addWidget(
            self._build_dock_control_bar(
                self.curves_dock,
                with_x=True,
                with_style=True,
                with_fit=True,
                with_curves_actions=True,
            )
        )
        curves_col.addWidget(self.scrollbar)
        self.pw_plot.setSizePolicy(
            QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        )
        curves_col.addWidget(self.pw_plot, stretch=1)
        self.lb_fit.setWordWrap(True)
        self.lb_fit.setMinimumHeight(28)
        curves_col.addWidget(self.lb_fit)
        self.curves_dock.plot_widget = self.pw_plot

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.curves_dock)
        self.plot_docks.append(self.curves_dock)
        self.curves_dock.installEventFilter(self)
        # Empty session: only Browser visible; Curves appears after Import
        self.curves_dock.hide()

        # Right browser dock
        right = QSplitter(Qt.Orientation.Vertical)
        exp_box = QWidget()
        exp_layout = QVBoxLayout(exp_box)
        exp_layout.setContentsMargins(4, 4, 4, 4)
        exp_layout.addWidget(QLabel("Experiments"))
        self.list_experiments = QListWidget()
        self.list_experiments.setMaximumHeight(140)
        self.list_experiments.setToolTip("Saved experiment packages (.zip). Double-click to load.")
        self.list_experiments.itemDoubleClicked.connect(self._on_experiment_double_clicked)
        self.list_experiments.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_experiments.customContextMenuRequested.connect(self._experiments_context_menu)
        exp_layout.addWidget(self.list_experiments)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.ed_exp_name = QLineEdit()
        self.ed_exp_name.setPlaceholderText("experiment name")
        self.list_experiments.currentTextChanged.connect(self.ed_exp_name.setText)
        name_row.addWidget(self.ed_exp_name, stretch=1)
        exp_layout.addLayout(name_row)
        exp_btn_row = QHBoxLayout()
        self.bt_save_exp = QPushButton("Save")
        self.bt_open_exp = QPushButton("Load")
        self.bt_refresh_exp = QPushButton("Refresh")
        self.bt_save_exp.clicked.connect(self.save_experiment)
        self.bt_open_exp.clicked.connect(self.open_experiment)
        self.bt_refresh_exp.clicked.connect(self.refresh_experiment_list)
        exp_btn_row.addWidget(self.bt_save_exp)
        exp_btn_row.addWidget(self.bt_open_exp)
        exp_btn_row.addWidget(self.bt_refresh_exp)
        exp_layout.addLayout(exp_btn_row)

        scans_box = QWidget()
        scans_layout = QVBoxLayout(scans_box)
        scans_layout.setContentsMargins(4, 4, 4, 4)
        scans_layout.addWidget(QLabel("Loaded scans"))
        scans_layout.addWidget(self.list_scans)
        roi_box = QWidget()
        roi_layout = QVBoxLayout(roi_box)
        roi_layout.setContentsMargins(4, 4, 4, 4)
        roi_layout.addWidget(QLabel("ROI (multi-select)"))
        roi_layout.addWidget(self.list_roi)
        right.addWidget(exp_box)
        right.addWidget(scans_box)
        right.addWidget(roi_box)
        right.setStretchFactor(0, 0)
        right.setStretchFactor(1, 1)
        right.setStretchFactor(2, 1)

        self.browser_dock = QDockWidget("Browser", self)
        self.browser_dock.setObjectName("BrowserDock")
        self.browser_dock.setWidget(right)
        self.browser_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.browser_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.browser_dock)
        self.refresh_experiment_list()

        # Hide leftover centralwidget chrome from Ui_MainWindow (already reparented)
        self.centralwidget.hide()

    def eventFilter(self, obj, event):
        if isinstance(obj, PlotDock):
            et = event.type()
            if et == event.Type.MouseButtonDblClick:
                self.rename_dock(obj)
                return True
            # Do NOT listen to ZOrderChange — it storms set_focused_dock (see debug H1).
            if et in (event.Type.MouseButtonPress, event.Type.WindowActivate):
                self.set_focused_dock(obj)
        return super().eventFilter(obj, event)

    def set_focused_dock(self, dock):
        if dock is None or not isinstance(dock, PlotDock):
            return
        # Skip no-op re-focus (MouseButtonPress / activate spam on same dock)
        if dock is self._focused_dock:
            return
        self._focused_dock = dock
        kind = dock.state.kind
        self.statusBar().showMessage(f"Focused: {dock.windowTitle()} ({kind})", 2000)

    def focused_plot_dock(self):
        if self._focused_dock is not None and self._focused_dock in self.plot_docks:
            return self._focused_dock
        return self.curves_dock

    def _relayout_main_docks(self):
        """Give Curves (left) most width; keep Browser a narrow right column."""
        if not self.curves_dock.isVisible():
            return
        total = max(self.width(), 900)
        browser_w = 260 if self.browser_dock.isVisible() else 0
        curves_w = max(500, total - browser_w - 16)
        docks, sizes = [self.curves_dock], [curves_w]
        if self.browser_dock.isVisible():
            docks.append(self.browser_dock)
            sizes.append(browser_w)
        self.resizeDocks(docks, sizes, Qt.Orientation.Horizontal)

    def _show_curves_dock(self):
        self.curves_dock.show()
        self.curves_dock.raise_()
        self.set_focused_dock(self.curves_dock)
        # Defer until after Qt applies show(); otherwise width stays ~sliver
        QTimer.singleShot(0, self._relayout_main_docks)

    def rename_dock(self, dock):
        if dock is None:
            return
        text, ok = QInputDialog.getText(self, "Rename dock", "Title:", text=dock.windowTitle())
        if not ok or not text.strip():
            return
        title = text.strip()
        old = dock.windowTitle()
        dock.setWindowTitle(title)
        dock.state.title = title
        if dock.state.recipe is not None:
            dock.state.recipe["title"] = title

    def _curves_fitfunc(self):
        ctrls = getattr(self.curves_dock, "ctrls", None)
        if ctrls is not None and getattr(ctrls, "cob_fit", None) is not None:
            return ctrls.cob_fit.currentText()
        return "Gauss"

    def _build_dock_control_bar(
        self,
        dock,
        *,
        with_x=False,
        with_style=True,
        with_fit=True,
        with_curves_actions=False,
        with_copy=True,
    ):
        """Per-dock View/Style/Fit strip — travels with the floating/docked window."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)
        ctrls = SimpleNamespace()

        if with_x:
            row.addWidget(QLabel("X:"))
            self.cob_x.show()
            row.addWidget(self.cob_x)
            ctrls.cob_x = self.cob_x

        if with_style:
            ctrls.chk_legend = QCheckBox("Legend")
            ctrls.chk_legend.setChecked(bool(dock.state.show_legend))
            ctrls.chk_errorbars = QCheckBox("Error bars")
            ctrls.chk_errorbars.setChecked(bool(dock.state.show_errorbars))
            ctrls.cob_style = QComboBox()
            ctrls.cob_style.addItems(STYLE_MODES)
            ctrls.cob_style.setCurrentText(dock.state.style.get("mode", "Scatter"))
            dock.state.style["linewidth"] = 2.0
            ctrls.chk_legend.toggled.connect(lambda c, d=dock: self._dock_set_legend(d, c))
            ctrls.chk_errorbars.toggled.connect(lambda c, d=dock: self._dock_set_errorbars(d, c))
            ctrls.cob_style.currentTextChanged.connect(lambda m, d=dock: self._dock_set_style(d, m))
            row.addWidget(ctrls.chk_legend)
            row.addWidget(ctrls.chk_errorbars)
            row.addWidget(QLabel("Style:"))
            row.addWidget(ctrls.cob_style)

        if with_fit:
            ctrls.cob_fit = QComboBox()
            ctrls.cob_fit.addItems(list(self.fitter.func_dict.keys()))
            if dock.state.fitfunc:
                i = ctrls.cob_fit.findText(dock.state.fitfunc)
                if i >= 0:
                    ctrls.cob_fit.setCurrentIndex(i)
            else:
                dock.state.fitfunc = ctrls.cob_fit.currentText()
            ctrls.cob_fit.currentTextChanged.connect(
                lambda f, d=dock: self._dock_set_fitfunc(d, f)
            )
            ctrls.bt_fit = QPushButton("Fit")
            ctrls.bt_clear = QPushButton("Clear fit")
            ctrls.bt_fit.clicked.connect(lambda _=False, d=dock: self._dock_fit(d))
            ctrls.bt_clear.clicked.connect(lambda _=False, d=dock: self._dock_clear_fit(d))
            ctrls.bt_fit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            ctrls.bt_fit.customContextMenuRequested.connect(
                lambda _pos, d=dock: self._dock_advanced_fit(d)
            )
            row.addWidget(ctrls.cob_fit)
            row.addWidget(ctrls.bt_fit)
            row.addWidget(ctrls.bt_clear)

        if with_curves_actions:
            ctrls.bt_2d = QPushButton("2D plot")
            ctrls.bt_export = QPushButton("Export")
            ctrls.bt_2d.setToolTip("Stack scrollbar slices into an image (≥2D scans).")
            ctrls.bt_export.setToolTip("Open current curve in a new dock + copy TSV")
            ctrls.bt_2d.clicked.connect(self.two_d_plot)
            ctrls.bt_export.clicked.connect(self.export_plot)
            row.addWidget(ctrls.bt_2d)
            row.addWidget(ctrls.bt_export)

        if with_copy:
            ctrls.bt_copy = QToolButton()
            ctrls.bt_copy.setText("Copy")
            ctrls.bt_copy.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            copy_menu = QMenu(ctrls.bt_copy)
            act_img = QAction("Copy image", self)
            act_img.triggered.connect(lambda _=False, d=dock: self._dock_copy_image(d))
            act_raw = QAction("Copy raw data", self)
            act_raw.triggered.connect(lambda _=False, d=dock: self._dock_copy_raw(d))
            copy_menu.addAction(act_img)
            copy_menu.addAction(act_raw)
            ctrls.bt_copy.setMenu(copy_menu)
            row.addWidget(ctrls.bt_copy)

        row.addStretch(1)
        dock.ctrls = ctrls
        return bar

    def _dock_set_legend(self, dock, checked):
        self.set_focused_dock(dock)
        dock.state.show_legend = bool(checked)
        self._redraw_dock(dock)

    def _dock_set_errorbars(self, dock, checked):
        self.set_focused_dock(dock)
        dock.state.show_errorbars = bool(checked)
        dock.state.style["show_errorbars"] = bool(checked)
        self._redraw_dock(dock)

    def _dock_set_style(self, dock, mode):
        self.set_focused_dock(dock)
        dock.state.style["mode"] = mode
        dock.state.style["linewidth"] = 2.0
        self._redraw_dock(dock)

    def _dock_set_fitfunc(self, dock, func):
        self.set_focused_dock(dock)
        if func:
            dock.state.fitfunc = func

    def _dock_fit(self, dock):
        self.set_focused_dock(dock)
        func = None
        if getattr(dock, "ctrls", None) is not None and getattr(dock.ctrls, "cob_fit", None):
            func = dock.ctrls.cob_fit.currentText()
            dock.state.fitfunc = func
        self.perform_fit(None, func=func, dock=dock)

    def _dock_clear_fit(self, dock):
        self.set_focused_dock(dock)
        self.clear_fit(dock=dock)

    def _dock_advanced_fit(self, dock):
        self.set_focused_dock(dock)
        dialog = FittingDialog(self.fitter.func_dict)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        func, guess, fixed, _custom = dialog.get_results()
        if getattr(dock, "ctrls", None) is not None and getattr(dock.ctrls, "cob_fit", None):
            idx = dock.ctrls.cob_fit.findText(func)
            if idx >= 0:
                dock.ctrls.cob_fit.setCurrentIndex(idx)
        dock.state.fitfunc = func
        self.perform_fit(None, func=func, guess=guess, fixed=fixed, dock=dock)

    def _dock_copy_image(self, dock):
        self.set_focused_dock(dock)
        widget = dock.widget() if dock else self.pw_plot
        QApplication.clipboard().setPixmap(widget.grab())
        self.statusBar().showMessage(f"Copied image: {dock.windowTitle()}", 2500)

    def _dock_copy_raw(self, dock):
        """Copy this dock's curve data as TSV (Excel/Origin-friendly)."""
        self.set_focused_dock(dock)
        if dock is None:
            self.statusBar().showMessage("Copy raw: no dock", 3000)
            return
        kind = dock.state.kind
        try:
            if dock.state.cached_xy is not None:
                x, y, heights, *_ = dock.state.cached_xy
                x = np.asarray(x, dtype=float).ravel()
                y = np.asarray(y, dtype=float).ravel()
                yerr = np.asarray(heights, dtype=float).ravel() / 2.0
                n = min(len(x), len(y), len(yerr))
                if n == 0:
                    raise ValueError("empty cached curve")
                self.copy_to_clipboard(x=x[:n], y=y[:n], yerr=yerr[:n])
                self.statusBar().showMessage(
                    f"Copied {n} points from '{dock.windowTitle()}' (x, y, yerr)", 4000
                )
                return

            if kind == "curves":
                n = self._copy_curves_raw_to_clipboard()
                self.statusBar().showMessage(
                    f"Copied Curves raw data ({n} series) to clipboard", 4000
                )
                return

            if kind.startswith("derived") and dock.state.derived_scan is not None:
                n = self._copy_scan_raw_to_clipboard(
                    dock.state.derived_scan,
                    slice_index=dock.state.slice or 0,
                    title=dock.windowTitle(),
                )
                self.statusBar().showMessage(
                    f"Copied '{dock.windowTitle()}' raw data ({n} series)", 4000
                )
                return

            if kind == "batch" and dock.state.panel_series:
                self._copy_panel_series_to_clipboard(dock.state.panel_series)
                self.statusBar().showMessage(
                    f"Copied Batch series ({len(dock.state.panel_series)}) to clipboard", 4000
                )
                return

            self.statusBar().showMessage(
                f"Copy raw data not available for '{dock.windowTitle()}' ({kind})", 4000
            )
        except Exception as exc:
            logging.exception("Copy raw data failed")
            self.statusBar().showMessage(f"Copy raw failed: {exc}", 5000)

    def _copy_curves_raw_to_clipboard(self):
        """All currently selected scan×ROI series (unbinned x,y)."""
        scan_idxs = self.selected_scan_indices()
        roi_idxs = self.selected_roi_indices()
        if not scan_idxs or not self.scans:
            raise ValueError("no scans selected")
        slice_index = self.scrollbar.value()
        series = []
        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None or not scan.plots.plots:
                continue
            if slice_index >= scan.plots.n_plots():
                continue
            for roi in roi_idxs:
                if roi >= scan.plots.n_rois:
                    continue
                x, y = scan.plots.get_data(slice_index, roi)
                if not scan.metadata["key_sets"]:
                    x = np.arange(len(y))
                name = self._series_legend_name(
                    scan, roi, len(scan_idxs), len(roi_idxs)
                )
                series.append((name, np.asarray(x, dtype=float).ravel(),
                               np.asarray(y, dtype=float).ravel()))
        if not series:
            raise ValueError("no curve series to copy")
        self._copy_named_xy_series(series)
        return len(series)

    def _copy_scan_raw_to_clipboard(self, scan, slice_index=0, title=""):
        if scan is None or scan.plots is None or not scan.plots.plots:
            raise ValueError("no plot data")
        slice_index = min(int(slice_index), max(scan.plots.n_plots() - 1, 0))
        series = []
        for roi in range(scan.plots.n_rois):
            x, y = scan.plots.get_data(slice_index, roi)
            if not scan.metadata["key_sets"]:
                x = np.arange(len(y))
            series.append((
                f"ROI{roi}",
                np.asarray(x, dtype=float).ravel(),
                np.asarray(y, dtype=float).ravel(),
            ))
        if not series:
            raise ValueError("empty derived scan")
        self._copy_named_xy_series(series)
        return len(series)

    def _copy_panel_series_to_clipboard(self, panels):
        series = []
        for panel in panels:
            label = panel.get("label", "series")
            x = np.asarray(panel["x"], dtype=float).ravel()
            y = np.asarray(panel["y"], dtype=float).ravel()
            yerr = panel.get("yerr")
            if yerr is not None:
                yerr = np.asarray(yerr, dtype=float).ravel()
                series.append((label, x, y, yerr))
            else:
                series.append((label, x, y, None))
        self._copy_named_xy_series(series)

    def _copy_named_xy_series(self, series):
        """
        Write one or more (name, x, y[, yerr]) series to the clipboard as TSV.
        Single series → columns x, y[, yerr].
        Multiple → side-by-side blocks name_x, name_y[, name_yerr], padded with blanks.
        """
        if not series:
            raise ValueError("no series")
        # Normalize to (name, x, y, yerr|None)
        norm = []
        for item in series:
            if len(item) == 3:
                name, x, y = item
                yerr = None
            else:
                name, x, y, yerr = item
            name = str(name).replace("\t", " ").replace("\n", " ")
            norm.append((name, np.asarray(x, dtype=float).ravel(),
                         np.asarray(y, dtype=float).ravel(),
                         None if yerr is None else np.asarray(yerr, dtype=float).ravel()))

        if len(norm) == 1:
            name, x, y, yerr = norm[0]
            n = min(len(x), len(y))
            if yerr is not None:
                n = min(n, len(yerr))
                self.copy_to_clipboard(x=x[:n], y=y[:n], yerr=yerr[:n])
            else:
                self.copy_to_clipboard(x=x[:n], y=y[:n])
            return

        max_len = max(min(len(x), len(y)) for _, x, y, _ in norm)
        headers = []
        cols = []
        for name, x, y, yerr in norm:
            n = min(len(x), len(y))
            headers.extend([f"{name}_x", f"{name}_y"])
            cols.append(list(x[:n]) + [""] * (max_len - n))
            cols.append(list(y[:n]) + [""] * (max_len - n))
            if yerr is not None:
                headers.append(f"{name}_yerr")
                ne = min(n, len(yerr))
                cols.append(list(yerr[:ne]) + [""] * (max_len - ne))
        lines = ["\t".join(headers)]
        for r in range(max_len):
            lines.append("\t".join("" if cols[c][r] == "" else str(cols[c][r])
                                   for c in range(len(cols))))
        QApplication.clipboard().setText("\n".join(lines) + "\n")

    def _redraw_dock(self, dock):
        if dock is None:
            return
        if dock.state.kind == "curves":
            self.draw_plot()
            return
        if dock.state.kind in ("export", "batch") and dock.state.cached_xy is not None:
            self._redraw_snapshot_dock(dock)
            return
        if dock.state.kind.startswith("derived") and dock.state.derived_scan is not None:
            self._redraw_derived_dock(dock)
            return
        if dock.state.kind == "batch" and dock.state.panel_series:
            self._redraw_batch_panels(dock)
            return
        # heatmap/2d: no per-dock restyle path
        if dock.state.kind in ("2d", "heatmap"):
            self.statusBar().showMessage(
                f"Style applies to Curves/Export/derived docks; '{dock.windowTitle()}' is view-only",
                4000,
            )

    def _apply_fit_overlays(self, dock):
        """Re-draw stored snapshot fit curves onto dock.plot_widget."""
        pw = getattr(dock, "plot_widget", None)
        if not isinstance(pw, PlotWidget):
            return
        for ov in dock.state.fit_overlays or []:
            color = ov.get("color") or OVERLAY_COLORS[1]
            pw.plot(
                np.asarray(ov["x"], dtype=float),
                np.asarray(ov["y"], dtype=float),
                pen=mkPen(color, width=2),
            )

    def _record_tab_recipe(self, recipe, dock=None):
        self.tab_recipes.append(recipe)
        if dock is not None:
            dock.state.recipe = recipe

    def _clear_analysis_tabs(self):
        for dock in list(self.plot_docks):
            if dock is self.curves_dock:
                continue
            self._close_plot_dock(dock)
        self.tab_recipes = []

    def _close_plot_dock(self, dock):
        if dock is self.curves_dock:
            dock.hide()
            return
        if dock.state.recipe in self.tab_recipes:
            try:
                self.tab_recipes.remove(dock.state.recipe)
            except ValueError:
                pass
        if dock in self.plot_docks:
            self.plot_docks.remove(dock)
        self.removeDockWidget(dock)
        dock.deleteLater()
        if self._focused_dock is dock:
            self.set_focused_dock(self.curves_dock)

    def _on_plot_dock_closed(self, dock):
        self._close_plot_dock(dock)

    def _unique_dock_name(self, title):
        existing = {d.windowTitle() for d in self.plot_docks}
        name = title
        n = 2
        while name in existing:
            name = f"{title} ({n})"
            n += 1
        return name

    def show_plot_tab(self, title, widget, kind="export", floating=False, recipe=None):
        """Backward-compatible alias → show_plot_dock."""
        return self.show_plot_dock(title, widget, kind=kind, floating=floating, recipe=recipe)

    def _place_floating_dock(self, dock, w_frac=0.58, h_frac=0.62):
        """Center a floating dock over the main window with a usable size."""
        geo = self.geometry()
        w = max(720, int(geo.width() * w_frac))
        h = max(520, int(geo.height() * h_frac))
        # Keep inside the parent window frame when possible
        x = geo.x() + max(0, (geo.width() - w) // 2)
        y = geo.y() + max(0, (geo.height() - h) // 2)
        dock.resize(w, h)
        dock.move(x, y)
        dock.raise_()
        dock.activateWindow()

    def show_plot_dock(self, title, widget, kind="export", floating=False, recipe=None):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        dock = PlotDock(self._unique_dock_name(title), kind, page, parent=self, closable=True)
        dock.plot_widget = widget
        dock.state.recipe = recipe
        curve_kinds = ("export", "batch", "derived_merge", "derived_global")
        if kind in curve_kinds:
            # Batch multi-panel: Fit yes; Style only helps when cached_xy / derived
            layout.addWidget(
                self._build_dock_control_bar(
                    dock,
                    with_style=True,
                    with_fit=True,
                    with_copy=True,
                )
            )
        elif kind in ("2d", "heatmap"):
            layout.addWidget(
                self._build_dock_control_bar(
                    dock,
                    with_style=False,
                    with_fit=False,
                    with_copy=True,
                )
            )
        layout.addWidget(widget, stretch=1)
        dock.closed.connect(self._on_plot_dock_closed)
        dock.activated.connect(self.set_focused_dock)
        dock.installEventFilter(self)
        # Main plot area (same side as Curves), not the thin bottom strip
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        if floating:
            dock.setFloating(True)
            # Geometry applies after Qt finishes float transition
            QTimer.singleShot(0, lambda d=dock: self._place_floating_dock(d))
        else:
            if self.curves_dock.isVisible() and self.curves_dock is not dock:
                self.tabifyDockWidget(self.curves_dock, dock)
            dock.show()
            dock.raise_()
        self.plot_docks.append(dock)
        self.set_focused_dock(dock)
        return dock

    def _style_plot_item(self, plot_item):
        f = QFont("Segoe UI", 10)
        plot_item.getAxis("left").setStyle(tickFont=f)
        plot_item.getAxis("bottom").setStyle(tickFont=f)
        plot_item.showAxis("top", show=True)
        plot_item.showAxis("right", show=True)
        plot_item.getAxis("top").setStyle(showValues=False)
        plot_item.getAxis("right").setStyle(showValues=False)
        plot_item.getAxis("right").setWidth(16)
        plot_item.showGrid(x=True, y=True, alpha=0.18)
        try:
            plot_item.getViewBox().setBackgroundColor(THEME["bg"])
        except Exception:
            pass

    def _new_plot_widget(self, date_axis=False):
        if date_axis:
            pw = PlotWidget(axisItems={"bottom": DateAxisItem(orientation="bottom")})
        else:
            pw = PlotWidget()
        self._style_plot_item(pw.getPlotItem())
        try:
            pw.setBackground(THEME["bg"])
        except Exception:
            pass
        return pw

    def _add_errorbar_scatter(
        self, plot_item, unique_x, means, heights, y_centers=None,
        style=None, name=None, view_style=None, show_errorbars=True, series_out=None,
    ):
        """Curve + optional ErrorBarItem; register pair for legend sync."""
        unique_x = np.asarray(unique_x, dtype=float)
        means = np.asarray(means, dtype=float)
        heights = np.asarray(heights, dtype=float)
        if y_centers is None:
            y_centers = means
        else:
            y_centers = np.asarray(y_centers, dtype=float)

        color = (86, 182, 194)
        if isinstance(style, dict) and style.get("symbolBrush"):
            color = style["symbolBrush"]
        plot_kw = plot_kwargs_from_style(color, view_style)
        if style and style.get("symbolBrush") and plot_kw.get("symbol"):
            plot_kw["symbolBrush"] = mkBrush(*style["symbolBrush"])

        err_item = None
        idx = np.isfinite(heights) & (heights > 0)
        if show_errorbars and np.any(idx):
            beam = 0.007 * (unique_x.max() - unique_x.min()) if len(unique_x) else 0.01
            err_item = ErrorBarItem(
                x=unique_x[idx], y=y_centers[idx], height=heights[idx], beam=beam
            )
            plot_item.addItem(err_item)
        if name:
            plot_kw["name"] = name
        curve = plot_item.plot(unique_x, means, **plot_kw)
        link_curve_errorbar(curve, err_item)
        if series_out is not None:
            series_out.append((curve, err_item, name))
        return curve, err_item

    def _add_image_with_colorbar(self, data, x0, y0, w, h, xlabel="", ylabel="", title=""):
        """GraphicsLayoutWidget with ImageItem + ColorBarItem (viridis)."""
        glw = GraphicsLayoutWidget()
        plot = glw.addPlot(row=0, col=0)
        self._style_plot_item(plot)
        if xlabel:
            plot.setLabel("bottom", xlabel, **font)
        if ylabel:
            plot.setLabel("left", ylabel, **font)
        if title:
            plot.setTitle(title, **font)
        img_data = np.asarray(data, dtype=float)
        cmap = pg_colormap.get("viridis")
        lut = cmap.getLookupTable(nPts=256)
        img = ImageItem(img_data)
        img.setOpts(axisOrder="row-major")
        img.setLookupTable(lut)
        img.setRect(x0, y0, w, h)
        plot.addItem(img)
        finite = img_data[np.isfinite(img_data)]
        if finite.size:
            vmin, vmax = float(np.nanmin(finite)), float(np.nanmax(finite))
            if vmin == vmax:
                vmax = vmin + 1.0
        else:
            vmin, vmax = 0.0, 1.0
        img.setLevels([vmin, vmax])
        cbar = ColorBarItem(
            values=(vmin, vmax),
            colorMap=cmap,
            width=18,
            interactive=False,
        )
        glw.addItem(cbar, row=0, col=1)
        cbar.setImageItem(img)
        return glw

    def closeEvent(self, event):
        #mainly just prevents a warning in artiq when closing applet
        QApplication.instance().quit()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.copy_plot_image()
        if event.key() == Qt.Key.Key_A and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.copy_raw_data()
        if event.key() == Qt.Key.Key_Q and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.two_d_plot()

    def copy_plot_image(self):
        dock = self.focused_plot_dock()
        widget = dock.widget() if dock else self.pw_plot
        screenshot = widget.grab()
        QApplication.clipboard().setPixmap(screenshot)

    def copy_raw_data(self):
        """Ctrl+A — copy raw data from the focused plot dock."""
        self._dock_copy_raw(self.focused_plot_dock())

    def selected_scan_indices(self):
        return sorted({item.row() for item in self.list_scans.selectedIndexes()})

    def selected_roi_indices(self):
        rows = sorted({item.row() for item in self.list_roi.selectedIndexes()})
        return rows if rows else [0]

    def primary_scan(self):
        idxs = self.selected_scan_indices()
        if not idxs and self.scans:
            return self.scans[0]
        if not idxs:
            return None
        return self.scans[idxs[0]]

    def sync_primary_aliases(self):
        scan = self.primary_scan()
        if scan is None:
            self.metadata = {"key_sets": [], "points": [], "function": ""}
            self.data = []
            self.plots = Plots(0)
            return
        self.metadata = scan.metadata
        self.data = scan.data
        self.plots = scan.plots if scan.plots is not None else Plots(0)

    def _on_scan_selection_changed(self):
        if self.skip:
            return
        self.sync_primary_aliases()
        self.refresh_roi_list(keep_selection=True)
        self.draw_plot()
        self.set_title()

    def clear_scans(self):
        self.scans = []
        self.list_scans.clear()
        self.list_roi.clear()
        self._clear_analysis_tabs()
        self.metadata = {"key_sets": [], "points": [], "function": ""}
        self.data = []
        self.plots = Plots(0)
        self.pw_plot.clear()
        self.lb_fit.setText("")
        self.set_focused_dock(self.curves_dock)

    def _confirm_clear_scans(self):
        if not self.scans:
            return
        ans = QMessageBox.question(
            self, "Clear all scans",
            "Remove all loaded scans and close analysis docks?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.clear_scans()

    def _scans_context_menu(self, pos):
        menu = QMenu(self)
        act_merge = menu.addAction("Merge selected → new dock")
        act_global = menu.addAction("Global (sum ROIs) → new dock")
        menu.addSeparator()
        act_clear = menu.addAction("Clear all…")
        chosen = menu.exec(self.list_scans.mapToGlobal(pos))
        if chosen is act_merge:
            self.merge_selected_scans()
        elif chosen is act_global:
            self.globalify()
        elif chosen is act_clear:
            self._confirm_clear_scans()

    def _experiments_context_menu(self, pos):
        menu = QMenu(self)
        act_open = menu.addAction("Open")
        act_dup = menu.addAction("Duplicate")
        act_del = menu.addAction("Delete…")
        act_reveal = menu.addAction("Reveal in folder")
        menu.addSeparator()
        act_refresh = menu.addAction("Refresh")
        chosen = menu.exec(self.list_experiments.mapToGlobal(pos))
        if chosen is act_open:
            self.open_experiment()
        elif chosen is act_dup:
            self._duplicate_experiment()
        elif chosen is act_del:
            self._delete_experiment()
        elif chosen is act_reveal:
            self._reveal_experiment()
        elif chosen is act_refresh:
            self.refresh_experiment_list()

    def _duplicate_experiment(self):
        src = self._selected_experiment_path()
        if src is None or not src.is_file():
            QMessageBox.warning(self, "Duplicate", "Select an experiment zip first.")
            return
        base = src.stem + "_copy"
        dest = EXPERIMENTS_DIR / f"{base}.zip"
        n = 2
        while dest.exists():
            dest = EXPERIMENTS_DIR / f"{base}{n}.zip"
            n += 1
        try:
            shutil.copy2(src, dest)
            self.refresh_experiment_list()
            matches = self.list_experiments.findItems(dest.stem, Qt.MatchFlag.MatchExactly)
            if matches:
                self.list_experiments.setCurrentItem(matches[0])
        except Exception as exc:
            logging.exception("Duplicate experiment failed")
            QMessageBox.critical(self, "Duplicate failed", str(exc))

    def _delete_experiment(self):
        path = self._selected_experiment_path()
        if path is None or not path.is_file():
            QMessageBox.warning(self, "Delete", "Select an experiment zip first.")
            return
        ans = QMessageBox.question(
            self, "Delete experiment",
            f"Delete {path.name}? This cannot be undone.",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink()
            self.refresh_experiment_list()
        except Exception as exc:
            logging.exception("Delete experiment failed")
            QMessageBox.critical(self, "Delete failed", str(exc))

    def _reveal_experiment(self):
        path = self._selected_experiment_path()
        if path is None or not path.is_file():
            QMessageBox.warning(self, "Reveal", "Select an experiment zip first.")
            return
        try:
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        except Exception as exc:
            logging.exception("Reveal failed")
            QMessageBox.critical(self, "Reveal failed", str(exc))

    def rebuild_scan_list_widget(self, select_indices=None):
        self.skip = True
        self.list_scans.clear()
        for scan in self.scans:
            self.list_scans.addItem(scan.name)
        if select_indices is None:
            select_indices = list(range(len(self.scans)))
        for i in select_indices:
            if 0 <= i < self.list_scans.count():
                self.list_scans.item(i).setSelected(True)
        self.skip = False
        self.sync_primary_aliases()

    def first_nonempty_roi(self, scan):
        """Return first ROI index with at least one survival attempt, else 0."""
        if scan is None or not scan.data:
            return 0
        n = len(scan.data[0])
        for roi in range(n):
            total = 0.0
            for point in scan.data:
                val = point[roi]
                if isinstance(val, (list, tuple, np.ndarray)) and len(val) >= 1:
                    v = float(val[0])
                    if not np.isnan(v):
                        total += v
                elif not isinstance(val, (list, tuple, np.ndarray)):
                    v = float(val)
                    if not np.isnan(v):
                        total += abs(v)
            if total > 0:
                return roi
        return 0

    def refresh_roi_list(self, keep_selection=False):
        scan = self.primary_scan()
        default_roi = self.first_nonempty_roi(scan)
        prev = set(self.selected_roi_indices()) if keep_selection else {default_roi}
        n = 1
        if scan is not None and scan.data:
            n = len(scan.data[0])
        self.n_rois = n
        self.skip = True
        self.list_roi.clear()
        for i in range(n):
            self.list_roi.addItem(f"ROI {i}")
        selected_any = False
        for i in range(n):
            if i in prev:
                self.list_roi.item(i).setSelected(True)
                selected_any = True
        if not selected_any and n > 0:
            self.list_roi.item(default_roi).setSelected(True)
        self.skip = False

    def add_all_points(self, scan=None):
        scan = scan or self.primary_scan()
        if scan is None or scan.plots is None:
            return
        for i, result in enumerate(scan.data):
            scan.plots.add_point(scan.metadata["points"][i], result)

    def build_plots_for_scan(self, scan, x_index):
        plots = Plots(x_index)
        func = scan.metadata["function"]
        if func != "Global survival" and scan.data:
            plots.set_n_rois(len(scan.data[0]))
        for point in scan.metadata["points"]:
            plots.add_plot(point)
        plots.resort_keys()
        scan.plots = plots
        for i, result in enumerate(scan.data):
            plots.add_point(scan.metadata["points"][i], result)
        return plots

    def get_plot_indices(self):
        rois = self.selected_roi_indices()
        return self.scrollbar.value(), rois[0]

    def get_current_data(self):
        self.sync_primary_aliases()
        if self.primary_scan() is None or self.plots is None or not self.plots.plots:
            return array([]), array([])
        return self.plots.get_data(*self.get_plot_indices())

    def set_x(self, index):
        if self.skip:
            return
        if not self.scans:
            return

        for scan in self.scans:
            self.build_plots_for_scan(scan, index)

        self.sync_primary_aliases()
        primary = self.primary_scan()
        if primary is None or primary.plots is None:
            return

        self.scrollbar.setRange(0, max(primary.plots.n_plots() - 1, 0))

        func = primary.metadata["function"]
        self.pw_plot.setLabel("left", func, **font)
        if primary.metadata["key_sets"]:
            self.pw_plot.setLabel("bottom", primary.metadata["key_sets"][index][-1], **font)
        else:
            self.pw_plot.setLabel("bottom", "repetition", **font)

        self.draw_plot()
        self.set_title()

    def change_plot(self):
        self.draw_plot()
        self.set_title()

    def _series_style(self, color_index):
        color = OVERLAY_COLORS[color_index % len(OVERLAY_COLORS)]
        return {"symbolBrush": color}

    def _scan_short_name(self, scan):
        return scan.name.replace("\\", "/").rstrip("/").split("/")[-1]

    def _series_legend_name(self, scan, roi, n_scans, n_rois):
        short = self._scan_short_name(scan)
        if n_scans == 1:
            return f"ROI {roi}"
        if n_rois == 1:
            return short
        return f"{short}/R{roi}"

    def _curves_view_state(self):
        return self.curves_dock.state

    def draw_plot(self):
        if self.skip:
            return
        self.pw_plot.clear()
        vst = self._curves_view_state()
        vst.series_items = []

        scan_idxs = self.selected_scan_indices()
        roi_idxs = self.selected_roi_indices()
        if not scan_idxs or not self.scans:
            self.lb_fit.setText("")
            return

        slice_index = self.scrollbar.value()
        series = []
        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None or not scan.plots.plots:
                continue
            if slice_index >= scan.plots.n_plots():
                logging.warning("Scan %s has no slice index %s; skipping", scan.name, slice_index)
                continue
            for roi in roi_idxs:
                if roi >= scan.plots.n_rois:
                    logging.warning("Scan %s has no ROI %s; skipping", scan.name, roi)
                    continue
                series.append((scan, roi))

        show_legend = bool(vst.show_legend)
        if show_legend:
            self.pw_plot.addLegend()

        n_scans = len({id(s) for s, _ in series})
        n_rois = len({r for _, r in series})
        for color_i, (scan, roi) in enumerate(series):
            x, y = scan.plots.get_data(slice_index, roi)
            if not scan.metadata["key_sets"]:
                x = np.arange(len(y))
            name = self._series_legend_name(scan, roi, n_scans, n_rois) if show_legend else None
            style = self._series_style(color_i)
            if "survival" in scan.metadata["function"].lower():
                self.plot_survival(x, y, style=style, name=name, plot_widget=self.pw_plot, view_state=vst)
            else:
                self.plot_errorbars(x, y, style=style, name=name, plot_widget=self.pw_plot, view_state=vst)

        summaries = []
        for scan, roi in series:
            fit, params, popt, perr = scan.plots.get_fit(slice_index, roi)
            if fit:
                try:
                    self.pw_plot.addItem(fit)
                except Exception:
                    fx, fy = fit.getData()
                    color = OVERLAY_COLORS[0]
                    self.pw_plot.plot(fx, fy, pen=mkPen(color, width=2))
                if params is not None and popt is not None:
                    short = ", ".join(
                        f"{p}={self.format_fit_params(v, e)}"
                        for p, v, e in zip(params, popt, perr)
                    )
                    label = f"ROI{roi}" if n_scans == 1 else f"{self._scan_short_name(scan)}/ROI{roi}"
                    summaries.append(f"{label}: {short}")
        if summaries:
            if len(summaries) <= 2:
                self.lb_fit.setText(" | ".join(summaries))
            else:
                self.lb_fit.setText(f"{summaries[0]}  (+{len(summaries) - 1} more fits)")
        else:
            self.lb_fit.setText("")

    def plot_survival(self, x, y, style=None, name=None, plot_widget=None, view_state=None):
        pw = plot_widget or self.pw_plot
        vst = view_state or self._curves_view_state()
        unique_x, means, err = self.get_survival_rate(x, y)
        means_a = np.asarray(means, dtype=float)
        if means_a.size == 0 or not np.any(np.isfinite(means_a)):
            return

        lower = err[0]
        upper = err[1]
        heights = lower + upper
        y_centers = means + (upper - lower) / 2
        self._add_errorbar_scatter(
            pw.getPlotItem(),
            unique_x, means, heights, y_centers=y_centers,
            style=style, name=name,
            view_style=vst.style,
            show_errorbars=vst.show_errorbars,
            series_out=vst.series_items,
        )

    def plot_errorbars(self, x, y, style=None, name=None, plot_widget=None, view_state=None):
        pw = plot_widget or self.pw_plot
        vst = view_state or self._curves_view_state()
        unique_x, means, err = self.get_errorbars(x, y)
        means_a = np.asarray(means, dtype=float)
        if means_a.size == 0 or not np.any(np.isfinite(means_a)):
            return
        self._add_errorbar_scatter(
            pw.getPlotItem(),
            unique_x, means, err * 2, y_centers=means,
            style=style, name=name,
            view_style=vst.style,
            show_errorbars=vst.show_errorbars,
            series_out=vst.series_items,
        )
    
    def get_errorbars(self, x, y):
        # NaN entries mark shots excluded upstream (e.g. "Sorting success
        # only" in the scan applet) and are dropped from each bin's stats
        unique_x = np.unique(x)
        means = np.full(unique_x.shape, np.nan)
        errors = np.zeros(unique_x.shape)
        for i, u in enumerate(unique_x):
            vals = y[u == x]
            vals = vals[~np.isnan(vals)]
            if vals.size == 0:
                continue
            means[i] = np.mean(vals)
            errors[i] = sem(vals) if vals.size > 1 else 0

        return unique_x, means, errors

    def get_survival_rate(self, x, y):
        # NaN entries mark shots excluded upstream (e.g. "Sorting success
        # only" in the scan applet); nansum treats them as a zero
        # contribution to both the attempt and success counts, i.e. as if
        # the shot never happened
        x = np.asarray(x)
        # legacy data stored excluded shots as flat NaN instead of
        # [nan, nan] pairs; coerce so the array is always 2D
        y = [v if hasattr(v, "__len__") else [np.nan, np.nan] for v in y]
        y = np.asarray(y, dtype=float)
        unique_x = np.unique(x)
        means = np.array([np.nansum(y[x == u, 1]) / np.nansum(y[x == u, 0]) if np.nansum(y[x == u, 0]) > 0 else np.nan for u in unique_x])
        #these will be 68% intervals of the Binomial proportion confidence interval
        errors = np.array([
            (
                means[i] - proportion_confint(
                    np.nansum(y[x == u, 1]),
                    np.nansum(y[x == u, 0]),
                    alpha=0.32,
                    method="wilson"
                )[0],
                proportion_confint(
                    np.nansum(y[x == u, 1]),
                    np.nansum(y[x == u, 0]),
                    alpha=0.32,
                    method="wilson"
                )[1] - means[i]
            ) if np.nansum(y[x == u, 0]) > 0 else (np.nan, np.nan)
            for i, u in enumerate(unique_x)
        ]).T

        return unique_x, means, errors
    
    def globalify(self):
        """Non-destructive: sum ROIs into a derived floating dock (not Loaded scans)."""
        scan = self.primary_scan()
        n_loaded_before = len(self.scans)
        if scan is None:
            logging.error("No scan loaded")
            return
        if not scan.data or len(scan.data[0]) < 2:
            logging.error("Data is already global / single-ROI")
            return

        meta = copy.deepcopy(scan.metadata)
        if meta["function"] == "Survival probability":
            meta["function"] = "Global survival"
        else:
            meta["function"] = "Global " + meta["function"]
        data = []
        for point in scan.data:
            if isinstance(point[0], (list, tuple, np.ndarray)):
                data.append([[sum(x[0] for x in point), sum(x[1] for x in point)]])
            else:
                data.append([sum(point)])
        derived = ScanDataset(
            name=f"global_{self._scan_short_name(scan)}",
            path=scan.path,
            metadata=meta,
            data=data,
        )
        self._open_derived_dock(derived, kind="derived_global", title=f"Global: {self._scan_short_name(scan)}")

    def write_fit_params(self, params, popt, perr):
        s = ", ".join([p + " = " + self.format_fit_params(v, e) for p, v, e in zip(params, popt, perr)])
        self.lb_fit.setText(s)
    
    def format_fit_params(self, value, error):
        if value == 0:
            return "0"
        if error == 0:
            exponent = int(np.floor(np.log10(np.abs(value))))
            mantissa = value / 10**exponent
            return f"{mantissa:.1f}(0)e{exponent}"
        if error == np.inf or np.isnan(error):
            exponent = int(np.floor(np.log10(np.abs(value))))
            mantissa = value / 10**exponent
            return f"{mantissa:.1f}(inf)e{exponent}"
        
        exponent = int(np.floor(np.log10(np.abs(value))))
        scale = 10 ** exponent
        norm_value = value / scale
        norm_error = error / scale

        unc_digits = max(-int(np.floor(np.log10(norm_error))), 0)
        rounded_error = round(norm_error, unc_digits)
        rounded_value = round(norm_value, unc_digits)

        error_digits = int(rounded_error * (10 ** unc_digits))
        value_str = f"{rounded_value:.{unc_digits}f}"

        return f"{value_str}({error_digits})e{exponent}"

    def add_point(self):
        i = len(self.data)-1
        point = self.metadata["points"][i]
        self.plots.add_point(point, self.data[-1])
        self.draw_plot()

    def set_title(self):
        self.sync_primary_aliases()
        if not self.plots or not self.plots.plots:
            self.pw_plot.setTitle("", **font)
            return
        i = self.scrollbar.value()
        keys = list(self.plots.plots.keys())
        if i >= len(keys):
            self.pw_plot.setTitle("", **font)
            return
        constants = keys[i]
        title = ""
        k = -1
        for j in constants:
            k += 2 if self.plots.x_index == k + 1 else 1
            if self.metadata["key_sets"]:
                title += self.metadata["key_sets"][k][-1] + f" = {j}, "
        n_scans = len(self.selected_scan_indices())
        n_rois = len(self.selected_roi_indices())
        if n_scans > 1 or n_rois > 1:
            title += f"({n_scans} scans × {n_rois} ROIs)"
        self.pw_plot.setTitle(title.rstrip(", "), **font)

    def create_new_plot(self, metadata=None):
        # metadata arg kept for compatibility; multi-scan uses self.scans
        if metadata is not None and self.primary_scan() is not None:
            self.primary_scan().metadata = metadata
        self.sync_primary_aliases()

        self.skip = True
        self.cob_x.clear()
        if self.metadata["key_sets"]:
            self.cob_x.addItems([key[-1] for key in self.metadata["key_sets"]])
        else:
            self.cob_x.addItems(["repetition"])
        self.refresh_roi_list()
        self.skip = False
        if self.scans:
            self._show_curves_dock()
        self.set_x(0)

    def data_changed(self, value, metadata, persist, mods):
        self.data = value[self.data_name]
        l = len(self.data)
        if mods[0]["action"] == "init" or l == 1:
            self.create_new_plot()
        elif l == 0:
            return
        else:
            self.add_point()

    def clear_fit(self, dock=None):
        """Clear fits on the given (or focused) dock (Curves / derived / snapshot)."""
        dock = dock or self.focused_plot_dock()
        kind = dock.state.kind if dock else "curves"
        if kind == "curves":
            scan_idxs = self.selected_scan_indices()
            roi_idxs = self.selected_roi_indices()
            slice_index = self.scrollbar.value()
            for s_i in scan_idxs:
                if s_i >= len(self.scans):
                    continue
                scan = self.scans[s_i]
                if scan.plots is None or not scan.plots.plots:
                    continue
                if slice_index >= scan.plots.n_plots():
                    continue
                for roi in roi_idxs:
                    if roi >= scan.plots.n_rois:
                        continue
                    fit, _, _, _ = scan.plots.get_fit(slice_index, roi)
                    if fit:
                        try:
                            self.pw_plot.removeItem(fit)
                        except Exception:
                            pass
                        scan.plots.remove_fit(slice_index, roi)
            self.lb_fit.setText("")
            self.draw_plot()
        elif kind.startswith("derived") and dock.state.derived_scan is not None:
            scan = dock.state.derived_scan
            if scan.plots is not None:
                for i in range(scan.plots.n_plots()):
                    for roi in range(scan.plots.n_rois):
                        scan.plots.remove_fit(i, roi)
            self._redraw_derived_dock(dock)
        elif kind == "batch" and dock.state.panel_series and dock.state.cached_xy is None:
            for panel in dock.state.panel_series:
                panel.pop("fit_x", None)
                panel.pop("fit_y", None)
            self._redraw_batch_panels(dock)
        else:
            dock.state.fit_overlays = []
            self._redraw_dock(dock)
        self.statusBar().showMessage("Fit cleared", 2000)

    def _series_xy_for_fit(self, scan, slice_index, roi):
        """Return cleaned x, y, errors ready for curve_fit."""
        x, y = scan.plots.get_data(slice_index, roi)
        if not scan.metadata["key_sets"]:
            x = np.arange(len(y))
        errors = None
        if "survival" in scan.metadata["function"].lower():
            x, y, errors = self.get_survival_rate(x, y)
            errors = (errors[0] + errors[1]) / 2
        mask = ~np.isnan(y)
        x = np.asarray(x)[mask]
        y = np.asarray(y)[mask]
        if errors is not None:
            errors = np.asarray(errors)[mask]
            errors = np.where(np.isfinite(errors) & (errors > 0), errors, np.nan)
            emask = np.isfinite(errors)
            x, y, errors = x[emask], y[emask], errors[emask]
        return x, y, errors

    def perform_fit(self, arg, func=None, guess=None, fixed=None, dock=None):
        """Fit curves on the given (or focused) dock (Curves / export / derived / batch)."""
        dock = dock or self.focused_plot_dock()
        kind = dock.state.kind if dock else "curves"
        if kind in ("2d", "heatmap"):
            self.statusBar().showMessage("Fit applies to curve docks only", 4000)
            logging.warning("Fit applies to curve docks only")
            return
        if func is None:
            if getattr(dock, "ctrls", None) is not None and getattr(dock.ctrls, "cob_fit", None):
                func = dock.ctrls.cob_fit.currentText()
            else:
                func = self._curves_fitfunc()
        if func == "Custom":
            logging.error("Custom fits require the advanced fit dialog with a registered function")
            return
        if func not in self.fitter.func_dict:
            logging.error("Unknown fit function: %s", func)
            return

        if kind == "curves":
            self._perform_fit_curves(func, guess, fixed)
        elif kind.startswith("derived"):
            self._perform_fit_derived(dock, func, guess, fixed)
        elif kind == "batch" and dock.state.cached_xy is None and dock.state.panel_series:
            self._perform_fit_batch_panels(dock, func, guess, fixed)
        else:
            self._perform_fit_snapshot(dock, func, guess, fixed)

    def _perform_fit_curves(self, func, guess=None, fixed=None):
        scan_idxs = self.selected_scan_indices()
        roi_idxs = self.selected_roi_indices()
        if not scan_idxs or not self.scans:
            logging.error("No data to fit")
            return
        self.clear_fit(self.curves_dock)
        params = self.fitter.func_dict[func]["params"]
        slice_index = self.scrollbar.value()
        failures = []
        n_ok = 0
        color_i = 0

        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None or not scan.plots.plots:
                failures.append(f"{scan.name}: no plots")
                color_i += len(roi_idxs)
                continue
            if slice_index >= scan.plots.n_plots():
                failures.append(f"{scan.name}: missing slice {slice_index}")
                color_i += len(roi_idxs)
                continue
            for roi in roi_idxs:
                if roi >= scan.plots.n_rois:
                    failures.append(f"{scan.name}: missing ROI {roi}")
                    color_i += 1
                    continue
                try:
                    x, y, errors = self._series_xy_for_fit(scan, slice_index, roi)
                    if len(x) < 2:
                        raise ValueError("not enough finite points")
                    xf, yf, popt, perr = self.fitter.perform_fit(
                        x, y, func, errors, guess, fixed
                    )
                    color = OVERLAY_COLORS[color_i % len(OVERLAY_COLORS)]
                    fit = self.pw_plot.plot(xf, yf, pen=mkPen(color, width=2))
                    scan.plots.add_fit(slice_index, roi, fit, params, popt, perr)
                    n_ok += 1
                except Exception as exc:
                    logging.exception("Fit failed for %s ROI %s", scan.name, roi)
                    failures.append(f"{scan.name}/ROI{roi}: {exc}")
                color_i += 1

        self.draw_plot()
        if failures and n_ok == 0:
            msg = f"Fit failed: {failures[0]}"
            self.lb_fit.setText(msg)
            self.statusBar().showMessage(msg, 6000)
        elif failures:
            logging.warning("Some fits failed: %s", failures)
            self.statusBar().showMessage(f"{n_ok} fits ok, {len(failures)} failed", 5000)

    def _perform_fit_derived(self, dock, func, guess=None, fixed=None):
        scan = dock.state.derived_scan
        if scan is None or scan.plots is None:
            logging.error("No derived data to fit")
            return
        params = self.fitter.func_dict[func]["params"]
        slice_index = min(dock.state.slice or 0, max(scan.plots.n_plots() - 1, 0))
        pw = dock.plot_widget
        # Drop previous fits on this derived scan before adding new ones
        for i in range(scan.plots.n_plots()):
            for roi in range(scan.plots.n_rois):
                scan.plots.remove_fit(i, roi)
        failures, n_ok = [], 0
        for roi in range(scan.plots.n_rois):
            try:
                x, y, errors = self._series_xy_for_fit(scan, slice_index, roi)
                xf, yf, popt, perr = self.fitter.perform_fit(x, y, func, errors, guess, fixed)
                color = OVERLAY_COLORS[roi % len(OVERLAY_COLORS)]
                fit = pw.plot(xf, yf, pen=mkPen(color, width=2))
                scan.plots.add_fit(slice_index, roi, fit, params, popt, perr)
                n_ok += 1
            except Exception as exc:
                failures.append(f"ROI{roi}: {exc}")
        dock.state.fitfunc = func
        self._redraw_derived_dock(dock)
        if failures and n_ok == 0:
            self.statusBar().showMessage(f"Fit failed: {failures[0]}", 6000)
        elif n_ok:
            self.statusBar().showMessage(f"Fit ok ({n_ok})", 3000)

    def _perform_fit_batch_panels(self, dock, func, guess=None, fixed=None):
        """Fit every series stored on a multi-panel Batch (vs time) dock."""
        panels = dock.state.panel_series or []
        n_ok, failures = 0, []
        for panel in panels:
            label = panel.get("label", "?")
            panel.pop("fit_x", None)
            panel.pop("fit_y", None)
            try:
                x = np.asarray(panel["x"], dtype=float)
                y = np.asarray(panel["y"], dtype=float)
                yerr = panel.get("yerr")
                if yerr is not None:
                    yerr = np.asarray(yerr, dtype=float)
                    yerr = np.where(np.isfinite(yerr) & (yerr > 0), yerr, np.nan)
                xf, yf, popt, perr = self.fitter.perform_fit(x, y, func, yerr, guess, fixed)
                if panel.get("plot_item") is None:
                    raise ValueError("missing plot_item")
                panel["fit_x"] = np.asarray(xf, dtype=float)
                panel["fit_y"] = np.asarray(yf, dtype=float)
                n_ok += 1
            except Exception as exc:
                failures.append(f"{label}: {exc}")
        self._redraw_batch_panels(dock)
        if failures and n_ok == 0:
            msg = f"Fit failed: {failures[0]}"
            logging.warning(msg)
            self.statusBar().showMessage(msg, 6000)
        elif failures:
            logging.warning("Some batch-panel fits failed: %s", failures)
            self.statusBar().showMessage(f"{n_ok} fits ok, {len(failures)} failed", 5000)
        else:
            self.statusBar().showMessage(f"Fit ok ({n_ok} series on batch dock)", 4000)

    def _perform_fit_snapshot(self, dock, func, guess=None, fixed=None):
        cached = dock.state.cached_xy
        if cached is None:
            msg = (
                f"Fit needs curve data on this dock; "
                f"'{dock.windowTitle()}' has no cached curve / panel series"
            )
            logging.warning(msg)
            self.statusBar().showMessage(msg, 6000)
            return
        x, y, heights, y_centers = cached
        yerr = np.asarray(heights, dtype=float) / 2.0
        try:
            xf, yf, popt, perr = self.fitter.perform_fit(x, y, func, yerr, guess, fixed)
        except Exception as exc:
            logging.exception("Snapshot fit failed")
            self.statusBar().showMessage(f"Fit failed: {exc}", 6000)
            return
        if not isinstance(dock.plot_widget, PlotWidget):
            msg = "Fit overlay needs a PlotWidget dock (not multi-panel layout)"
            self.statusBar().showMessage(msg, 5000)
            return
        dock.state.fit_overlays = [{
            "x": np.asarray(xf, dtype=float),
            "y": np.asarray(yf, dtype=float),
            "color": OVERLAY_COLORS[1],
        }]
        dock.state.fitfunc = func
        self._redraw_snapshot_dock(dock)
        params = self.fitter.func_dict[func]["params"]
        summary = ", ".join(
            f"{p}={self.format_fit_params(v, e)}" for p, v, e in zip(params, popt, perr)
        )
        self.statusBar().showMessage(f"Fit: {summary}", 8000)

    def advanced_fit(self):
        """Legacy entry — route to focused dock."""
        self._dock_advanced_fit(self.focused_plot_dock())

    def two_d_plot(self):
        n = self.scrollbar.maximum()+1
        survival = "survival" in self.metadata["function"].lower()
        slices = []
        for i in range(n):
            self.scrollbar.setValue(i)
            x, y = self.get_current_data()
            if survival:
                unique_x, means, _ = self.get_survival_rate(x, y)
            else:
                unique_x, means, _ = self.get_errorbars(x, y)
            slices.append((unique_x, means))

        # slices can have different numbers of unique x values so align them onto the
        # union of x values instead of assuming matching shape/order
        global_x = np.unique(np.concatenate([unique_x for unique_x, _ in slices]))
        data = np.full((n, len(global_x)), np.nan)
        for row, (unique_x, means) in enumerate(slices):
            data[row, np.searchsorted(global_x, unique_x)] = means

        # self.req.set_dataset("2d_plot", np.nan_to_num(data), persist=True)

        # row keys are only a single real-valued scan parameter for a true
        # 2D scan (one non-x key); otherwise fall back to plotting by index
        row_keys = list(self.plots.plots.keys())
        if row_keys and len(row_keys[0]) == 1:
            y_vals = np.array([k[0] for k in row_keys])
        else:
            y_vals = np.arange(n)

        x_step = (global_x[-1] - global_x[0]) / (len(global_x) - 1) if len(global_x) > 1 else 1
        y_step = (y_vals[-1] - y_vals[0]) / (len(y_vals) - 1) if len(y_vals) > 1 else 1
        x0 = float(global_x[0] - x_step / 2)
        y0 = float(y_vals[0] - y_step / 2)
        w = float(global_x[-1] - global_x[0] + x_step)
        h = float(y_vals[-1] - y_vals[0] + y_step)

        y_keys = [key[-1] for i, key in enumerate(self.metadata["key_sets"]) if i != self.plots.x_index]
        glw = self._add_image_with_colorbar(
            data,
            x0, y0, w, h,
            xlabel=self.cob_x.currentText(),
            ylabel=", ".join(y_keys) if y_keys else "index",
        )
        recipe = {
            "type": "2d",
            "title": "2D plot",
            "scan_sel": self.selected_scan_indices(),
            "roi_sel": self.selected_roi_indices(),
            "x_index": self.cob_x.currentIndex(),
            "slice": self.scrollbar.value(),
        }
        dock = self.show_plot_dock("2D plot", glw, kind="2d", recipe=recipe)
        self._record_tab_recipe(recipe, dock=dock)
        self.copy_widget_to_clipboard(glw)

    def export_plot(self):
        x, y = self.get_current_data()

        if "survival" in self.metadata["function"].lower():
            unique_x, means, err = self.get_survival_rate(x, y)
            lower = err[0]
            upper = err[1]
            heights = lower + upper
            y_centers = means + (upper - lower) / 2
            clip_heights = heights / 2
        else:
            unique_x, means, err = self.get_errorbars(x, y)
            heights = err * 2
            y_centers = means
            clip_heights = err

        self.copy_to_clipboard(x=unique_x, y=means, yerr=clip_heights, yerr_center=y_centers)
        pw = self._new_plot_widget()
        pw.setLabel("bottom", self.cob_x.currentText(), **font)
        pw.setLabel("left", self.metadata["function"], **font)
        recipe = {
            "type": "export",
            "title": "Export",
            "scan_sel": self.selected_scan_indices(),
            "roi_sel": self.selected_roi_indices(),
            "x_index": self.cob_x.currentIndex(),
            "slice": self.scrollbar.value(),
        }
        dock = self.show_plot_dock("Export", pw, kind="export", recipe=recipe)
        dock.state.cached_xy = (unique_x, means, heights, y_centers)
        # Carry over Curves dock view prefs so Export starts matching what you see
        src = self.curves_dock.state
        dock.state.style = dict(src.style)
        dock.state.show_legend = bool(src.show_legend)
        dock.state.show_errorbars = bool(src.show_errorbars)
        dock.state.fitfunc = src.fitfunc or self._curves_fitfunc()
        self._sync_dock_ctrls_from_state(dock)
        self._record_tab_recipe(recipe, dock=dock)

        fit, _, _, _ = self.plots.get_fit(*self.get_plot_indices())
        if fit:
            try:
                fx, fy = fit.getData()
                dock.state.fit_overlays = [{
                    "x": np.asarray(fx, dtype=float),
                    "y": np.asarray(fy, dtype=float),
                    "color": OVERLAY_COLORS[1],
                }]
            except Exception:
                dock.state.fit_overlays = []
        self._redraw_snapshot_dock(dock)

    def copy_to_clipboard(self, **kwargs):
        """Copy equal-length 1D columns as TSV (header + rows)."""
        keys = list(kwargs.keys())
        cols = [np.asarray(kwargs[k]).ravel() for k in keys]
        if not cols:
            raise ValueError("nothing to copy")
        n = min(len(c) for c in cols)
        if n == 0:
            raise ValueError("empty columns")
        lines = ["\t".join(keys)]
        for i in range(n):
            lines.append("\t".join(str(cols[k][i]) for k in range(len(cols))))
        QApplication.clipboard().setText("\n".join(lines) + "\n")

    def _plot_param_scatter(self, scan, fitfunc, param_name, mode, recipe_extra=None):
        """Fit-param style plot for one scan: vs secondary axis or vs ROI."""
        if scan.plots is None or not scan.plots.plots:
            logging.error("No plot data for %s", scan.name)
            return False
        fit_params = self.fitter.func_dict[fitfunc]["params"]
        if param_name not in fit_params:
            logging.error("Unknown param %s", param_name)
            return False
        fit_value = fit_params.index(param_name)
        selected_rois = self.selected_roi_indices()
        slice_index = self.scrollbar.value()
        x_index = scan.plots.x_index

        if mode == BatchAnalyzeDialog.MODE_ROI:
            index = [slice_index] * len(selected_rois)
            roi = selected_rois
            xs = list(selected_rois)
            xlabel = "ROI"
        else:
            # vs secondary scan axis (first non-x key dimension in plot keys)
            axis_labels = [key[-1] for i, key in enumerate(scan.metadata["key_sets"]) if i != x_index]
            if not axis_labels:
                logging.error("Scan has no secondary axis for vs-axis mode")
                return False
            xlabel = axis_labels[0]
            index = list(range(scan.plots.n_plots()))
            roi = [selected_rois[0]] * len(index)
            xs = [list(k)[0] if len(k) else i for i, k in enumerate(scan.plots.plots.keys())]

        ys, yerr, failures = [], [], []
        for i, r in zip(index, roi):
            xp, yp = scan.plots.get_data(i, r)
            if "survival" in scan.metadata["function"].lower():
                xp, yp, _ = self.get_survival_rate(xp, yp)
                xp = xp[~np.isnan(yp)]
                yp = yp[~np.isnan(yp)]
            try:
                x, y, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc)
                ys.append(popt[fit_value])
                yerr.append(perr[fit_value])
                fit = self.pw_plot.plot(x, y)
                scan.plots.add_fit(i, r, fit, fit_params, popt, perr)
            except Exception as exc:
                failures.append(f"slice={i} ROI={r}: {exc}")
                ys.append(np.nan)
                yerr.append(np.nan)

        if failures:
            logging.warning("Fit param issues (%s):\n%s", len(failures), "\n".join(failures[:10]))
        if not any(np.isfinite(ys)):
            logging.error("Fit param: no valid fits")
            return False

        self.draw_plot()
        self.copy_to_clipboard(x=xs, y=ys, yerr=yerr)
        pw = self._new_plot_widget()
        pw.showGrid(x=True, y=True, alpha=0.3)
        pw.setLabel("bottom", xlabel, **font)
        pw.setLabel("left", param_name, **font)
        xs_arr = np.asarray(xs, dtype=float)
        ys_arr = np.asarray(ys, dtype=float)
        yerr_arr = np.asarray(yerr, dtype=float)
        finite = np.isfinite(ys_arr)
        self._add_errorbar_scatter(
            pw.getPlotItem(),
            xs_arr[finite],
            ys_arr[finite],
            np.nan_to_num(yerr_arr[finite], nan=0.0) * 2,
            y_centers=ys_arr[finite],
        )
        title = f"Fit param: {param_name} ({mode})"
        recipe = {
            "type": "batch",
            "title": title,
            "mode": mode,
            "fitfunc": fitfunc,
            "params": [param_name],
            "scan_name": scan.name,
            "roi_sel": selected_rois,
            "slice": slice_index,
            "x_index": self.cob_x.currentIndex(),
        }
        if recipe_extra:
            recipe.update(recipe_extra)
        dock = self.show_plot_dock(title, pw, kind="batch", recipe=recipe)
        dock.state.cached_xy = (
            xs_arr[finite],
            ys_arr[finite],
            np.nan_to_num(yerr_arr[finite], nan=0.0) * 2,
            ys_arr[finite],
        )
        self._record_tab_recipe(recipe, dock=dock)
        self._redraw_snapshot_dock(dock)
        return True

    def generate_heatmap(self, recipe=None):
        # Qt clicked(bool) may pass False — only dict recipes are valid
        if not isinstance(recipe, dict):
            recipe = None
        scan_idxs = self.selected_scan_indices()
        if not scan_idxs:
            logging.error("Select at least one scan for heatmap")
            return
        labels = [self.scans[i].name for i in scan_idxs]
        # Use first selected scan's ROI count for dialog defaults; update after pick
        probe = self.scans[scan_idxs[0]]
        if probe.plots is None:
            logging.error("No plots for selected scan")
            return
        fitfunc = self._curves_fitfunc()
        fit_params = self.fitter.func_dict[fitfunc]["params"]

        if recipe is None:
            dialog = HeatmapDialog(fit_params, probe.plots.n_rois, scan_labels=labels, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            fit_value, num_rows, num_cols, label_i = dialog.get_values()
            if num_rows is None or num_cols is None:
                return
            scan = self.scans[scan_idxs[label_i]]
            param_name = fit_params[fit_value]
        else:
            scan = next((s for s in self.scans if s.name == recipe.get("scan_name")), None)
            if scan is None and scan_idxs:
                scan = self.scans[scan_idxs[0]]
            if scan is None:
                logging.error("Heatmap recipe: scan not found")
                return
            param_name = recipe["param"]
            fit_value = fit_params.index(param_name) if param_name in fit_params else 0
            num_rows, num_cols = recipe["rows"], recipe["cols"]
            fitfunc = recipe.get("fitfunc", fitfunc)

        if scan.plots is None or scan.plots.n_rois < 2:
            logging.error("Cannot make heatmap of global / single-ROI data")
            return

        n_rois = scan.plots.n_rois
        slice_index = self.scrollbar.value() if recipe is None else recipe.get("slice", 0)
        ys, failures = [], []
        for r in range(n_rois):
            xp, yp = scan.plots.get_data(min(slice_index, scan.plots.n_plots() - 1), r)
            if "survival" in scan.metadata["function"].lower():
                xp, yp, _ = self.get_survival_rate(xp, yp)
                xp = xp[~np.isnan(yp)]
                yp = yp[~np.isnan(yp)]
            try:
                x, y, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc)
                ys.append(popt[fit_value])
                fit = self.pw_plot.plot(x, y)
                scan.plots.add_fit(slice_index, r, fit, fit_params, popt, perr)
            except Exception as exc:
                failures.append(f"ROI {r}: {exc}")
                ys.append(np.nan)

        if failures:
            logging.warning("Heatmap fit issues (%s/%s ROIs):\n%s",
                            len(failures), n_rois, "\n".join(failures[:10]))
        if not any(np.isfinite(ys)):
            logging.error("Heatmap: no ROI produced a valid fit")
            return
        if num_rows * num_cols != len(ys):
            logging.error(
                "Heatmap grid %sx%s != %s ROIs; adjust rows/cols in dialog",
                num_rows, num_cols, len(ys),
            )
            return

        self.draw_plot()
        arr = np.asarray(ys, dtype=float).reshape((num_rows, num_cols))
        keys = [f"c{i}" for i in range(num_cols)]
        self.copy_to_clipboard(**dict(zip(keys, arr.T)))
        short = self._scan_short_name(scan)
        glw = self._add_image_with_colorbar(
            arr,
            -0.5, -0.5, num_cols, num_rows,
            xlabel="col",
            ylabel="row",
            title=f"{param_name} — {short}",
        )
        title = f"Heatmap: {param_name} ({short})"
        rec = recipe if isinstance(recipe, dict) else {
            "type": "heatmap",
            "title": title,
            "scan_name": scan.name,
            "param": param_name,
            "fitfunc": fitfunc,
            "rows": num_rows,
            "cols": num_cols,
            "slice": slice_index,
            "x_index": self.cob_x.currentIndex(),
        }
        rec.setdefault("title", title)
        dock = self.show_plot_dock(title, glw, kind="heatmap", recipe=rec)
        self._record_tab_recipe(rec, dock=dock)

    def count_clusters(self, values, threshold=3):
        if len(values) == 1:
            return 1
        values = np.sort(values).reshape(-1, 1)
        clusters = fcluster(linkage(values, method="single"), threshold, criterion="distance")
        return len(np.unique(clusters))

    def import_data(self):
        dialog = ScanPickerDialog(FOLDER, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_scans = []
        failures = []
        csv_pair = dialog.selected_csv_pair()
        if csv_pair is not None:
            paths = [Path(p) for p in csv_pair]
            results = next((p for p in paths if "result" in p.name.lower()), paths[1])
            log = next((p for p in paths if p != results), paths[0])
            try:
                new_scans.append(load_scan_pair(log, results))
            except Exception as exc:
                logging.exception("Invalid CSV pair imported")
                failures.append(str(exc))
        else:
            dirs = dialog.selected_scan_dirs() or []
            if not dirs:
                logging.error("No scan folders selected")
                return
            for d in dirs:
                try:
                    new_scans.append(load_scan_pair(d / "log.csv", d / "logResults.csv"))
                except Exception as exc:
                    logging.exception("Skipping invalid scan %s", d)
                    failures.append(f"{d}: {exc}")
        if not new_scans:
            logging.error("No scans loaded%s", f": {failures}" if failures else "")
            return
        if failures:
            logging.warning("Some scans failed to load: %s", failures)

        start = len(self.scans)
        self.scans.extend(new_scans)
        select = list(range(start, len(self.scans)))
        # if first load, select all; if appending, select the new ones plus previous selection
        prev = self.selected_scan_indices() if start > 0 else []
        self.rebuild_scan_list_widget(select_indices=sorted(set(prev) | set(select)))
        self.create_new_plot()

    def merge_selected_scans(self):
        """Non-destructive: concatenate into a derived floating dock (not Loaded scans)."""
        idxs = self.selected_scan_indices()
        n_loaded_before = len(self.scans)
        if len(idxs) < 2:
            logging.error("Select at least two scans to merge")
            return
        scans = [self.scans[i] for i in idxs]
        ref = scans[0]
        for s in scans[1:]:
            if s.metadata["key_sets"] != ref.metadata["key_sets"]:
                logging.error("Cannot merge: key_sets differ (%s vs %s)", ref.name, s.name)
                return
            if s.metadata["function"] != ref.metadata["function"]:
                logging.error("Cannot merge: function differ (%s vs %s)", ref.name, s.name)
                return
            if ref.data and s.data and len(s.data[0]) != len(ref.data[0]):
                logging.error("Cannot merge: ROI count differs (%s vs %s)", ref.name, s.name)
                return

        merged = ScanDataset(
            name="merged_" + "_".join(self._scan_short_name(s) for s in scans),
            path=ref.path,
            metadata={
                "key_sets": copy.deepcopy(ref.metadata["key_sets"]),
                "points": [],
                "function": ref.metadata["function"],
            },
            data=[],
        )
        for s in scans:
            merged.metadata["points"].extend(copy.deepcopy(s.metadata["points"]))
            merged.data.extend(copy.deepcopy(s.data))

        title = "Merge: " + "+".join(self._scan_short_name(s) for s in scans[:3])
        if len(scans) > 3:
            title += f"+{len(scans) - 3}"
        self._open_derived_dock(merged, kind="derived_merge", title=title)

    def _sync_dock_ctrls_from_state(self, dock):
        """Push PlotViewState into that dock's control widgets (no redraw signals)."""
        ctrls = getattr(dock, "ctrls", None)
        if ctrls is None:
            return
        st = dock.state
        if getattr(ctrls, "chk_legend", None):
            ctrls.chk_legend.blockSignals(True)
            ctrls.chk_legend.setChecked(bool(st.show_legend))
            ctrls.chk_legend.blockSignals(False)
        if getattr(ctrls, "chk_errorbars", None):
            ctrls.chk_errorbars.blockSignals(True)
            ctrls.chk_errorbars.setChecked(bool(st.show_errorbars))
            ctrls.chk_errorbars.blockSignals(False)
        if getattr(ctrls, "cob_style", None):
            mode = st.style.get("mode", "Scatter")
            i = ctrls.cob_style.findText(mode)
            if i >= 0:
                ctrls.cob_style.blockSignals(True)
                ctrls.cob_style.setCurrentIndex(i)
                ctrls.cob_style.blockSignals(False)
        st.style["linewidth"] = 2.0
        if getattr(ctrls, "cob_fit", None) and st.fitfunc:
            i = ctrls.cob_fit.findText(st.fitfunc)
            if i >= 0:
                ctrls.cob_fit.blockSignals(True)
                ctrls.cob_fit.setCurrentIndex(i)
                ctrls.cob_fit.blockSignals(False)

    def _open_derived_dock(self, scan, kind, title):
        x_index = self.cob_x.currentIndex() if self.cob_x.count() else 0
        self.build_plots_for_scan(scan, x_index)
        pw = self._new_plot_widget()
        dock = self.show_plot_dock(title, pw, kind=kind, floating=True)
        dock.state.derived_scan = scan
        dock.state.x_index = x_index
        dock.state.slice = 0
        src = self.curves_dock.state
        dock.state.style = dict(src.style)
        dock.state.show_legend = bool(src.show_legend)
        dock.state.show_errorbars = bool(src.show_errorbars)
        dock.state.fitfunc = src.fitfunc or self._curves_fitfunc()
        self._sync_dock_ctrls_from_state(dock)
        recipe = {
            "type": kind,
            "title": dock.windowTitle(),
            "source_names": [scan.name],
            "x_index": x_index,
        }
        self._record_tab_recipe(recipe, dock=dock)
        self._redraw_derived_dock(dock)
        return dock

    def _redraw_derived_dock(self, dock):
        scan = dock.state.derived_scan
        if scan is None or scan.plots is None:
            return
        pw = dock.plot_widget
        if not isinstance(pw, PlotWidget):
            return
        pw.clear()
        dock.state.series_items = []
        slice_index = min(dock.state.slice or 0, max(scan.plots.n_plots() - 1, 0))
        show_legend = dock.state.show_legend
        if show_legend:
            pw.addLegend()
        n_rois = scan.plots.n_rois
        for roi in range(n_rois):
            x, y = scan.plots.get_data(slice_index, roi)
            if not scan.metadata["key_sets"]:
                x = np.arange(len(y))
            name = f"ROI {roi}" if show_legend and n_rois > 1 else (f"ROI {roi}" if show_legend else None)
            style = self._series_style(roi)
            if "survival" in scan.metadata["function"].lower():
                self.plot_survival(x, y, style=style, name=name, plot_widget=pw, view_state=dock.state)
            else:
                self.plot_errorbars(x, y, style=style, name=name, plot_widget=pw, view_state=dock.state)
        # Re-attach fits stored on the derived scan (same pattern as Curves draw_plot)
        for roi in range(n_rois):
            fit, *_ = scan.plots.get_fit(slice_index, roi)
            if not fit:
                continue
            try:
                pw.addItem(fit)
            except Exception:
                try:
                    fx, fy = fit.getData()
                    color = OVERLAY_COLORS[roi % len(OVERLAY_COLORS)]
                    pw.plot(fx, fy, pen=mkPen(color, width=2))
                except Exception:
                    pass
        pw.setLabel("left", scan.metadata.get("function", ""), **font)
        if scan.metadata.get("key_sets") and dock.state.x_index is not None:
            keys = scan.metadata["key_sets"]
            if 0 <= dock.state.x_index < len(keys):
                pw.setLabel("bottom", keys[dock.state.x_index][-1], **font)

    def _redraw_snapshot_dock(self, dock):
        cached = dock.state.cached_xy
        if cached is None:
            return
        pw = dock.plot_widget
        if not isinstance(pw, PlotWidget):
            return
        x, y, heights, y_centers = cached
        pw.clear()
        dock.state.series_items = []
        if dock.state.show_legend:
            pw.addLegend()
        self._add_errorbar_scatter(
            pw.getPlotItem(), x, y, heights, y_centers=y_centers,
            style=self._series_style(0), name="data" if dock.state.show_legend else None,
            view_style=dock.state.style,
            show_errorbars=dock.state.show_errorbars,
            series_out=dock.state.series_items,
        )
        self._apply_fit_overlays(dock)

    def _redraw_batch_panels(self, dock):
        """Restyle / restore data + fit overlays on a multi-panel Batch dock."""
        panels = dock.state.panel_series or []
        if not panels:
            return
        view_style = dock.state.style or default_plot_style()
        show_eb = bool(dock.state.show_errorbars)
        by_plot = {}
        for panel in panels:
            p = panel.get("plot_item")
            if p is None:
                continue
            by_plot.setdefault(p, []).append(panel)
        first_plot = next(iter(by_plot), None)
        for plot_item, group in by_plot.items():
            plot_item.clear()
            plot_item.showGrid(x=True, y=True, alpha=0.3)
            if dock.state.show_legend and plot_item is first_plot:
                plot_item.addLegend()
            for color_i, panel in enumerate(group):
                xs = np.asarray(panel["x"], dtype=float)
                ys = np.asarray(panel["y"], dtype=float)
                yerr = panel.get("yerr")
                heights = np.zeros_like(ys) if yerr is None else np.asarray(yerr, dtype=float) * 2
                color = OVERLAY_COLORS[color_i % len(OVERLAY_COLORS)]
                self._add_errorbar_scatter(
                    plot_item, xs, ys, heights, y_centers=ys,
                    style={"symbolBrush": color},
                    name=panel.get("label") if dock.state.show_legend else None,
                    view_style=view_style,
                    show_errorbars=show_eb,
                )
                if panel.get("fit_x") is not None and panel.get("fit_y") is not None:
                    plot_item.plot(
                        np.asarray(panel["fit_x"], dtype=float),
                        np.asarray(panel["fit_y"], dtype=float),
                        pen=mkPen(OVERLAY_COLORS[1], width=2),
                    )

    def batch_fit_selected(self, recipe=None):
        # Qt clicked(bool) may pass False as first arg — only dict recipes are valid
        if not isinstance(recipe, dict):
            recipe = None
        scan_idxs = self.selected_scan_indices()
        roi_idxs = self.selected_roi_indices()
        if not scan_idxs:
            logging.error("No scans selected for batch fit")
            return
        fitfunc = self._curves_fitfunc()
        if fitfunc not in self.fitter.func_dict:
            logging.error("Unknown fit function: %s", fitfunc)
            return
        fit_params = self.fitter.func_dict[fitfunc]["params"]
        labels = [self.scans[i].name for i in scan_idxs]

        if recipe is None:
            dialog = BatchAnalyzeDialog(fit_params, labels, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            mode = dialog.selected_mode()
            plot_params = [p for p in fit_params if p in dialog.selected_params()]
            target_label_i = dialog.selected_scan_index()
            if not plot_params:
                return
        else:
            mode = recipe.get("mode", BatchAnalyzeDialog.MODE_TIME)
            plot_params = recipe.get("params") or fit_params[:1]
            fitfunc = recipe.get("fitfunc", fitfunc)
            fit_params = self.fitter.func_dict[fitfunc]["params"]
            name = recipe.get("scan_name")
            target_label_i = 0
            if name:
                for j, lab in enumerate(labels):
                    if lab == name:
                        target_label_i = j
                        break
            roi_idxs = recipe.get("roi_sel", roi_idxs)

        if mode in (BatchAnalyzeDialog.MODE_AXIS, BatchAnalyzeDialog.MODE_ROI):
            scan = self.scans[scan_idxs[target_label_i]]
            for param in plot_params:
                self._plot_param_scatter(scan, fitfunc, param, mode)
            return

        # --- vs time (original batch fit) ---
        slice_index = self.scrollbar.value() if recipe is None else recipe.get("slice", self.scrollbar.value())
        rows, failures = [], []
        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None or not scan.plots.plots:
                failures.append(f"{scan.name}: no plots")
                continue
            if slice_index >= scan.plots.n_plots():
                failures.append(f"{scan.name}: missing slice {slice_index}")
                continue
            ts = parse_scan_timestamp(scan.path)
            for roi in roi_idxs:
                if roi >= scan.plots.n_rois:
                    failures.append(f"{scan.name}: missing ROI {roi}")
                    continue
                xp, yp = scan.plots.get_data(slice_index, roi)
                try:
                    if "survival" in scan.metadata["function"].lower():
                        xp, yp, errors = self.get_survival_rate(xp, yp)
                        errors = (errors[0] + errors[1]) / 2
                        errors = errors[~np.isnan(yp)]
                    else:
                        errors = None
                    xp = xp[~np.isnan(yp)]
                    yp = yp[~np.isnan(yp)]
                    if len(xp) < 2:
                        raise ValueError("not enough points")
                    _, _, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc, errors)
                    row = {
                        "scan": scan.name,
                        "roi": roi,
                        "time": ts.isoformat(sep=" ") if ts else "",
                        "t_index": s_i,
                    }
                    for p, v, e in zip(fit_params, popt, perr):
                        row[p] = v
                        row[f"{p}_err"] = e
                    rows.append(row)
                except Exception as exc:
                    failures.append(f"{scan.name} ROI {roi}: {exc}")
                    row = {
                        "scan": scan.name,
                        "roi": roi,
                        "time": ts.isoformat(sep=" ") if ts else "",
                        "t_index": s_i,
                    }
                    for p in fit_params:
                        row[p] = np.nan
                        row[f"{p}_err"] = np.nan
                    rows.append(row)

        if failures:
            logging.warning("Batch fit issues:\n%s", "\n".join(failures))
        if not rows:
            logging.error("Batch fit produced no rows")
            return

        headers = ["scan", "roi", "time"] + [h for p in fit_params for h in (p, f"{p}_err")]
        lines = ["\t".join(headers)]
        for row in rows:
            lines.append("\t".join(str(row.get(h, "")) for h in headers))
        QApplication.clipboard().setText("\n".join(lines) + "\n")

        n_params = len(plot_params)
        use_time = any(row["time"] for row in rows)
        glw = GraphicsLayoutWidget()
        first_plot = None
        panel_series = []
        for ax_i, param in enumerate(plot_params):
            if use_time:
                p = glw.addPlot(
                    row=ax_i, col=0,
                    axisItems={"bottom": DateAxisItem(orientation="bottom")},
                )
            else:
                p = glw.addPlot(row=ax_i, col=0)
            self._style_plot_item(p)
            p.showGrid(x=True, y=True, alpha=0.3)
            p.setLabel("left", param, **font)
            if ax_i == 0:
                p.addLegend()
                p.setTitle(f"Batch fit: {fitfunc} vs time", **font)
                first_plot = p
            else:
                p.setXLink(first_plot)
            if ax_i == n_params - 1:
                p.setLabel("bottom", "scan time", **font)
            else:
                p.getAxis("bottom").setStyle(showValues=False)

            for color_i, roi in enumerate(roi_idxs):
                points = []
                for row in rows:
                    if row["roi"] != roi:
                        continue
                    if row["time"]:
                        xval = datetime.fromisoformat(row["time"]).timestamp()
                    else:
                        xval = float(row["t_index"])
                    points.append((xval, row[param], row[f"{param}_err"]))
                if not points:
                    continue
                points.sort(key=lambda t: t[0])
                xs = np.array([pt[0] for pt in points], dtype=float)
                ys = np.array([pt[1] for pt in points], dtype=float)
                yerr = np.array([pt[2] for pt in points], dtype=float)
                color = OVERLAY_COLORS[color_i % len(OVERLAY_COLORS)]
                self._add_errorbar_scatter(
                    p, xs, ys, yerr * 2, y_centers=ys,
                    style={"symbolBrush": color},
                    name=f"ROI {roi}" if ax_i == 0 else None,
                    view_style=default_plot_style(),
                    show_errorbars=True,
                )
                panel_series.append({
                    "plot_item": p,
                    "x": xs,
                    "y": ys,
                    "yerr": yerr,
                    "label": f"{param}/ROI{roi}",
                })
        title = f"Batch fit: {fitfunc}"
        rec = {
            "type": "batch",
            "title": title,
            "mode": BatchAnalyzeDialog.MODE_TIME,
            "fitfunc": fitfunc,
            "params": plot_params,
            "scan_sel_names": [self.scans[i].name for i in scan_idxs],
            "roi_sel": roi_idxs,
            "slice": slice_index,
            "x_index": self.cob_x.currentIndex(),
        }
        dock = self.show_plot_dock(title, glw, kind="batch", recipe=rec)
        dock.state.panel_series = panel_series
        self._record_tab_recipe(rec, dock=dock)

    def _scan_to_serializable(self, scan):
        """Embed scan metadata + data for zip package (portable)."""
        return {
            "name": scan.name,
            "path": str(scan.path),
            "metadata": scan.metadata,
            "data": scan.data,
        }

    def _scan_from_serializable(self, obj):
        return ScanDataset(
            name=obj["name"],
            path=Path(obj.get("path", "")),
            metadata=obj["metadata"],
            data=obj["data"],
            plots=None,
        )

    def refresh_experiment_list(self):
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        cur = self.list_experiments.currentItem()
        cur_name = cur.text() if cur else None
        self.list_experiments.clear()
        for z in sorted(EXPERIMENTS_DIR.glob("*.zip")):
            self.list_experiments.addItem(z.stem)
        if cur_name:
            matches = self.list_experiments.findItems(cur_name, Qt.MatchFlag.MatchExactly)
            if matches:
                self.list_experiments.setCurrentItem(matches[0])

    def _selected_experiment_path(self):
        item = self.list_experiments.currentItem()
        name = (item.text() if item else "") or self.ed_exp_name.text().strip()
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        if not name:
            return None
        return EXPERIMENTS_DIR / f"{name}.zip"

    def _on_experiment_double_clicked(self, item):
        self.ed_exp_name.setText(item.text())
        self.open_experiment()

    def save_experiment(self):
        if not self.scans:
            QMessageBox.warning(self, "Save experiment", "No scans loaded.")
            return
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        name = self.ed_exp_name.text().strip()
        if not name:
            item = self.list_experiments.currentItem()
            name = item.text() if item else ""
        if not name.strip():
            QMessageBox.warning(self, "Save experiment", "Enter a name in the Name field.")
            return
        name = re.sub(r'[<>:"/\\|?*]', "_", name.strip())
        self.ed_exp_name.setText(name)
        zip_path = EXPERIMENTS_DIR / f"{name}.zip"
        if zip_path.exists():
            ans = QMessageBox.question(
                self, "Overwrite?",
                f"{zip_path.name} already exists. Overwrite?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        dock_layout = base64.b64encode(bytes(self.saveState())).decode("ascii")
        payload = {
            "version": 2,
            "name": name,
            "saved_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "ui": {
                "x_index": self.cob_x.currentIndex(),
                "fitfunc": self._curves_fitfunc(),
                "slice": self.scrollbar.value(),
                "legend": self.curves_dock.state.show_legend,
                "style": copy.deepcopy(self.curves_dock.state.style),
                "scan_sel_names": [self.scans[i].name for i in self.selected_scan_indices()],
                "roi_sel": self.selected_roi_indices(),
            },
            "scans": [self._scan_to_serializable(s) for s in self.scans],
            "tabs": copy.deepcopy(self.tab_recipes),
            "dock_layout": dock_layout,
        }

        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("experiment.json", json.dumps(payload, indent=2, default=str))
            self.refresh_experiment_list()
            matches = self.list_experiments.findItems(name, Qt.MatchFlag.MatchExactly)
            if matches:
                self.list_experiments.setCurrentItem(matches[0])
        except Exception as exc:
            logging.exception("Save experiment failed")
            QMessageBox.critical(self, "Save failed", str(exc))

    def open_experiment(self):
        zip_path = self._selected_experiment_path()
        if zip_path is None or not zip_path.is_file():
            QMessageBox.warning(
                self, "Load experiment",
                "Select an experiment in the list (or type an existing name).",
            )
            return
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                raw = zf.read("experiment.json")
            payload = json.loads(raw)
        except Exception as exc:
            logging.exception("Open experiment failed")
            QMessageBox.critical(self, "Open failed", str(exc))
            return

        self.clear_scans()
        new_scans = []
        for obj in payload.get("scans", []):
            try:
                meta = obj["metadata"]
                meta["points"] = [tuple(p) if not isinstance(p, tuple) else p for p in meta.get("points", [])]
                meta["key_sets"] = [list(k) for k in meta.get("key_sets", [])]
                new_scans.append(self._scan_from_serializable(obj))
            except Exception as exc:
                logging.exception("Skipping scan in experiment: %s", exc)

        if not new_scans:
            QMessageBox.warning(self, "Open experiment", "No scans in package.")
            return

        self.scans = new_scans
        ui = payload.get("ui", {})
        name_to_i = {s.name: i for i, s in enumerate(self.scans)}
        sel = [name_to_i[n] for n in ui.get("scan_sel_names", []) if n in name_to_i]
        if not sel:
            sel = list(range(len(self.scans)))
        self.rebuild_scan_list_widget(select_indices=sel)
        self.create_new_plot()

        fitfunc = ui.get("fitfunc")
        if fitfunc and getattr(self.curves_dock, "ctrls", None) is not None:
            idx = self.curves_dock.ctrls.cob_fit.findText(fitfunc)
            if idx >= 0:
                self.curves_dock.ctrls.cob_fit.setCurrentIndex(idx)
            self.curves_dock.state.fitfunc = fitfunc
        x_index = ui.get("x_index", 0)
        if 0 <= x_index < self.cob_x.count():
            self.cob_x.setCurrentIndex(x_index)
            self.set_x(x_index)
        self.curves_dock.state.show_legend = bool(ui.get("legend", False))
        if isinstance(ui.get("style"), dict):
            self.curves_dock.state.style.update(ui["style"])
        self.curves_dock.state.style["linewidth"] = 2.0
        # Refresh Curves dock control widgets from restored state
        ctrls = getattr(self.curves_dock, "ctrls", None)
        if ctrls is not None:
            if getattr(ctrls, "chk_legend", None):
                ctrls.chk_legend.setChecked(self.curves_dock.state.show_legend)
            if getattr(ctrls, "chk_errorbars", None):
                ctrls.chk_errorbars.setChecked(self.curves_dock.state.show_errorbars)
            if getattr(ctrls, "cob_style", None):
                mode = self.curves_dock.state.style.get("mode", "Scatter")
                i = ctrls.cob_style.findText(mode)
                if i >= 0:
                    ctrls.cob_style.setCurrentIndex(i)
        slice_v = ui.get("slice", 0)
        self.scrollbar.setValue(min(slice_v, self.scrollbar.maximum()))
        roi_sel = set(ui.get("roi_sel", [0]))
        for i in range(self.list_roi.count()):
            self.list_roi.item(i).setSelected(i in roi_sel)

        recipes = payload.get("tabs", [])
        self.tab_recipes = []
        for rec in recipes:
            try:
                self._replay_tab_recipe(rec)
            except Exception as exc:
                logging.warning("Could not restore tab %s: %s", rec.get("type"), exc)

        layout_b64 = payload.get("dock_layout")
        if layout_b64:
            try:
                self.restoreState(QByteArray(base64.b64decode(layout_b64)))
            except Exception as exc:
                logging.warning("Could not restore dock layout: %s", exc)

        self.setWindowTitle(f"Scan Plot — {payload.get('name', zip_path.stem)}")
        self.ed_exp_name.setText(zip_path.stem)
        matches = self.list_experiments.findItems(zip_path.stem, Qt.MatchFlag.MatchExactly)
        if matches:
            self.list_experiments.setCurrentItem(matches[0])

    def _replay_tab_recipe(self, rec):
        rtype = rec.get("type")
        names = rec.get("scan_sel_names")
        if names:
            name_to_i = {s.name: i for i, s in enumerate(self.scans)}
            sel = [name_to_i[n] for n in names if n in name_to_i]
            if sel:
                self.rebuild_scan_list_widget(select_indices=sel)
                self.sync_primary_aliases()
        if "roi_sel" in rec:
            want = set(rec["roi_sel"])
            for i in range(self.list_roi.count()):
                self.list_roi.item(i).setSelected(i in want)
        if "slice" in rec:
            self.scrollbar.setValue(min(rec["slice"], self.scrollbar.maximum()))
        if "x_index" in rec and 0 <= rec["x_index"] < self.cob_x.count():
            self.cob_x.setCurrentIndex(rec["x_index"])
            self.set_x(rec["x_index"])
        if "fitfunc" in rec and getattr(self.curves_dock, "ctrls", None) is not None:
            idx = self.curves_dock.ctrls.cob_fit.findText(rec["fitfunc"])
            if idx >= 0:
                self.curves_dock.ctrls.cob_fit.setCurrentIndex(idx)

        if rtype == "export":
            self.export_plot()
        elif rtype == "2d":
            self.two_d_plot()
        elif rtype == "heatmap":
            self.generate_heatmap(recipe=rec)
        elif rtype == "batch":
            self.batch_fit_selected(recipe=rec)
        elif rtype in ("derived_merge", "derived_global"):
            logging.info("Skipping derived recipe restore (not portable without embedded data): %s", rtype)
        else:
            logging.warning("Unknown tab recipe type: %s", rtype)

        # Apply saved title if present
        title = rec.get("title")
        if title and self._focused_dock is not None and self._focused_dock is not self.curves_dock:
            self._focused_dock.setWindowTitle(title)
            self._focused_dock.state.title = title

    def copy_widget_to_clipboard(self, widget):
        """Copy a QWidget grab to the Windows clipboard as a DIB."""
        qimg = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        ptr = qimg.bits()
        ptr.setsize(qimg.sizeInBytes())
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(qimg.height(), qimg.width(), 4)
        img = Image.fromarray(arr[:, :, :3].copy(), "RGB")
        output = io.BytesIO()
        img.save(output, "BMP")
        data = output.getvalue()[14:]
        output.close()
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        win32clipboard.CloseClipboard()

class dummy:
    def __init__(self):
        self.data = 0

def main():
    import sys
    app = QApplication.instance() or QApplication(sys.argv[:1])

    window = ScanPlotApplet(dummy(), None)
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()