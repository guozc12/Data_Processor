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
    QButtonGroup, QLineEdit, QDockWidget, QToolBar, QScrollArea,
    QGroupBox,
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
import warnings
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

# Curves display-X modes (cob_curves_x userData kind) — legacy bridge
CURVES_X_AXIS = "axis"
CURVES_X_ROI = "roi"
CURVES_X_TIME = "time"

# Universal plotter encoding
DIM_MODE_ONE = "one"
DIM_MODE_MULTI = "multi"
DIM_MODE_ALL = "all"
DIM_MODE_MEAN = "mean"
DIM_MODES = (DIM_MODE_ONE, DIM_MODE_MULTI, DIM_MODE_ALL, DIM_MODE_MEAN)
Y_CHANNEL_MEASUREMENT = "measurement"


def dim_id_axis(i: int) -> str:
    return f"axis:{int(i)}"


def parse_dim_id(dim_id: str):
    """Return ('axis', i) | ('roi', None) | ('scan', None)."""
    if dim_id == "roi":
        return "roi", None
    if dim_id == "scan":
        return "scan", None
    if isinstance(dim_id, str) and dim_id.startswith("axis:"):
        return "axis", int(dim_id.split(":", 1)[1])
    return "axis", 0


def default_encoding(x_dim="axis:0", y_channel=Y_CHANNEL_MEASUREMENT):
    return {
        "plot_type": "xy",
        "x_dim": x_dim,
        "y_channel": y_channel,
        "dims": {},  # dim_id -> {"mode", "values"}
    }


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
    # Per-dock encoding (Curves / derived): dim filters + curve-axis reduce
    encoding_dim_state: dict = field(default_factory=dict)
    # Universal plotter encoding: {plot_type, x_dim, y_channel, dims}
    encoding: dict = field(default_factory=default_encoding)
    # Derived fit-param points: list of {x, y, yerr, label} when y_channel != measurement
    derived_points: list = field(default_factory=list)


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


class EncodingPanel(QWidget):
    """Data encoding controls: Type / X / Y / Dimensions (vertical sidebar)."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._skip = False
        self._y_channel = Y_CHANNEL_MEASUREMENT
        self._dim_catalog = []  # [{id, label, values, value_labels?}, ...]
        self._rows = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(8)

        hdr = QLabel("Encoding")
        hdr.setStyleSheet(f"font-weight: 600; color: {THEME['text']};")
        root.addWidget(hdr)

        form = QVBoxLayout()
        form.setSpacing(6)
        form.addWidget(QLabel("Type"))
        self.cob_type = QComboBox()
        self.cob_type.addItem("XY", "xy")
        self.cob_type.setToolTip("Plot type (v1: XY only)")
        form.addWidget(self.cob_type)

        form.addWidget(QLabel("X"))
        self.cob_x = QComboBox()
        self.cob_x.setToolTip("Horizontal axis dimension")
        form.addWidget(self.cob_x)

        form.addWidget(QLabel("Y"))
        self.lb_y = QLabel("measurement")
        self.lb_y.setWordWrap(True)
        self.lb_y.setStyleSheet(f"color: {THEME['muted']};")
        form.addWidget(self.lb_y)
        root.addLayout(form)

        dim_hdr = QLabel("Dimensions")
        dim_hdr.setStyleSheet(f"font-weight: 600; color: {THEME['text']};")
        dim_hdr.setToolTip("Other dimensions: One / Multi / All / Mean")
        root.addWidget(dim_hdr)

        self.dim_host = QWidget()
        self.dim_layout = QVBoxLayout(self.dim_host)
        self.dim_layout.setContentsMargins(0, 0, 0, 0)
        self.dim_layout.setSpacing(8)
        root.addWidget(self.dim_host)
        root.addStretch(1)

        self.cob_type.currentIndexChanged.connect(self._emit_changed)
        self.cob_x.currentIndexChanged.connect(self._on_x_changed)

    def set_y_label(self, text):
        self.lb_y.setText(text)

    def set_catalog(self, dim_catalog, encoding=None):
        """dim_catalog: [{id, label, values, value_labels?}, ...]."""
        self._dim_catalog = list(dim_catalog or [])
        enc = encoding or default_encoding(
            self._dim_catalog[0]["id"] if self._dim_catalog else "axis:0"
        )
        self._skip = True
        self.cob_x.clear()
        for d in self._dim_catalog:
            self.cob_x.addItem(d["label"], d["id"])
        x_dim = enc.get("x_dim") or (self._dim_catalog[0]["id"] if self._dim_catalog else "axis:0")
        xi = self.cob_x.findData(x_dim)
        if xi < 0:
            xi = 0
            x_dim = self.cob_x.itemData(0) if self.cob_x.count() else "axis:0"
        self.cob_x.setCurrentIndex(xi)
        pt = enc.get("plot_type") or "xy"
        ti = self.cob_type.findData(pt)
        if ti >= 0:
            self.cob_type.setCurrentIndex(ti)
        ych = enc.get("y_channel") or Y_CHANNEL_MEASUREMENT
        self._y_channel = ych
        if ych == Y_CHANNEL_MEASUREMENT:
            self.set_y_label("measurement")
        else:
            self.set_y_label(ych.replace("fit:", "fit ") if ych.startswith("fit:") else ych)
        self._rebuild_dim_rows(enc)
        self._skip = False

    def _clear_dim_rows(self):
        lay = self.dim_layout
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows = {}

    def _value_labels(self, d):
        vals = list(d.get("values") or [])
        labs = d.get("value_labels")
        if labs and len(labs) == len(vals):
            return list(labs)
        return [self._fmt(v) for v in vals]

    def _rebuild_dim_rows(self, encoding=None):
        encoding = encoding or self.read_encoding()
        # Prevent Multi setSelected / mode sync from emitting while rebuilding
        prev_skip = self._skip
        self._skip = True
        self._clear_dim_rows()
        x_dim = self.cob_x.currentData()
        dims_state = encoding.get("dims") or {}
        for d in self._dim_catalog:
            did = d["id"]
            if did == x_dim:
                continue
            st = dims_state.get(did) or {}
            mode = st.get("mode") or (
                DIM_MODE_MEAN if did.startswith("axis:") else DIM_MODE_ALL
            )
            if did.startswith("axis:") and parse_dim_id(x_dim)[0] == "axis" and not st:
                mode = DIM_MODE_ONE
            if did == "roi" and not st:
                mode = DIM_MODE_ONE
            if did == "scan" and not st:
                mode = DIM_MODE_ALL
            values = list(st.get("values") or [])
            labels = self._value_labels(d)

            box = QGroupBox(d["label"])
            col = QVBoxLayout(box)
            col.setContentsMargins(6, 8, 6, 6)
            col.setSpacing(4)

            cob_mode = QComboBox()
            for m, lab in (
                (DIM_MODE_ONE, "One"),
                (DIM_MODE_MULTI, "Multi"),
                (DIM_MODE_ALL, "All"),
                (DIM_MODE_MEAN, "Mean"),
            ):
                cob_mode.addItem(lab, m)
            mi = cob_mode.findData(mode)
            cob_mode.setCurrentIndex(mi if mi >= 0 else 0)
            col.addWidget(cob_mode)

            cob_one = QComboBox()
            for v, lab in zip(d["values"], labels):
                cob_one.addItem(lab, v)
            if values:
                i = cob_one.findData(values[0])
                if i >= 0:
                    cob_one.setCurrentIndex(i)
            elif cob_one.count():
                cob_one.setCurrentIndex(0)
            col.addWidget(cob_one)

            list_multi = QListWidget()
            list_multi.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            list_multi.setMinimumHeight(88)
            list_multi.setMaximumHeight(140)
            for v, lab in zip(d["values"], labels):
                item = QListWidgetItem(lab)
                item.setData(Qt.ItemDataRole.UserRole, v)
                list_multi.addItem(item)
            list_multi.blockSignals(True)
            if values:
                want = set(values)
                for i in range(list_multi.count()):
                    it = list_multi.item(i)
                    if it.data(Qt.ItemDataRole.UserRole) in want:
                        it.setSelected(True)
            else:
                for i in range(list_multi.count()):
                    list_multi.item(i).setSelected(True)
            list_multi.blockSignals(False)
            col.addWidget(list_multi)

            ns = SimpleNamespace(
                mode=cob_mode, one=cob_one, multi=list_multi, dim_id=did, values=d["values"]
            )
            self._rows[did] = ns

            def _sync_vis(_=0, n=ns):
                m = n.mode.currentData()
                n.one.setVisible(m == DIM_MODE_ONE)
                n.multi.setVisible(m == DIM_MODE_MULTI)
                # Mode toggle should redraw; avoid emit during panel._skip rebuild
                self._emit_changed()

            cob_mode.currentIndexChanged.connect(_sync_vis)
            cob_one.currentIndexChanged.connect(self._emit_changed)
            list_multi.itemSelectionChanged.connect(self._emit_changed)
            # Visibility only (panel._skip already True — no emit)
            m0 = cob_mode.currentData()
            cob_one.setVisible(m0 == DIM_MODE_ONE)
            list_multi.setVisible(m0 == DIM_MODE_MULTI)
            self.dim_layout.addWidget(box)
        self._skip = prev_skip

    @staticmethod
    def _fmt(v):
        if isinstance(v, float):
            return f"{v:g}"
        return str(v)

    def _on_x_changed(self, _=0):
        if self._skip:
            return
        enc = self.read_encoding()
        self._rebuild_dim_rows(enc)
        self._emit_changed()

    def _emit_changed(self, *_):
        if self._skip:
            return
        self.changed.emit()

    def read_encoding(self):
        x_dim = self.cob_x.currentData() or "axis:0"
        dims = {}
        for did, ns in self._rows.items():
            mode = ns.mode.currentData() or DIM_MODE_ONE
            if mode == DIM_MODE_ONE:
                vals = [ns.one.currentData()] if ns.one.count() else []
            elif mode == DIM_MODE_MULTI:
                vals = [
                    ns.multi.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(ns.multi.count())
                    if ns.multi.item(i).isSelected()
                ]
            else:
                vals = []
            dims[did] = {"mode": mode, "values": vals}
        return {
            "plot_type": self.cob_type.currentData() or "xy",
            "x_dim": x_dim,
            "y_channel": getattr(self, "_y_channel", None) or Y_CHANNEL_MEASUREMENT,
            "dims": dims,
        }

    def write_y_channel(self, y_channel):
        self._y_channel = y_channel or Y_CHANNEL_MEASUREMENT
        if y_channel == Y_CHANNEL_MEASUREMENT:
            self.set_y_label("measurement")
        elif isinstance(y_channel, str) and y_channel.startswith("fit:"):
            self.set_y_label("fit " + y_channel[4:])
        else:
            self.set_y_label(str(y_channel))


class StylePanel(QWidget):
    """Legend / error bars / line style."""

    legend_toggled = pyqtSignal(bool)
    errorbars_toggled = pyqtSignal(bool)
    style_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        hdr = QLabel("Style")
        hdr.setStyleSheet(f"font-weight: 600; color: {THEME['text']};")
        root.addWidget(hdr)
        self.chk_legend = QCheckBox("Legend")
        self.chk_errorbars = QCheckBox("Error bars")
        self.cob_style = QComboBox()
        self.cob_style.addItems(STYLE_MODES)
        root.addWidget(self.chk_legend)
        root.addWidget(self.chk_errorbars)
        root.addWidget(QLabel("Mode"))
        root.addWidget(self.cob_style)
        self.chk_legend.toggled.connect(self.legend_toggled.emit)
        self.chk_errorbars.toggled.connect(self.errorbars_toggled.emit)
        self.cob_style.currentTextChanged.connect(self.style_changed.emit)

    def sync_from_state(self, state):
        self.chk_legend.blockSignals(True)
        self.chk_errorbars.blockSignals(True)
        self.cob_style.blockSignals(True)
        self.chk_legend.setChecked(bool(state.show_legend))
        self.chk_errorbars.setChecked(bool(state.show_errorbars))
        self.cob_style.setCurrentText(state.style.get("mode", "Scatter"))
        self.chk_legend.blockSignals(False)
        self.chk_errorbars.blockSignals(False)
        self.cob_style.blockSignals(False)


class AnalyzePanel(QWidget):
    """Fit / Derive / legacy analyze actions (vertical)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        hdr = QLabel("Analyze")
        hdr.setStyleSheet(f"font-weight: 600; color: {THEME['text']};")
        root.addWidget(hdr)
        self.cob_fit = QComboBox()
        root.addWidget(QLabel("Fit function"))
        root.addWidget(self.cob_fit)
        row = QHBoxLayout()
        self.bt_fit = QPushButton("Fit")
        self.bt_clear = QPushButton("Clear")
        row.addWidget(self.bt_fit)
        row.addWidget(self.bt_clear)
        root.addLayout(row)
        self.bt_derive = QPushButton("Derive…")
        root.addWidget(self.bt_derive)
        root.addWidget(self._hline())
        self.bt_2d = QPushButton("2D")
        self.bt_batch = QPushButton("Batch fit")
        self.bt_roi_grid = QPushButton("ROI grid")
        self.bt_export = QPushButton("Export")
        root.addWidget(self.bt_2d)
        root.addWidget(self.bt_batch)
        root.addWidget(self.bt_roi_grid)
        root.addWidget(self.bt_export)
        root.addWidget(self._hline())
        self.bt_copy = QToolButton()
        self.bt_copy.setText("Copy")
        self.bt_copy.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.bt_copy.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root.addWidget(self.bt_copy)
        root.addStretch(1)

    @staticmethod
    def _hline():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line


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
    """Batch fit: reduce along Fit-along, plot fit params vs an outer dimension."""

    OUTER_TIME = "time"
    OUTER_ROI = "roi"
    OUTER_AXIS = "axis"
    # Legacy recipe mode strings still accepted by batch_fit_selected
    MODE_TIME = "vs_time"
    MODE_AXIS = "vs_axis"
    MODE_ROI = "vs_ROI"

    def __init__(self, param_names, scan_labels, outer_choices, parent=None):
        """
        outer_choices: list of (label, outer_kind, axis_index|None)
          e.g. [("scan time", "time", None), ("ROI", "roi", None), ("detuning_y", "axis", 1)]
        """
        super().__init__(parent)
        self.setWindowTitle("Batch fit")
        self.setModal(True)
        self.resize(400, 440)
        self._outer_choices = list(outer_choices or [])

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Plot fit parameter vs (outer dimension):"))
        self.cob_outer = QComboBox()
        for label, _kind, _axis in self._outer_choices:
            self.cob_outer.addItem(label)
        if not self._outer_choices:
            self.cob_outer.addItem("(none)")
            self.cob_outer.setEnabled(False)
        layout.addWidget(self.cob_outer)

        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Target scan (axis / ROI outer):"))
        self.cob_scan = QComboBox()
        self.cob_scan.addItems(scan_labels or ["(none)"])
        self.cob_scan.setEnabled(bool(scan_labels))
        scan_row.addWidget(self.cob_scan, stretch=1)
        layout.addLayout(scan_row)

        def _sync_scan_enabled():
            kind = self.selected_outer()[0]
            self.cob_scan.setEnabled(
                kind in (self.OUTER_AXIS, self.OUTER_ROI) and bool(scan_labels)
            )
        self.cob_outer.currentIndexChanged.connect(lambda _=0: _sync_scan_enabled())
        _sync_scan_enabled()

        layout.addWidget(QLabel("Parameters to plot:"))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for name in param_names:
            item = QListWidgetItem(name)
            self.list_widget.addItem(item)
            # Default: none selected (user picks params explicitly)
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

    def selected_outer(self):
        """Return (outer_kind, axis_index|None)."""
        i = self.cob_outer.currentIndex()
        if i < 0 or i >= len(self._outer_choices):
            return self.OUTER_TIME, None
        _label, kind, axis = self._outer_choices[i]
        return kind, axis

    def selected_mode(self):
        """Map outer kind → legacy mode string for recipes."""
        kind, _ = self.selected_outer()
        if kind == self.OUTER_AXIS:
            return self.MODE_AXIS
        if kind == self.OUTER_ROI:
            return self.MODE_ROI
        return self.MODE_TIME

    def selected_scan_index(self):
        """Index into the scan_labels list passed to __init__ (0-based)."""
        return max(0, self.cob_scan.currentIndex())


class TwoDPlotDialog(QDialog):
    """Pick scan, ROI, and two scan axes for a measurement-value 2D image."""

    def __init__(self, scan_labels, axis_labels, n_rois, parent=None):
        super().__init__(parent)
        self.setWindowTitle("2D plot (scan axes)")
        self.setModal(True)
        self.resize(360, 220)

        layout = QVBoxLayout(self)

        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Scan:"))
        self.cob_scan = QComboBox()
        self.cob_scan.addItems(scan_labels or ["(none)"])
        self.cob_scan.setEnabled(bool(scan_labels))
        scan_row.addWidget(self.cob_scan, stretch=1)
        layout.addLayout(scan_row)

        roi_row = QHBoxLayout()
        roi_row.addWidget(QLabel("ROI:"))
        self.cob_roi = QComboBox()
        self.cob_roi.addItems([str(i) for i in range(max(1, n_rois))])
        roi_row.addWidget(self.cob_roi, stretch=1)
        layout.addLayout(roi_row)

        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axis X:"))
        self.cob_axis_x = QComboBox()
        self.cob_axis_x.addItems(axis_labels or ["(none)"])
        ax_row.addWidget(self.cob_axis_x, stretch=1)
        layout.addLayout(ax_row)

        ay_row = QHBoxLayout()
        ay_row.addWidget(QLabel("Axis Y:"))
        self.cob_axis_y = QComboBox()
        self.cob_axis_y.addItems(axis_labels or ["(none)"])
        if len(axis_labels or []) > 1:
            self.cob_axis_y.setCurrentIndex(1)
        ay_row.addWidget(self.cob_axis_y, stretch=1)
        layout.addLayout(ay_row)

        tip = QLabel("Color = measured value (same binning as Curves). "
                     "Axes beyond X/Y stay fixed at the current slice.")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {THEME['muted']};")
        layout.addWidget(tip)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        return (
            max(0, self.cob_scan.currentIndex()),
            max(0, self.cob_roi.currentIndex()),
            max(0, self.cob_axis_x.currentIndex()),
            max(0, self.cob_axis_y.currentIndex()),
        )


from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtGui import QIntValidator


class HeatmapDialog(QDialog):
    def __init__(self, fit_options, n_rois, scan_labels=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROI grid (fit parameter)")
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
from scipy.optimize import OptimizeWarning, curve_fit
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
        
        # execute fit (OptimizeWarning is common on short/noisy series — not a hard failure)
        if len(p0) == 0:
            popt, perr = [], []
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=OptimizeWarning)
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
        if getattr(self, "cob_curves_x", None) is not None:
            self.cob_curves_x.currentIndexChanged.connect(self._on_curves_x_changed)
        self.scrollbar.valueChanged.connect(self.change_plot)
        self.bt_import.clicked.connect(self.import_data)

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
        """Dockable Curves / analysis / browser panels (no global Analyze toolbar)."""
        while self.gridLayout.count():
            self.gridLayout.takeAt(0)

        self.sb_roi.hide()
        for w in (self.lb_x, self.lb_roi, self.lb_fitfunc, self.cob_action):
            w.hide()
        # Ui "Global" button — hide; Browser gets a new Global action below
        try:
            self.bt_global.hide()
        except RuntimeError:
            pass

        # Hidden ROI list kept for legacy recipe restore; selection comes from EncodingPanel
        self.list_roi = QListWidget()
        self.list_roi.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_roi.hide()

        # Hidden scan list kept for recipe restore / internal selection; UI uses EncodingPanel
        self.list_scans = QListWidget()
        self.list_scans.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_scans.hide()
        self.list_scans.itemSelectionChanged.connect(self._on_scan_selection_changed)

        self.bt_import.setText("Import")
        self.bt_import.setToolTip("Load scan folders from data/")
        # View/Style/Fit live on each plot dock — hide unused legacy Ui chrome
        for w in (self.bt_fit, self.bt_clear, self.bt_export, self.cob_fit):
            w.hide()
        # Remove any prior main toolbar (Import/Analyze moved to Browser / plot docks)
        for tb in self.findChildren(QToolBar):
            self.removeToolBar(tb)
            tb.deleteLater()

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

        # Curves: left sidebar (Encoding / Style / Analyze) + plot
        curves_page = QWidget()
        curves_outer = QHBoxLayout(curves_page)
        curves_outer.setContentsMargins(0, 0, 0, 0)
        curves_outer.setSpacing(0)
        self.curves_dock = PlotDock("Curves", "curves", curves_page, parent=self, closable=False)
        self.curves_dock.activated.connect(self.set_focused_dock)
        self.curves_dock.state.encoding = default_encoding()

        self.curves_encoding_panel = EncodingPanel()
        self.curves_encoding_panel.changed.connect(self._on_curves_encoding_changed)
        self.curves_style_panel = StylePanel()
        self.curves_analyze_panel = AnalyzePanel()
        self._wire_curves_side_panels()
        self.curves_dock.encoding_panel = self.curves_encoding_panel
        self.curves_dock.style_panel = self.curves_style_panel
        self.curves_dock.analyze_panel = self.curves_analyze_panel

        side = QWidget()
        side.setMinimumWidth(220)
        side.setMaximumWidth(320)
        side_col = QVBoxLayout(side)
        side_col.setContentsMargins(0, 0, 0, 0)
        side_col.setSpacing(0)
        side_col.addWidget(self.curves_encoding_panel)
        side_col.addWidget(self._side_sep())
        side_col.addWidget(self.curves_style_panel)
        side_col.addWidget(self._side_sep())
        side_col.addWidget(self.curves_analyze_panel, stretch=1)
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setWidget(side)
        side_scroll.setMinimumWidth(230)
        side_scroll.setMaximumWidth(340)

        plot_col_w = QWidget()
        plot_col = QVBoxLayout(plot_col_w)
        plot_col.setContentsMargins(4, 4, 4, 4)
        self.curves_dim_state = {}
        self.curves_dim_bar = QWidget()
        self.curves_dim_layout = QHBoxLayout(self.curves_dim_bar)
        self.curves_dim_bar.hide()
        self.scrollbar.hide()
        plot_col.addWidget(self.scrollbar)
        self.pw_plot.setSizePolicy(
            QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        )
        plot_col.addWidget(self.pw_plot, stretch=1)
        self.lb_fit.setWordWrap(True)
        self.lb_fit.setMinimumHeight(28)
        plot_col.addWidget(self.lb_fit)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(side_scroll)
        split.addWidget(plot_col_w)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 900])
        curves_outer.addWidget(split)
        self.curves_dock.plot_widget = self.pw_plot

        self.cob_x = QComboBox(self)
        self.cob_x.hide()
        self.cob_x.setObjectName("cob_x_legacy")
        self.cob_curves_x = QComboBox(self)
        self.cob_curves_x.hide()

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.curves_dock)
        self.plot_docks.append(self.curves_dock)
        self.curves_dock.installEventFilter(self)
        self.curves_dock.hide()

        # Right browser dock (no Loaded scans — scan filter is in EncodingPanel)
        right = QSplitter(Qt.Orientation.Vertical)
        data_box = QWidget()
        data_layout = QVBoxLayout(data_box)
        data_layout.setContentsMargins(4, 4, 4, 4)
        data_layout.addWidget(QLabel("Data"))
        data_btn_row = QHBoxLayout()
        data_btn_row.addWidget(self.bt_import)
        self.bt_refresh_all = QPushButton("Refresh all")
        self.bt_refresh_all.setToolTip(
            "Recompute all analysis docks from their saved recipes."
        )
        self.bt_refresh_all.clicked.connect(self.refresh_all_analysis)
        data_btn_row.addWidget(self.bt_refresh_all)
        data_btn_row.addStretch(1)
        data_layout.addLayout(data_btn_row)
        cube_row = QHBoxLayout()
        self.bt_merge = QPushButton("Merge")
        self.bt_global_cube = QPushButton("Global")
        self.bt_clear_scans = QPushButton("Clear…")
        self.bt_merge.setToolTip("Merge all loaded scans → new dock")
        self.bt_global_cube.setToolTip("Sum ROIs of primary scan → new dock")
        self.bt_clear_scans.setToolTip("Clear all loaded scans and analysis docks")
        self.bt_merge.clicked.connect(self.merge_selected_scans)
        self.bt_global_cube.clicked.connect(self.globalify)
        self.bt_clear_scans.clicked.connect(self._confirm_clear_scans)
        cube_row.addWidget(self.bt_merge)
        cube_row.addWidget(self.bt_global_cube)
        cube_row.addWidget(self.bt_clear_scans)
        data_layout.addLayout(cube_row)

        exp_box = QWidget()
        exp_layout = QVBoxLayout(exp_box)
        exp_layout.setContentsMargins(4, 4, 4, 4)
        exp_layout.addWidget(QLabel("Experiments"))
        self.list_experiments = QListWidget()
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

        right.addWidget(data_box)
        right.addWidget(exp_box)
        right.setStretchFactor(0, 0)
        right.setStretchFactor(1, 1)

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

    def _side_sep(self):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _wire_curves_side_panels(self):
        """Connect StylePanel + AnalyzePanel on Curves dock; expose dock.ctrls aliases."""
        dock = self.curves_dock
        style = self.curves_style_panel
        ana = self.curves_analyze_panel
        style.sync_from_state(dock.state)
        style.legend_toggled.connect(lambda c, d=dock: self._dock_set_legend(d, c))
        style.errorbars_toggled.connect(lambda c, d=dock: self._dock_set_errorbars(d, c))
        style.style_changed.connect(lambda m, d=dock: self._dock_set_style(d, m))

        ana.cob_fit.clear()
        ana.cob_fit.addItems(list(self.fitter.func_dict.keys()))
        dock.state.fitfunc = dock.state.fitfunc or ana.cob_fit.currentText()
        i = ana.cob_fit.findText(dock.state.fitfunc or "")
        if i >= 0:
            ana.cob_fit.setCurrentIndex(i)
        ana.cob_fit.currentTextChanged.connect(lambda f, d=dock: self._dock_set_fitfunc(d, f))
        ana.bt_fit.clicked.connect(lambda _=False, d=dock: self._dock_fit(d))
        ana.bt_clear.clicked.connect(lambda _=False, d=dock: self._dock_clear_fit(d))
        ana.bt_fit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        ana.bt_fit.customContextMenuRequested.connect(
            lambda _pos, d=dock: self._dock_advanced_fit(d)
        )
        ana.bt_derive.clicked.connect(lambda _=False, d=dock: self.derive_fit_param_plot(dock=d))
        ana.bt_2d.setToolTip("Measurement vs two scan axes.")
        ana.bt_batch.setToolTip("Legacy batch fit (fit params vs outer dim).")
        ana.bt_roi_grid.setToolTip("Legacy ROI grid of one fit parameter.")
        ana.bt_export.setToolTip("Snapshot current curve → Export dock + TSV")
        ana.bt_2d.clicked.connect(lambda _=False, d=dock: self.two_d_plot(dock=d))
        ana.bt_batch.clicked.connect(lambda _=False, d=dock: self.batch_fit_selected(dock=d))
        ana.bt_roi_grid.clicked.connect(lambda _=False, d=dock: self.generate_heatmap(dock=d))
        ana.bt_export.clicked.connect(self.export_plot)
        copy_menu = QMenu(ana.bt_copy)
        act_img = QAction("Copy image", self)
        act_img.triggered.connect(lambda _=False, d=dock: self._dock_copy_image(d))
        act_raw = QAction("Copy raw data", self)
        act_raw.triggered.connect(lambda _=False, d=dock: self._dock_copy_raw(d))
        copy_menu.addAction(act_img)
        copy_menu.addAction(act_raw)
        ana.bt_copy.setMenu(copy_menu)

        # Aliases so existing helpers (_curves_fitfunc, analyze enable) keep working
        dock.ctrls = SimpleNamespace(
            chk_legend=style.chk_legend,
            chk_errorbars=style.chk_errorbars,
            cob_style=style.cob_style,
            cob_fit=ana.cob_fit,
            bt_fit=ana.bt_fit,
            bt_clear=ana.bt_clear,
            bt_2d=ana.bt_2d,
            bt_batch=ana.bt_batch,
            bt_roi_grid=ana.bt_roi_grid,
            bt_export=ana.bt_export,
            bt_derive=ana.bt_derive,
            bt_copy=ana.bt_copy,
        )

    def _relayout_main_docks(self):
        """Give Curves (left) most width; keep Browser a narrow right column."""
        if not self.curves_dock.isVisible():
            return
        total = max(self.width(), 900)
        browser_w = 240 if self.browser_dock.isVisible() else 0
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
        with_derive=False,
        with_copy=True,
    ):
        """Per-dock View/Style/Fit strip — travels with the floating/docked window."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)
        ctrls = SimpleNamespace()

        # Legacy with_x path removed from Curves; EncodingPanel owns X/dims.
        # Keep flag for any external callers — no-op UI.
        _ = with_x

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
            ctrls.bt_2d = QPushButton("2D")
            ctrls.bt_batch = QPushButton("Batch fit")
            ctrls.bt_roi_grid = QPushButton("ROI grid")
            ctrls.bt_export = QPushButton("Export")
            ctrls.bt_2d.setToolTip(
                "Measurement value vs two scan axes (pick axes in dialog)."
            )
            ctrls.bt_batch.setToolTip(
                "Fit along Curve axis; plot fit params vs scan time / scan axis / ROI."
            )
            ctrls.bt_roi_grid.setToolTip(
                "Fit all ROIs; show one fit parameter as a rows×cols grid."
            )
            ctrls.bt_export.setToolTip("Open current curve in a new dock + copy TSV")
            ctrls.bt_2d.clicked.connect(lambda _=False, d=dock: self.two_d_plot(dock=d))
            ctrls.bt_batch.clicked.connect(
                lambda _=False, d=dock: self.batch_fit_selected(dock=d)
            )
            ctrls.bt_roi_grid.clicked.connect(
                lambda _=False, d=dock: self.generate_heatmap(dock=d)
            )
            ctrls.bt_export.clicked.connect(self.export_plot)
            row.addWidget(ctrls.bt_2d)
            row.addWidget(ctrls.bt_batch)
            row.addWidget(ctrls.bt_roi_grid)
            row.addWidget(ctrls.bt_export)

        if with_derive:
            ctrls.bt_derive = QPushButton("Derive")
            ctrls.bt_derive.setToolTip(
                "Fit along current X for each series; open one new plot of a single fit parameter."
            )
            ctrls.bt_derive.clicked.connect(lambda _=False, d=dock: self.derive_fit_param_plot(dock=d))
            row.addWidget(ctrls.bt_derive)

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
        # Same X / Curve axis / Dimensions encoding as Curves (for live scan plots)
        if kind in ("derived_merge", "derived_global"):
            enc = self._build_dock_encoding_panel(dock)
            layout.addWidget(enc)
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
        """Loaded-scans multi-select (cube membership / Browser). Plot scan filter uses encoding."""
        rows = sorted({item.row() for item in self.list_scans.selectedIndexes()})
        if rows:
            return rows
        return list(range(len(self.scans))) if self.scans else []

    def selected_roi_indices(self):
        """ROI from Curves encoding (Browser ROI list removed).

        Prefer Curves encoding when the focused dock has no dims (export/batch
        snapshots) — otherwise ROI silently collapses to [0].
        """
        dock = self.focused_plot_dock() or self.curves_dock
        enc = getattr(getattr(dock, "state", None), "encoding", None)
        if not enc or not (enc.get("dims") or {}):
            enc = getattr(self.curves_dock.state, "encoding", None)
        if enc:
            rois = self._encoding_roi_indices(enc)
            if rois:
                return rois
        rows = sorted({item.row() for item in self.list_roi.selectedIndexes()})
        return rows if rows else [0]

    def primary_scan(self):
        idxs = self.selected_scan_indices()
        if not idxs and self.scans:
            return self.scans[0]
        if not idxs:
            return None
        return self.scans[idxs[0]]

    def _cube_dim_catalog(self, scans=None):
        """Dims available for encoding: native axes + ROI + scan."""
        scans = scans if scans is not None else self.scans
        catalog = []
        if not scans:
            return catalog
        primary = scans[0]
        key_sets = primary.metadata.get("key_sets") or []
        if key_sets:
            for i, key in enumerate(key_sets):
                vals = self._unique_axis_values(primary, i)
                catalog.append({"id": dim_id_axis(i), "label": key[-1], "values": vals})
        else:
            catalog.append({"id": dim_id_axis(0), "label": "repetition", "values": [0.0]})
        n_rois = 1
        if primary.data:
            first = primary.data[0]
            if isinstance(first, (list, tuple, np.ndarray)):
                n_rois = max(1, len(first))
        catalog.append({
            "id": "roi",
            "label": "ROI",
            "values": list(range(n_rois)),
            "value_labels": [f"ROI {i}" for i in range(n_rois)],
        })
        catalog.append({
            "id": "scan",
            "label": "scan",
            "values": list(range(len(scans))),
            "value_labels": [self._scan_short_name(s) for s in scans],
        })
        return catalog

    def _encoding_roi_indices(self, enc):
        st = (enc.get("dims") or {}).get("roi") or {}
        mode = st.get("mode") or DIM_MODE_ONE
        catalog = {d["id"]: d for d in self._cube_dim_catalog()}
        all_rois = list((catalog.get("roi") or {}).get("values") or [0])
        if mode in (DIM_MODE_ALL, DIM_MODE_MEAN):
            return all_rois
        vals = st.get("values") or []
        if mode == DIM_MODE_ONE:
            if vals:
                return [int(vals[0])]
            return [int(all_rois[0])] if all_rois else [0]
        # multi
        out = [int(v) for v in vals if int(v) in all_rois]
        return out if out else all_rois[:1] or [0]

    def _encoding_scan_indices(self, enc):
        st = (enc.get("dims") or {}).get("scan") or {}
        mode = st.get("mode") or DIM_MODE_ALL
        n = len(self.scans)
        all_scans = list(range(n))
        if mode in (DIM_MODE_ALL, DIM_MODE_MEAN):
            return all_scans
        vals = st.get("values") or []
        if mode == DIM_MODE_ONE:
            if vals:
                i = int(vals[0])
                return [i] if 0 <= i < n else (all_scans[:1] if all_scans else [])
            return all_scans[:1] if all_scans else []
        out = [int(v) for v in vals if 0 <= int(v) < n]
        return out if out else (all_scans[:1] if all_scans else [])

    def _populate_curves_encoding(self):
        panel = getattr(self, "curves_encoding_panel", None)
        if panel is None:
            return
        catalog = self._cube_dim_catalog()
        enc = self.curves_dock.state.encoding or default_encoding()
        if catalog and enc.get("x_dim") not in {d["id"] for d in catalog}:
            enc["x_dim"] = catalog[0]["id"]
        # Seed defaults for missing dims
        dims = enc.setdefault("dims", {})
        x_dim = enc.get("x_dim")
        for d in catalog:
            if d["id"] == x_dim:
                continue
            if d["id"] not in dims:
                if d["id"] == "roi":
                    dims[d["id"]] = {"mode": DIM_MODE_ONE, "values": [d["values"][0]] if d["values"] else [0]}
                elif d["id"] == "scan":
                    dims[d["id"]] = {"mode": DIM_MODE_ALL, "values": []}
                elif parse_dim_id(x_dim)[0] in ("roi", "scan") and d["id"].startswith("axis:"):
                    dims[d["id"]] = {"mode": DIM_MODE_MEAN, "values": []}
                else:
                    dims[d["id"]] = {
                        "mode": DIM_MODE_ONE,
                        "values": [d["values"][0]] if d["values"] else [],
                    }
        self.curves_dock.state.encoding = enc
        panel.set_catalog(catalog, enc)
        panel.write_y_channel(enc.get("y_channel") or Y_CHANNEL_MEASUREMENT)

    def _on_curves_encoding_changed(self):
        # Defer apply: Multi list selection may still be mid-update when
        # itemSelectionChanged fires; also avoid dropping events while skip=True.
        QTimer.singleShot(0, lambda: self._apply_curves_encoding_from_panel(0))

    def _apply_curves_encoding_from_panel(self, retry=0):
        if self.skip:
            if retry < 8:
                QTimer.singleShot(
                    0, lambda r=retry + 1: self._apply_curves_encoding_from_panel(r)
                )
            return
        panel = getattr(self, "curves_encoding_panel", None)
        if panel is None:
            return
        enc = panel.read_encoding()
        enc["y_channel"] = Y_CHANNEL_MEASUREMENT
        self.curves_dock.state.encoding = enc
        self._sync_legacy_from_encoding(enc)
        self.draw_plot()
        self.set_title()

    def _sync_legacy_from_encoding(self, enc):
        """Bridge universal encoding → cob_x / cob_curves_x / curves_dim_state / list_roi."""
        if not enc:
            return
        x_dim = enc.get("x_dim") or "axis:0"
        kind, axis_i = parse_dim_id(x_dim)
        dims = enc.get("dims") or {}
        self.skip = True
        # Populate hidden cob_curves_x for legacy _curves_x_selection
        cob = getattr(self, "cob_curves_x", None)
        if cob is not None:
            cob.clear()
            for d in self._cube_dim_catalog():
                dk, ai = parse_dim_id(d["id"])
                if dk == "axis":
                    cob.addItem(d["label"], (CURVES_X_AXIS, ai))
                elif dk == "roi":
                    cob.addItem(d["label"], (CURVES_X_ROI, None))
                elif dk == "scan":
                    cob.addItem(d["label"], (CURVES_X_TIME, None))
            target = (
                (CURVES_X_AXIS, axis_i) if kind == "axis"
                else ((CURVES_X_ROI, None) if kind == "roi" else (CURVES_X_TIME, None))
            )
            for i in range(cob.count()):
                if cob.itemData(i) == target:
                    cob.setCurrentIndex(i)
                    break
        # Fit-along / Plots x_index: X axis if axis mode; else first Mean axis or 0
        fit_i = axis_i if kind == "axis" else 0
        if kind != "axis":
            for did, st in dims.items():
                dk, ai = parse_dim_id(did)
                if dk == "axis" and (st.get("mode") or "") == DIM_MODE_MEAN:
                    fit_i = ai
                    break
            else:
                # any axis dim
                for did in dims:
                    dk, ai = parse_dim_id(did)
                    if dk == "axis":
                        fit_i = ai
                        break
        if self.cob_x.count() == 0 and self.scans:
            primary = self.primary_scan()
            keys = (primary.metadata.get("key_sets") if primary else None) or []
            if keys:
                self.cob_x.addItems([k[-1] for k in keys])
            else:
                self.cob_x.addItems(["repetition"])
        need_rebuild = False
        if 0 <= fit_i < self.cob_x.count():
            if self.cob_x.currentIndex() != fit_i:
                self.cob_x.setCurrentIndex(fit_i)
                need_rebuild = True
            elif self.scans and (
                self.scans[0].plots is None or self.scans[0].plots.x_index != fit_i
            ):
                need_rebuild = True

        # curves_dim_state bridge
        self.curves_dim_state = {}
        for did, st in dims.items():
            dk, ai = parse_dim_id(did)
            mode = st.get("mode") or DIM_MODE_ONE
            vals = st.get("values") or []
            if dk == "axis":
                if mode == DIM_MODE_MEAN:
                    self.curves_dim_state[f"fit:{ai}"] = {"mode": "mean", "value": None}
                    self.curves_dim_state[f"axis:{ai}"] = {"mode": "all", "value": None}
                elif mode == DIM_MODE_ALL:
                    self.curves_dim_state[f"axis:{ai}"] = {"mode": "all", "value": None}
                    if kind != "axis" and ai == fit_i:
                        # Curve-axis All when X is ROI/scan: expand per unique value
                        self.curves_dim_state[f"fit:{ai}"] = {"mode": "all", "value": None}
                elif mode == DIM_MODE_MULTI:
                    self.curves_dim_state[f"axis:{ai}"] = {"mode": "all", "value": None}
                    if kind != "axis" and ai == fit_i:
                        self.curves_dim_state[f"fit:{ai}"] = {
                            "mode": "multi",
                            "value": None,
                            "values": list(vals),
                        }
                else:
                    self.curves_dim_state[f"axis:{ai}"] = {
                        "mode": "one",
                        "value": vals[0] if vals else None,
                    }
                if kind != "axis" and mode == DIM_MODE_ONE and ai == fit_i:
                    self.curves_dim_state[f"fit:{ai}"] = {
                        "mode": "one",
                        "value": vals[0] if vals else None,
                    }
                if kind != "axis" and mode == DIM_MODE_MEAN and ai == fit_i:
                    self.curves_dim_state[f"fit:{ai}"] = {"mode": "mean", "value": None}

        # Sync hidden list_roi for recipe restore
        rois = self._encoding_roi_indices(enc)
        self.list_roi.blockSignals(True)
        for i in range(self.list_roi.count()):
            it = self.list_roi.item(i)
            if it is not None:
                it.setSelected(i in rois)
        self.list_roi.blockSignals(False)
        self.skip = False
        if need_rebuild:
            self.set_x(fit_i)

    def _on_dock_encoding_changed_universal(self, dock):
        panel = getattr(dock, "encoding_panel", None)
        if panel is None or self.skip:
            return
        enc = panel.read_encoding()
        prev = dock.state.encoding or {}
        enc["y_channel"] = prev.get("y_channel") or Y_CHANNEL_MEASUREMENT
        dock.state.encoding = enc
        self._redraw_universal_dock(dock)

    def _fit_along_index(self):
        return self.cob_x.currentIndex() if self.cob_x.count() else 0

    def _curves_x_selection(self):
        """Return (kind, axis_index|None) for Curves display X."""
        cob = getattr(self, "cob_curves_x", None)
        if cob is None or cob.count() == 0:
            return CURVES_X_AXIS, self._fit_along_index()
        data = cob.currentData()
        if isinstance(data, (tuple, list)) and len(data) == 2:
            return data[0], data[1]
        # Fallback: treat as axis by index
        return CURVES_X_AXIS, max(0, cob.currentIndex())

    def _sync_curve_axis_visibility(self):
        """Curve axis + Reduce only when X is ROI / scan time."""
        kind, _ = self._curves_x_selection()
        show = kind in (CURVES_X_ROI, CURVES_X_TIME)
        for w in (
            getattr(self, "lb_curve_axis", None),
            getattr(self, "lb_fit_along", None),
            self.cob_x,
            getattr(self, "lb_curve_reduce", None),
            getattr(self, "cob_curve_reduce", None),
            getattr(self, "cob_curve_reduce_val", None),
        ):
            if w is None:
                continue
            w.setVisible(show)
        if show:
            self._sync_curve_reduce_widgets()
        # When X is a scan axis, Curve axis == X (hidden)
        if kind == CURVES_X_AXIS:
            _, axis_i = self._curves_x_selection()
            if axis_i is not None and 0 <= axis_i < self.cob_x.count() and self.cob_x.currentIndex() != axis_i:
                self.skip = True
                self.cob_x.setCurrentIndex(axis_i)
                self.skip = False

    def _sync_fit_along_visibility(self):
        """Backward-compatible alias."""
        self._sync_curve_axis_visibility()

    def _sync_curve_reduce_widgets(self):
        """Fill Reduce value combo from Curve axis unique values; apply dim_state."""
        cob_m = getattr(self, "cob_curve_reduce", None)
        cob_v = getattr(self, "cob_curve_reduce_val", None)
        if cob_m is None or cob_v is None:
            return
        scan = self.primary_scan()
        i = self._fit_along_index()
        dkey = f"fit:{i}"
        st = self._dim_state(dkey, default_mode="mean")
        self.skip = True
        mode = st.get("mode") or "mean"
        idx = cob_m.findData(mode)
        if idx < 0:
            idx = 0
            mode = "mean"
            st["mode"] = mode
        cob_m.setCurrentIndex(idx)
        vals = self._unique_axis_values(scan, i) if scan is not None else []
        cob_v.clear()
        for v in vals:
            cob_v.addItem(f"{v:g}", v)
        if st.get("value") is not None and vals:
            nearest = min(vals, key=lambda v: abs(v - float(st["value"])))
            cob_v.setCurrentIndex(vals.index(nearest))
            st["value"] = nearest
        elif vals:
            st["value"] = vals[0]
        cob_v.setEnabled(mode == "one")
        self.skip = False

    def _on_curve_reduce_changed(self, _index=0):
        if self.skip:
            return
        cob_m = self.cob_curve_reduce
        cob_v = self.cob_curve_reduce_val
        i = self._fit_along_index()
        st = self._dim_state(f"fit:{i}", default_mode="mean")
        st["mode"] = cob_m.currentData() or "mean"
        cob_v.setEnabled(st["mode"] == "one")
        self.draw_plot()

    def _on_curve_reduce_val_changed(self, _index=0):
        if self.skip:
            return
        i = self._fit_along_index()
        st = self._dim_state(f"fit:{i}", default_mode="mean")
        st["value"] = self.cob_curve_reduce_val.currentData()
        self.draw_plot()

    def _populate_curves_x_combo(self, scan=None):
        """Fill Curves X with scan axes + ROI + scan time."""
        cob = getattr(self, "cob_curves_x", None)
        if cob is None:
            return
        scan = scan or self.primary_scan()
        prev = cob.currentData()
        self.skip = True
        cob.clear()
        labels = self._axis_labels(scan) if scan is not None else ["repetition"]
        key_sets = (scan.metadata.get("key_sets") if scan else None) or []
        if key_sets:
            for i, key in enumerate(key_sets):
                cob.addItem(key[-1], (CURVES_X_AXIS, i))
        else:
            cob.addItem(labels[0] if labels else "repetition", (CURVES_X_AXIS, 0))
        cob.addItem("ROI", (CURVES_X_ROI, None))
        cob.addItem("scan time", (CURVES_X_TIME, None))
        # Restore previous selection if still present
        restore = 0
        if prev is not None:
            for i in range(cob.count()):
                if cob.itemData(i) == prev:
                    restore = i
                    break
        cob.setCurrentIndex(restore)
        self.skip = False
        self._sync_fit_along_visibility()

    def _on_curves_x_changed(self, _index=0):
        if self.skip:
            return
        kind, axis_i = self._curves_x_selection()
        self._sync_fit_along_visibility()
        if kind == CURVES_X_AXIS and axis_i is not None:
            if 0 <= axis_i < self.cob_x.count():
                if self.cob_x.currentIndex() != axis_i:
                    self.cob_x.setCurrentIndex(axis_i)  # triggers set_x
                else:
                    self.set_x(axis_i)  # same axis index — still need axis-mode redraw
            else:
                self.set_x(axis_i)
            return
        # ROI / time: keep Fit along plots; just redraw
        if not self.scans:
            return
        fit_along = self._fit_along_index()
        for scan in self.scans:
            self._ensure_scan_plots(scan, fit_along)
        self.sync_primary_aliases()
        primary = self.primary_scan()
        if primary is not None and primary.plots is not None:
            self.scrollbar.setRange(0, max(primary.plots.n_plots() - 1, 0))
        func = primary.metadata["function"] if primary else ""
        self.pw_plot.setLabel("left", func, **font)
        self.pw_plot.setLabel(
            "bottom",
            "ROI" if kind == CURVES_X_ROI else "Δt from first scan (s)",
            **font,
        )
        self._rebuild_curves_dim_bar()
        self.draw_plot()
        self.set_title()

    def _collapse_curve_point(self, scan, slice_index, roi, x_pick=None):
        """Collapse a 1D curve to one (y, yerr) for ROI/time display X.

        x_pick=None → mean over all Fit-along points; else nearest binned x.
        """
        if scan.plots is None or not scan.plots.plots:
            return np.nan, np.nan
        if slice_index >= scan.plots.n_plots() or roi >= scan.plots.n_rois:
            return np.nan, np.nan
        x, y = scan.plots.get_data(slice_index, roi)
        if not scan.metadata["key_sets"]:
            x = np.arange(len(y))
        if "survival" in scan.metadata["function"].lower():
            ux, means, err = self.get_survival_rate(x, y)
            means = np.asarray(means, dtype=float)
            ux = np.asarray(ux, dtype=float)
            if means.size == 0 or not np.any(np.isfinite(means)):
                return np.nan, np.nan
            half = 0.5 * (np.asarray(err[0], dtype=float) + np.asarray(err[1], dtype=float))
            if x_pick is None:
                yv = float(np.nanmean(means))
                ye = float(np.nanmean(half[np.isfinite(half)])) if np.any(np.isfinite(half)) else 0.0
            else:
                j = int(np.nanargmin(np.abs(ux - float(x_pick))))
                yv = float(means[j])
                ye = float(half[j]) if np.isfinite(half[j]) else 0.0
            return yv, ye
        ux, means, err = self.get_errorbars(x, y)
        means = np.asarray(means, dtype=float)
        ux = np.asarray(ux, dtype=float)
        err = np.asarray(err, dtype=float)
        if means.size == 0 or not np.any(np.isfinite(means)):
            return np.nan, np.nan
        if x_pick is None:
            yv = float(np.nanmean(means))
            ye = float(np.nanmean(err[np.isfinite(err)])) if np.any(np.isfinite(err)) else 0.0
        else:
            j = int(np.nanargmin(np.abs(ux - float(x_pick))))
            yv = float(means[j])
            ye = float(err[j]) if np.isfinite(err[j]) else 0.0
        return yv, ye

    def _unique_axis_values(self, scan, axis_index):
        pts = (scan.metadata.get("points") if scan else None) or []
        vals = []
        for p in pts:
            if axis_index < len(p):
                vals.append(p[axis_index])
        if not vals:
            return []
        return sorted({float(v) for v in vals})

    def _dim_state(self, key, default_mode="one"):
        st = self.curves_dim_state.setdefault(key, {"mode": default_mode, "value": None})
        return st

    def _rebuild_curves_dim_bar(self):
        """Dimensions row: remaining scan axes only (One / All). ROI/scans = Browser."""
        lay = getattr(self, "curves_dim_layout", None)
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        scan = self.primary_scan()
        if scan is None:
            self.curves_dim_bar.hide()
            return

        kind, x_axis = self._curves_x_selection()
        fit_along = self._fit_along_index()
        key_sets = scan.metadata.get("key_sets") or []
        rows = []

        tip = QLabel("Dimensions:")
        tip.setStyleSheet(f"color: {THEME['muted']};")
        tip.setToolTip(
            "Remaining scan axes: One (fix value) or All (overlay). "
            "ROI / scans: select in Browser."
        )
        lay.addWidget(tip)
        note = QLabel("ROI/scans → Browser")
        note.setStyleSheet(f"color: {THEME['muted']}; font-size: 11px;")
        lay.addWidget(note)

        for i, key in enumerate(key_sets):
            name = key[-1]
            if kind == CURVES_X_AXIS and i == x_axis:
                continue
            # Curve axis handled on main strip (Reduce), not here
            if kind in (CURVES_X_ROI, CURVES_X_TIME) and i == fit_along:
                continue

            dkey = f"axis:{i}"
            st = self._dim_state(dkey, default_mode="one")
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.addWidget(QLabel(f"{name}:"))
            mode = QComboBox()
            mode.addItem("One", "one")
            mode.addItem("All", "all")
            mode.setCurrentIndex(0 if st["mode"] != "all" else 1)
            vals = self._unique_axis_values(scan, i)
            val_cob = QComboBox()
            for v in vals:
                val_cob.addItem(f"{v:g}", v)
            if st["value"] is not None and vals:
                nearest = min(vals, key=lambda v: abs(v - float(st["value"])))
                val_cob.setCurrentIndex(vals.index(nearest))
                st["value"] = nearest
            elif vals:
                st["value"] = vals[0]
            val_cob.setEnabled(st["mode"] == "one")
            val_cob.setMinimumWidth(80)

            def _on_ax_mode(m, k=dkey, vc=val_cob):
                self.curves_dim_state[k]["mode"] = m
                vc.setEnabled(m == "one")
                self.draw_plot()

            def _on_ax_val(_t, k=dkey, vc=val_cob):
                self.curves_dim_state[k]["value"] = vc.currentData()
                self.draw_plot()

            mode.currentIndexChanged.connect(
                lambda _=0, c=mode: _on_ax_mode(c.currentData())
            )
            val_cob.currentIndexChanged.connect(_on_ax_val)
            row.addWidget(mode)
            row.addWidget(val_cob)
            lay.addWidget(box)
            rows.append(box)

        lay.addStretch(1)
        # Legacy dim bar replaced by EncodingPanel — keep hidden
        self.curves_dim_bar.setVisible(False)

    def _curves_roi_indices(self):
        """ROI from Curves encoding dim panel."""
        enc = getattr(self.curves_dock.state, "encoding", None) or {}
        if enc.get("dims") is not None and "roi" in (enc.get("dims") or {}):
            return self._encoding_roi_indices(enc)
        if enc.get("x_dim") == "roi":
            # X is ROI → all ROIs along X
            cat = {d["id"]: d for d in self._cube_dim_catalog()}
            return list((cat.get("roi") or {}).get("values") or [0])
        return self._encoding_roi_indices(enc) if enc else [0]

    def _curves_scan_indices(self):
        """Scan filter from Curves encoding (not Browser selection)."""
        enc = getattr(self.curves_dock.state, "encoding", None) or {}
        if enc.get("x_dim") == "scan":
            return list(range(len(self.scans)))
        if enc.get("dims") is not None:
            return self._encoding_scan_indices(enc)
        return self.selected_scan_indices()

    def _fit_along_pick(self):
        """None = mean over curve; float = One value on Fit along (ROI/time X)."""
        kind, _ = self._curves_x_selection()
        if kind not in (CURVES_X_ROI, CURVES_X_TIME):
            return None
        i = self._fit_along_index()
        st = self.curves_dim_state.get(f"fit:{i}", {"mode": "mean"})
        if st.get("mode") == "one":
            return st.get("value")
        return None

    def _curve_axis_expand_picks(self, scan):
        """When X is ROI/scan: how to expand the curve axis (pulse_detuning etc.).

        Returns list of x_pick values:
          - [None] = Mean (collapse whole curve)
          - [v] = One value
          - [v1, v2, ...] = All/Multi unique curve-axis values
        """
        kind, _ = self._curves_x_selection()
        if kind not in (CURVES_X_ROI, CURVES_X_TIME):
            return [None]
        i = self._fit_along_index()
        st = self.curves_dim_state.get(f"fit:{i}", {"mode": "mean"})
        mode = st.get("mode") or "mean"
        if mode == "one":
            return [st.get("value")]
        if mode == "mean":
            return [None]
        # all / multi → unique values along curve axis from scan data
        vals = self._unique_axis_values(scan, i)
        if mode == "multi":
            want = st.get("values") or []
            if want:
                picked = []
                for w in want:
                    if not vals:
                        continue
                    nearest = min(vals, key=lambda v: abs(v - float(w)))
                    if nearest not in picked:
                        picked.append(nearest)
                return picked if picked else vals
        return vals if vals else [None]

    def _clear_plot_legend(self, pw):
        """Remove stale LegendItem so redraw does not keep old ROI names."""
        if pw is None:
            return
        try:
            pi = pw.getPlotItem()
        except Exception:
            return
        legend = getattr(pi, "legend", None)
        if legend is None:
            return
        try:
            legend.clear()
        except Exception:
            pass
        try:
            if legend.scene() is not None:
                legend.scene().removeItem(legend)
        except Exception:
            pass
        try:
            pi.legend = None
        except Exception:
            pass

    def _matching_slice_indices(self, scan):
        """Slice indices whose key matches One-constraints on non-X / non-fit axes."""
        if scan.plots is None or not scan.plots.plots:
            return []
        kind, x_axis = self._curves_x_selection()
        fit_along = scan.plots.x_index
        key_sets = scan.metadata.get("key_sets") or []
        key_axis_indices = [i for i in range(len(key_sets)) if i != fit_along]
        keys = list(scan.plots.plots.keys())
        out = []
        for si, key in enumerate(keys):
            ok = True
            for pos, ax in enumerate(key_axis_indices):
                # When Curves X is this axis, keys don't contain it (it's fit_along)
                if kind == CURVES_X_AXIS and ax == x_axis:
                    continue
                dkey = f"axis:{ax}"
                st = self.curves_dim_state.get(dkey, {"mode": "one"})
                if st.get("mode") == "all":
                    continue
                want = st.get("value")
                if want is None or pos >= len(key):
                    continue
                if float(key[pos]) != float(want):
                    ok = False
                    break
            if ok:
                out.append(si)
        return out if out else list(range(len(keys)))

    def _axis_labels(self, scan=None):
        scan = scan or self.primary_scan()
        if scan is None or not scan.metadata.get("key_sets"):
            return ["repetition"]
        return [key[-1] for key in scan.metadata["key_sets"]]

    def _outer_dimension_choices(self, fit_along, key_sets):
        """Labels for Batch outer dimension: time, ROI, other scan axes."""
        choices = [
            ("scan time", BatchAnalyzeDialog.OUTER_TIME, None),
            ("ROI (all)", BatchAnalyzeDialog.OUTER_ROI, None),
        ]
        n = len(key_sets or [])
        for i, key in enumerate(key_sets or []):
            if i == fit_along:
                continue
            choices.append((key[-1], BatchAnalyzeDialog.OUTER_AXIS, i))
        if n == 0:
            # still allow time / ROI
            pass
        return choices

    def _binned_measurement(self, scan, slice_index, roi):
        """Return unique_x, means for one curve (same rules as Curves)."""
        x, y = scan.plots.get_data(slice_index, roi)
        if not scan.metadata["key_sets"]:
            x = np.arange(len(y))
        if "survival" in scan.metadata["function"].lower():
            return self.get_survival_rate(x, y)[:2]
        unique_x, means, _ = self.get_errorbars(x, y)
        return unique_x, means

    def _ensure_scan_plots(self, scan, fit_along):
        if scan.plots is None or scan.plots.x_index != fit_along:
            self.build_plots_for_scan(scan, fit_along)

    def _recipe_for_live_refresh(self, rec):
        """Keep analysis kind/params; drop frozen Browser context (use current UI)."""
        r = dict(rec)
        for k in (
            "scan_sel_names", "roi_sel", "slice", "x_index",
            "scan_name", "fixed", "scan_sel",
        ):
            r.pop(k, None)
        # 2D: also drop frozen ROI so current selection is used
        if r.get("type") == "2d":
            r.pop("roi", None)
        return r

    def refresh_all_analysis(self, checked=False):
        """Re-run analysis docks with *current* Browser/Curves context.

        Recipes keep the analysis kind (batch outer, ROI grid param, 2D axes…).
        Scan/ROI/slice/Fit-along come from the live UI — not the frozen recipe.
        `checked` absorbs QPushButton.clicked(bool) — required on PyQt6.
        """
        _ = checked
        targets = [
            d for d in list(self.plot_docks)
            if d is not self.curves_dock
            and isinstance(getattr(d.state, "recipe", None), dict)
            and d.state.recipe.get("type") in ("export", "2d", "heatmap", "batch")
        ]
        if not targets:
            self.statusBar().showMessage("Refresh all: no analysis docks with recipes", 3000)
            return
        recipes = [self._recipe_for_live_refresh(d.state.recipe) for d in targets]
        for d in targets:
            try:
                d.close()
            except Exception:
                pass
        ok, fail = 0, 0
        for rec in recipes:
            try:
                self._replay_tab_recipe(rec, restore_ui_context=False)
                ok += 1
            except Exception as exc:
                fail += 1
                logging.exception("Refresh failed for %s: %s", rec.get("title"), exc)
        self.statusBar().showMessage(
            f"Refresh all: {ok} ok" + (f", {fail} failed" if fail else ""),
            5000,
        )

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
        self._update_curves_analyze_enabled()
        # Refresh scan dim values in encoding catalog
        if getattr(self, "curves_encoding_panel", None) is not None and self.scans:
            prev = copy.deepcopy(self.curves_dock.state.encoding)
            self._populate_curves_encoding()
            if prev:
                self.curves_dock.state.encoding = prev
                self.curves_encoding_panel.set_catalog(self._cube_dim_catalog(), prev)
        self.draw_plot()
        self.set_title()

    def _update_curves_analyze_enabled(self):
        """Enable 2D / ROI grid only when the primary selection supports them."""
        ctrls = getattr(self.curves_dock, "ctrls", None) if getattr(self, "curves_dock", None) else None
        if ctrls is None:
            return
        scan = self.primary_scan()
        n_axes = len((scan.metadata.get("key_sets") if scan else None) or [])
        n_rois = 1
        if scan is not None:
            if scan.plots is not None:
                n_rois = scan.plots.n_rois
            elif scan.data:
                try:
                    n_rois = len(scan.data[0])
                except Exception:
                    n_rois = 1
        if getattr(ctrls, "bt_2d", None) is not None:
            ctrls.bt_2d.setEnabled(n_axes >= 2)
            ctrls.bt_2d.setToolTip(
                "Measurement value vs two scan axes."
                if n_axes >= 2
                else "Needs a multi-axis scan (≥2 parameters in log.csv)."
            )
        if getattr(ctrls, "bt_roi_grid", None) is not None:
            ctrls.bt_roi_grid.setEnabled(n_rois >= 2)
            ctrls.bt_roi_grid.setToolTip(
                "Fit all ROIs; show one fit parameter as a rows×cols grid."
                if n_rois >= 2
                else "Needs ≥2 ROIs."
            )

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
        kind, _ = self._curves_x_selection()
        if kind == CURVES_X_ROI:
            self.pw_plot.setLabel("bottom", "ROI", **font)
        elif kind == CURVES_X_TIME:
            self.pw_plot.setLabel("bottom", "scan time", **font)
        elif primary.metadata["key_sets"]:
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
        kind, _ = self._curves_x_selection()
        if kind == CURVES_X_ROI:
            self._draw_plot_vs_roi()
        elif kind == CURVES_X_TIME:
            self._draw_plot_vs_time()
        else:
            self._draw_plot_vs_axis()
        self.set_title()

    def _draw_plot_vs_axis(self):
        """Classic Curves: measurement vs Fit-along / scan axis."""
        self.pw_plot.clear()
        self._clear_plot_legend(self.pw_plot)
        vst = self._curves_view_state()
        vst.series_items = []

        scan_idxs = self._curves_scan_indices()
        roi_idxs = self._curves_roi_indices()
        if not scan_idxs or not self.scans:
            self.lb_fit.setText("")
            return

        series = []  # (scan, roi, slice_index, extra_label)
        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None or not scan.plots.plots:
                continue
            slices = self._matching_slice_indices(scan)
            keys = list(scan.plots.plots.keys())
            for slice_index in slices:
                key = keys[slice_index] if slice_index < len(keys) else ()
                extra = ",".join(f"{v:g}" for v in key) if len(slices) > 1 and key else ""
                for roi in roi_idxs:
                    if roi >= scan.plots.n_rois:
                        continue
                    series.append((scan, roi, slice_index, extra))

        show_legend = bool(vst.show_legend)
        if show_legend:
            self.pw_plot.addLegend()

        n_scans = len({id(s) for s, _, _, _ in series})
        n_rois = len({r for _, r, _, _ in series})
        for color_i, (scan, roi, slice_index, extra) in enumerate(series):
            x, y = scan.plots.get_data(slice_index, roi)
            if not scan.metadata["key_sets"]:
                x = np.arange(len(y))
            name = self._series_legend_name(scan, roi, n_scans, n_rois) if show_legend else None
            if name and extra:
                name = f"{name}[{extra}]"
            style = self._series_style(color_i)
            if "survival" in scan.metadata["function"].lower():
                self.plot_survival(x, y, style=style, name=name, plot_widget=self.pw_plot, view_state=vst)
            else:
                self.plot_errorbars(x, y, style=style, name=name, plot_widget=self.pw_plot, view_state=vst)

        summaries = []
        for scan, roi, slice_index, _extra in series:
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

    def _draw_plot_vs_roi(self):
        """Measurement vs ROI; curve axis Mean/One/All/Multi; other axes One/All."""
        self.pw_plot.clear()
        self._clear_plot_legend(self.pw_plot)
        vst = self._curves_view_state()
        vst.series_items = []
        self.lb_fit.setText("")
        scan_idxs = self._curves_scan_indices()
        roi_idxs = self._curves_roi_indices()
        if not scan_idxs or not roi_idxs:
            return
        show_legend = bool(vst.show_legend)
        if show_legend:
            self.pw_plot.addLegend()
        self.pw_plot.setLabel("bottom", "ROI", **font)
        color_i = 0
        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None:
                continue
            picks = self._curve_axis_expand_picks(scan)
            slices = self._matching_slice_indices(scan)
            keys = list(scan.plots.plots.keys())
            for x_pick in picks:
                for slice_index in slices:
                    key = keys[slice_index] if slice_index < len(keys) else ()
                    xs, ys, yerr = [], [], []
                    for roi in roi_idxs:
                        yv, ye = self._collapse_curve_point(
                            scan, slice_index, roi, x_pick=x_pick
                        )
                        if not np.isfinite(yv):
                            continue
                        xs.append(float(roi))
                        ys.append(yv)
                        yerr.append(ye if np.isfinite(ye) else 0.0)
                    if not xs:
                        continue
                    name = self._scan_short_name(scan) if show_legend else None
                    if name and x_pick is not None and len(picks) > 1:
                        name = f"{name}@{x_pick:g}"
                    if name and len(slices) > 1 and key:
                        name = f"{name}[{','.join(f'{v:g}' for v in key)}]"
                    self._add_errorbar_scatter(
                        self.pw_plot.getPlotItem(),
                        np.asarray(xs), np.asarray(ys), np.asarray(yerr) * 2,
                        y_centers=np.asarray(ys),
                        style=self._series_style(color_i),
                        name=name,
                        view_style=vst.style,
                        show_errorbars=vst.show_errorbars,
                        series_out=vst.series_items,
                    )
                    color_i += 1

    def _draw_plot_vs_time(self):
        """Measurement vs scan time; curve axis Mean/One/All/Multi."""
        self.pw_plot.clear()
        self._clear_plot_legend(self.pw_plot)
        vst = self._curves_view_state()
        vst.series_items = []
        self.lb_fit.setText("")
        scan_idxs = self._curves_scan_indices()
        roi_idxs = self._curves_roi_indices()
        if not scan_idxs or not roi_idxs:
            return
        show_legend = bool(vst.show_legend)

        raw_times = []
        parsed_any = False
        for s_i in scan_idxs:
            ts = parse_scan_timestamp(self.scans[s_i].path)
            if ts:
                raw_times.append(ts.timestamp())
                parsed_any = True
            else:
                raw_times.append(float(s_i))
        if parsed_any:
            t0 = min(raw_times)
            times = [t - t0 for t in raw_times]
            self.pw_plot.setLabel("bottom", "Δt from first scan (s)", **font)
        else:
            times = [float(i) for i in range(len(scan_idxs))]
            self.pw_plot.setLabel("bottom", "scan index", **font)
        if show_legend:
            self.pw_plot.addLegend()

        # Expand curve-axis picks from first scan (shared detuning grid)
        probe = self.scans[scan_idxs[0]]
        self._ensure_scan_plots(probe, self._fit_along_index())
        picks = self._curve_axis_expand_picks(probe)

        color_i = 0
        for x_pick in picks:
            for roi in roi_idxs:
                xs, ys, yerr = [], [], []
                for j, s_i in enumerate(scan_idxs):
                    scan = self.scans[s_i]
                    if scan.plots is None or not scan.plots.plots:
                        continue
                    slices = self._matching_slice_indices(scan)
                    if not slices:
                        continue
                    si = slices[0]
                    yv, ye = self._collapse_curve_point(scan, si, roi, x_pick=x_pick)
                    if not np.isfinite(yv):
                        continue
                    xs.append(times[j])
                    ys.append(yv)
                    yerr.append(ye if np.isfinite(ye) else 0.0)
                if not xs:
                    continue
                order = np.argsort(xs)
                xs = np.asarray(xs, dtype=float)[order]
                ys = np.asarray(ys, dtype=float)[order]
                yerr = np.asarray(yerr, dtype=float)[order]
                if show_legend:
                    if len(picks) > 1 and x_pick is not None:
                        name = (
                            f"ROI {roi}@{x_pick:g}"
                            if len(roi_idxs) > 1
                            else f"{x_pick:g}"
                        )
                    else:
                        name = f"ROI {roi}"
                else:
                    name = None
                self._add_errorbar_scatter(
                    self.pw_plot.getPlotItem(),
                    xs, ys, yerr * 2, y_centers=ys,
                    style=self._series_style(color_i),
                    name=name,
                    view_style=vst.style,
                    show_errorbars=vst.show_errorbars,
                    series_out=vst.series_items,
                )
                color_i += 1

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
        kind, _ = self._curves_x_selection()
        n_scans = len(self._curves_scan_indices())
        n_rois = len(self._curves_roi_indices())
        if kind in (CURVES_X_ROI, CURVES_X_TIME):
            title = f"({n_scans} scans × {n_rois} ROIs)"
            self.pw_plot.setTitle(title, **font)
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
        # Reset / populate universal encoding
        if self.metadata["key_sets"]:
            self.curves_dock.state.encoding = default_encoding(dim_id_axis(0))
        else:
            self.curves_dock.state.encoding = default_encoding(dim_id_axis(0))
        self._populate_curves_encoding()
        enc = self.curves_dock.state.encoding
        self._sync_legacy_from_encoding(enc)
        if self.scans:
            self._show_curves_dock()
        self.draw_plot()
        self.set_title()
        self._update_curves_analyze_enabled()

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

    def two_d_plot(self, recipe=None, dock=None):
        """Measurement-value image vs two scan axes (not a fit-parameter map)."""
        if not isinstance(recipe, dict):
            recipe = None
        _ = dock  # source dock reserved for future derived-scan context

        scan_idxs = self.selected_scan_indices()
        if not scan_idxs and self.scans:
            scan_idxs = [0]
        if not scan_idxs:
            logging.error("2D plot: no scans loaded")
            self.statusBar().showMessage("2D plot: load a scan first", 4000)
            return

        if recipe is None:
            labels = [self.scans[i].name for i in scan_idxs]
            probe = self.scans[scan_idxs[0]]
            axis_labels = self._axis_labels(probe)
            if len(axis_labels) < 2:
                msg = (
                    "2D plot needs a scan with at least two axes "
                    f"(this scan only has: {', '.join(axis_labels) or 'none'})."
                )
                logging.info(msg)
                self.statusBar().showMessage("2D plot: need ≥2 scan axes", 5000)
                QMessageBox.information(self, "2D plot", msg)
                return
            n_rois = probe.plots.n_rois if probe.plots else max(1, len(probe.data[0]) if probe.data else 1)
            default_roi = self.selected_roi_indices()[0]
            dialog = TwoDPlotDialog(labels, axis_labels, n_rois, parent=self)
            dialog.cob_roi.setCurrentIndex(min(default_roi, n_rois - 1))
            # Prefer current Fit along as Axis X when possible
            fit_along = self._fit_along_index()
            if 0 <= fit_along < dialog.cob_axis_x.count():
                dialog.cob_axis_x.setCurrentIndex(fit_along)
                if dialog.cob_axis_y.count() > 1:
                    dialog.cob_axis_y.setCurrentIndex(0 if fit_along != 0 else 1)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            label_i, roi, axis_x, axis_y = dialog.get_values()
            scan = self.scans[scan_idxs[label_i]]
        else:
            name = recipe.get("scan_name")
            scan = next((s for s in self.scans if s.name == name), None)
            if scan is None and scan_idxs:
                scan = self.scans[scan_idxs[0]]
            if scan is None:
                logging.error("2D recipe: scan not found")
                return
            roi = int(recipe["roi"]) if "roi" in recipe else self.selected_roi_indices()[0]
            axis_x = int(recipe.get("axis_x", self._fit_along_index()))
            axis_y = int(recipe.get("axis_y", 1 if axis_x == 0 else 0))

        key_sets = scan.metadata.get("key_sets") or []
        n_axes = len(key_sets)
        if n_axes < 2 or axis_x == axis_y or axis_x >= n_axes or axis_y >= n_axes:
            logging.error("2D plot: invalid axis selection")
            self.statusBar().showMessage("2D plot: pick two different axes", 4000)
            return

        # Capture fixed axes from current Curves slice BEFORE rebuilding plots
        restore_fit = self._fit_along_index()
        fixed = {}
        primary = self.primary_scan()
        if primary is not None and primary.plots is not None and primary.plots.plots:
            cur_keys = list(primary.plots.plots.keys())
            si = min(self.scrollbar.value(), len(cur_keys) - 1)
            cur_key = cur_keys[si]
            cur_fit = primary.plots.x_index
            cur_key_axes = [
                i for i in range(len(primary.metadata.get("key_sets") or [])) if i != cur_fit
            ]
            for pos, ax in enumerate(cur_key_axes):
                if ax in (axis_x, axis_y):
                    continue
                if pos < len(cur_key):
                    fixed[ax] = cur_key[pos]

        # Build along axis_x so each slice key holds the other axes (incl. axis_y)
        self._ensure_scan_plots(scan, axis_x)
        if roi >= scan.plots.n_rois:
            logging.error("2D plot: ROI %s out of range", roi)
            self._ensure_scan_plots(scan, restore_fit)
            return

        key_axis_indices = [i for i in range(n_axes) if i != axis_x]
        try:
            y_pos = key_axis_indices.index(axis_y)
        except ValueError:
            logging.error("2D plot: axis_y not in slice keys")
            self._ensure_scan_plots(scan, restore_fit)
            return

        keys_list = list(scan.plots.plots.keys())
        slices = []  # (y_val, unique_x, means)
        for slice_index, key in enumerate(keys_list):
            ok = True
            for ax, val in fixed.items():
                if ax not in key_axis_indices:
                    continue
                pos = key_axis_indices.index(ax)
                if pos >= len(key) or key[pos] != val:
                    ok = False
                    break
            if not ok:
                continue
            y_val = key[y_pos] if y_pos < len(key) else float(slice_index)
            if roi >= scan.plots.n_rois:
                continue
            unique_x, means = self._binned_measurement(scan, slice_index, roi)
            slices.append((float(y_val), unique_x, means))

        # Restore Curves Fit-along layout for this scan
        self._ensure_scan_plots(scan, restore_fit)
        self.sync_primary_aliases()

        if not slices:
            logging.error("2D plot: no slices matched fixed axes")
            self.statusBar().showMessage("2D plot: no data for current slice fixes", 4000)
            return

        slices.sort(key=lambda t: t[0])
        y_vals = np.array([s[0] for s in slices], dtype=float)
        global_x = np.unique(np.concatenate([s[1] for s in slices]))
        data = np.full((len(slices), len(global_x)), np.nan)
        for row, (_yv, unique_x, means) in enumerate(slices):
            data[row, np.searchsorted(global_x, unique_x)] = means

        x_step = (global_x[-1] - global_x[0]) / (len(global_x) - 1) if len(global_x) > 1 else 1
        y_step = (y_vals[-1] - y_vals[0]) / (len(y_vals) - 1) if len(y_vals) > 1 else 1
        x0 = float(global_x[0] - x_step / 2)
        y0 = float(y_vals[0] - y_step / 2)
        w = float(global_x[-1] - global_x[0] + x_step)
        h = float(y_vals[-1] - y_vals[0] + y_step)

        xlabel = key_sets[axis_x][-1]
        ylabel = key_sets[axis_y][-1]
        short = self._scan_short_name(scan)
        title = f"2D: {short} ROI{roi}"
        glw = self._add_image_with_colorbar(
            data, x0, y0, w, h, xlabel=xlabel, ylabel=ylabel, title=title,
        )
        rec = recipe if isinstance(recipe, dict) else {
            "type": "2d",
            "title": title,
            "scan_name": scan.name,
            "roi": roi,
            "axis_x": axis_x,
            "axis_y": axis_y,
            "x_index": restore_fit,
            "slice": self.scrollbar.value(),
            "fixed": {str(k): v for k, v in fixed.items()},
        }
        rec.setdefault("title", title)
        out = self.show_plot_dock(title, glw, kind="2d", recipe=rec)
        self._record_tab_recipe(rec, dock=out)
        self.draw_plot()

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

    def derive_fit_param_plot(self, dock=None):
        """Fit along current X; open one new universal plotter for a single fit parameter."""
        dock = dock or self.curves_dock
        src_enc = copy.deepcopy(dock.state.encoding or default_encoding())
        fitfunc = dock.state.fitfunc or self._curves_fitfunc()
        if fitfunc not in self.fitter.func_dict:
            logging.error("Unknown fit function %s", fitfunc)
            return
        params = list(self.fitter.func_dict[fitfunc]["params"])
        catalog = self._cube_dim_catalog()
        if not catalog or not self.scans:
            logging.error("No data loaded")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Derive fit-parameter plot")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Fit function: {fitfunc}"))
        row_p = QHBoxLayout()
        row_p.addWidget(QLabel("Parameter:"))
        cob_param = QComboBox()
        cob_param.addItems(params)
        row_p.addWidget(cob_param, stretch=1)
        lay.addLayout(row_p)
        row_x = QHBoxLayout()
        row_x.addWidget(QLabel("New X:"))
        cob_x = QComboBox()
        src_x = src_enc.get("x_dim")
        for d in catalog:
            if d["id"] == src_x:
                continue  # cannot use fit-along dim as outer X
            cob_x.addItem(d["label"], d["id"])
        if cob_x.count() == 0:
            for d in catalog:
                cob_x.addItem(d["label"], d["id"])
        row_x.addWidget(cob_x, stretch=1)
        lay.addLayout(row_x)
        btns = QHBoxLayout()
        ok_b = QPushButton("OK")
        cancel_b = QPushButton("Cancel")
        ok_b.clicked.connect(dlg.accept)
        cancel_b.clicked.connect(dlg.reject)
        btns.addStretch(1)
        btns.addWidget(ok_b)
        btns.addWidget(cancel_b)
        lay.addLayout(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        param_name = cob_param.currentText()
        new_x = cob_x.currentData()
        points = self._compute_derive_points(src_enc, fitfunc, param_name, new_x)
        if not points:
            QMessageBox.warning(self, "Derive", "No fit points produced.")
            return

        new_enc = default_encoding(x_dim=new_x, y_channel=f"fit:{param_name}")
        # Other dims: One on first value (single series of derived points)
        for d in catalog:
            if d["id"] == new_x:
                continue
            new_enc["dims"][d["id"]] = {
                "mode": DIM_MODE_ALL if d["id"] in ("scan", "roi") else DIM_MODE_ONE,
                "values": [d["values"][0]] if d["values"] else [],
            }

        pw = self._new_plot_widget()
        title = f"Derive: {param_name} vs {cob_x.currentText()}"
        out = self.show_plot_dock(title, pw, kind="export", floating=True)
        out.state.encoding = new_enc
        out.state.derived_points = points
        out.state.fitfunc = fitfunc
        out.state.style = dict(dock.state.style)
        out.state.show_legend = bool(dock.state.show_legend)
        out.state.show_errorbars = bool(dock.state.show_errorbars)
        self._sync_dock_ctrls_from_state(out)

        panel = EncodingPanel()
        panel.write_y_channel(new_enc["y_channel"])
        panel.set_catalog(catalog, new_enc)
        out.encoding_panel = panel
        page = out.widget()
        if isinstance(page, QWidget) and page.layout() is not None:
            page.layout().insertWidget(1, panel)
        panel.changed.connect(lambda d=out: self._on_derived_encoding_changed(d))

        recipe = {
            "type": "derive",
            "title": title,
            "param": param_name,
            "fitfunc": fitfunc,
            "x_dim": new_x,
            "source_encoding": src_enc,
        }
        self._record_tab_recipe(recipe, dock=out)
        self._redraw_universal_dock(out)
        self.statusBar().showMessage(f"Derived {param_name} ({len(points)} points)", 4000)

    def _compute_derive_points(self, src_enc, fitfunc, param_name, new_x_dim):
        """Fit each series along src X; return points [{x,y,yerr,label}, ...] for new_x_dim."""
        self._sync_legacy_from_encoding(src_enc)
        scan_idxs = self._encoding_scan_indices(src_enc) if src_enc.get("x_dim") != "scan" else list(range(len(self.scans)))
        if src_enc.get("x_dim") == "scan":
            scan_idxs = list(range(len(self.scans)))
        roi_idxs = self._encoding_roi_indices(src_enc) if src_enc.get("x_dim") != "roi" else list(
            ( {d["id"]: d for d in self._cube_dim_catalog()}.get("roi") or {} ).get("values") or [0]
        )
        if src_enc.get("x_dim") == "roi":
            cat = {d["id"]: d for d in self._cube_dim_catalog()}
            roi_idxs = list((cat.get("roi") or {}).get("values") or [0])

        fit_params = self.fitter.func_dict[fitfunc]["params"]
        if param_name not in fit_params:
            return []
        p_i = fit_params.index(param_name)
        nx_kind, nx_axis = parse_dim_id(new_x_dim)
        points = []

        for s_i in scan_idxs:
            scan = self.scans[s_i]
            if scan.plots is None:
                continue
            slices = self._matching_slice_indices(scan)
            keys = list(scan.plots.plots.keys())
            for slice_index in slices:
                key = keys[slice_index] if slice_index < len(keys) else ()
                for roi in roi_idxs:
                    if roi >= scan.plots.n_rois:
                        continue
                    try:
                        xp, yp, errors = self._series_xy_for_fit(scan, slice_index, roi)
                        if len(xp) < 2:
                            continue
                        _x, _y, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc, errors)
                    except Exception as exc:
                        logging.debug("Derive fit failed: %s", exc)
                        continue
                    # Outer X coordinate
                    if nx_kind == "roi":
                        xv = float(roi)
                    elif nx_kind == "scan":
                        ts = parse_scan_timestamp(scan.path)
                        xv = ts.timestamp() if ts else float(s_i)
                    else:
                        # axis value from slice key or mean of curve x
                        fit_along = scan.plots.x_index
                        key_sets = scan.metadata.get("key_sets") or []
                        key_axis_indices = [i for i in range(len(key_sets)) if i != fit_along]
                        if nx_axis in key_axis_indices:
                            pos = key_axis_indices.index(nx_axis)
                            xv = float(key[pos]) if pos < len(key) else np.nan
                        elif nx_axis == fit_along:
                            xv = float(np.nanmean(xp))
                        else:
                            xv = np.nan
                    if not np.isfinite(xv):
                        continue
                    label = f"{self._scan_short_name(scan)}/R{roi}"
                    points.append({
                        "x": xv,
                        "y": float(popt[p_i]),
                        "yerr": float(perr[p_i]) if np.isfinite(perr[p_i]) else 0.0,
                        "label": label,
                        "scan_i": s_i,
                        "roi": roi,
                    })
        if nx_kind == "scan" and points:
            t0 = min(p["x"] for p in points)
            for p in points:
                p["x"] = p["x"] - t0
        return points

    def _on_derived_encoding_changed(self, dock):
        if self.skip:
            return
        panel = getattr(dock, "encoding_panel", None)
        if panel is None:
            return
        enc = panel.read_encoding()
        prev = dock.state.encoding or {}
        enc["y_channel"] = prev.get("y_channel") or Y_CHANNEL_MEASUREMENT
        dock.state.encoding = enc
        # Re-filter cached derived_points by encoding dims when possible
        self._redraw_universal_dock(dock)

    def _redraw_universal_dock(self, dock):
        """Draw measurement curves or derived fit-param points for a plotter dock."""
        pw = dock.plot_widget
        if not isinstance(pw, PlotWidget):
            return
        enc = dock.state.encoding or {}
        ych = enc.get("y_channel") or Y_CHANNEL_MEASUREMENT
        pw.clear()
        dock.state.series_items = []
        if dock.state.show_legend:
            pw.addLegend()

        if ych != Y_CHANNEL_MEASUREMENT and dock.state.derived_points:
            pts = list(dock.state.derived_points)
            # Optional filter by ROI/scan One/Multi
            dims = enc.get("dims") or {}
            roi_st = dims.get("roi") or {}
            if roi_st.get("mode") == DIM_MODE_ONE and roi_st.get("values"):
                want = int(roi_st["values"][0])
                pts = [p for p in pts if int(p.get("roi", -1)) == want]
            elif roi_st.get("mode") == DIM_MODE_MULTI and roi_st.get("values"):
                want = {int(v) for v in roi_st["values"]}
                pts = [p for p in pts if int(p.get("roi", -1)) in want]
            scan_st = dims.get("scan") or {}
            if scan_st.get("mode") == DIM_MODE_ONE and scan_st.get("values"):
                want = int(scan_st["values"][0])
                pts = [p for p in pts if int(p.get("scan_i", -1)) == want]
            elif scan_st.get("mode") == DIM_MODE_MULTI and scan_st.get("values"):
                want = {int(v) for v in scan_st["values"]}
                pts = [p for p in pts if int(p.get("scan_i", -1)) in want]

            xs = np.asarray([p["x"] for p in pts], dtype=float)
            ys = np.asarray([p["y"] for p in pts], dtype=float)
            yerr = np.asarray([p.get("yerr", 0.0) for p in pts], dtype=float)
            order = np.argsort(xs)
            xs, ys, yerr = xs[order], ys[order], yerr[order]
            x_dim = enc.get("x_dim") or ""
            label = next((d["label"] for d in self._cube_dim_catalog() if d["id"] == x_dim), x_dim)
            pw.setLabel("bottom", label, **font)
            ylab = ych[4:] if ych.startswith("fit:") else ych
            pw.setLabel("left", ylab, **font)
            if len(xs):
                self._add_errorbar_scatter(
                    pw.getPlotItem(),
                    xs, ys, yerr * 2,
                    y_centers=ys,
                    style=self._series_style(0),
                    name=ylab if dock.state.show_legend else None,
                    view_style=dock.state.style,
                    show_errorbars=dock.state.show_errorbars,
                    series_out=dock.state.series_items,
                )
            return

        # Measurement: reuse Curves draw path when this is the curves dock
        if dock is self.curves_dock:
            self.draw_plot()
            return

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

    def _reduce_fit_points(self, scan, fitfunc, param_name, slice_roi_pairs):
        """
        Fit each (slice_index, roi) along current scan.plots (Fit along).
        Returns xs_ignored, ys, yerr, failures — caller supplies xs separately.
        """
        fit_params = self.fitter.func_dict[fitfunc]["params"]
        fit_value = fit_params.index(param_name)
        ys, yerr, failures = [], [], []
        for i, r in slice_roi_pairs:
            try:
                xp, yp, errors = self._series_xy_for_fit(scan, i, r)
                if len(xp) < 2:
                    raise ValueError("not enough points")
                x, y, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc, errors)
                ys.append(popt[fit_value])
                yerr.append(perr[fit_value])
                fit = self.pw_plot.plot(x, y)
                scan.plots.add_fit(i, r, fit, fit_params, popt, perr)
            except Exception as exc:
                failures.append(f"slice={i} ROI={r}: {exc}")
                ys.append(np.nan)
                yerr.append(np.nan)
        return ys, yerr, failures

    def _plot_param_scatter(
        self, scan, fitfunc, param_name, mode, recipe_extra=None, outer_axis=None, rois=None,
    ):
        """Fit-param plot for one scan: vs a scan axis or vs ROI (all ROIs for ROI mode)."""
        fit_along = self._fit_along_index()
        self._ensure_scan_plots(scan, fit_along)
        if scan.plots is None or not scan.plots.plots:
            logging.error("No plot data for %s", scan.name)
            return False
        fit_params = self.fitter.func_dict[fitfunc]["params"]
        if param_name not in fit_params:
            logging.error("Unknown param %s", param_name)
            return False
        slice_index = self.scrollbar.value()
        n_axes = len(scan.metadata.get("key_sets") or [])
        key_axis_indices = [i for i in range(n_axes) if i != fit_along]

        if mode in (BatchAnalyzeDialog.MODE_ROI, BatchAnalyzeDialog.OUTER_ROI):
            # Always all ROIs (structure-preserving; matches ROI grid)
            all_rois = list(range(scan.plots.n_rois))
            pairs = [(min(slice_index, scan.plots.n_plots() - 1), r) for r in all_rois]
            xs = list(all_rois)
            xlabel = "ROI"
            mode_tag = BatchAnalyzeDialog.MODE_ROI
        else:
            if outer_axis is None:
                # Legacy: first non-fit-along axis
                if not key_axis_indices:
                    logging.error("Scan has no secondary axis for vs-axis mode")
                    return False
                outer_axis = key_axis_indices[0]
            if outer_axis not in key_axis_indices:
                logging.error("Outer axis %s not available with Fit along=%s", outer_axis, fit_along)
                return False
            y_pos = key_axis_indices.index(outer_axis)
            xlabel = scan.metadata["key_sets"][outer_axis][-1]
            roi0 = (rois or self.selected_roi_indices() or [0])[0]
            # Fix other key dims to current slice
            keys = list(scan.plots.plots.keys())
            cur = keys[min(slice_index, len(keys) - 1)]
            pairs, xs = [], []
            for i, key in enumerate(keys):
                ok = True
                for pos, ax in enumerate(key_axis_indices):
                    if ax == outer_axis:
                        continue
                    if pos < len(key) and pos < len(cur) and key[pos] != cur[pos]:
                        ok = False
                        break
                if not ok:
                    continue
                pairs.append((i, roi0))
                xs.append(float(key[y_pos]) if y_pos < len(key) else float(i))
            mode_tag = BatchAnalyzeDialog.MODE_AXIS

        ys, yerr, failures = self._reduce_fit_points(scan, fitfunc, param_name, pairs)
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
        title = f"Fit param: {param_name} ({mode_tag})"
        recipe = {
            "type": "batch",
            "title": title,
            "mode": mode_tag,
            "outer": BatchAnalyzeDialog.OUTER_ROI if mode_tag == BatchAnalyzeDialog.MODE_ROI else BatchAnalyzeDialog.OUTER_AXIS,
            "outer_axis": outer_axis if mode_tag == BatchAnalyzeDialog.MODE_AXIS else None,
            "fitfunc": fitfunc,
            "params": [param_name],
            "scan_name": scan.name,
            "roi_sel": rois or self.selected_roi_indices(),
            "slice": slice_index,
            "x_index": fit_along,
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

    def generate_heatmap(self, recipe=None, dock=None):
        """ROI grid: fit all ROIs, show one parameter as rows×cols (not scan-axis 2D)."""
        if not isinstance(recipe, dict):
            recipe = None
        _ = dock
        scan_idxs = self.selected_scan_indices()
        if not scan_idxs:
            logging.error("Select at least one scan for ROI grid")
            self.statusBar().showMessage("ROI grid: select a scan", 4000)
            return
        labels = [self.scans[i].name for i in scan_idxs]
        probe = self.scans[scan_idxs[0]]
        fit_along = self._fit_along_index()
        self._ensure_scan_plots(probe, fit_along)
        if probe.plots is None:
            logging.error("No plots for selected scan")
            return
        if probe.plots.n_rois < 2:
            logging.error("ROI grid needs multiple ROIs")
            self.statusBar().showMessage("ROI grid: need ≥2 ROIs", 4000)
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
                logging.error("ROI grid recipe: scan not found")
                return
            param_name = recipe["param"]
            fit_value = fit_params.index(param_name) if param_name in fit_params else 0
            num_rows, num_cols = recipe["rows"], recipe["cols"]
            fitfunc = recipe.get("fitfunc", fitfunc)
            if "x_index" in recipe:
                fit_along = int(recipe["x_index"])

        self._ensure_scan_plots(scan, fit_along)
        if scan.plots is None or scan.plots.n_rois < 2:
            logging.error("Cannot make ROI grid of global / single-ROI data")
            return

        n_rois = scan.plots.n_rois
        if recipe is None or "slice" not in recipe:
            slice_index = self.scrollbar.value()
        else:
            slice_index = recipe["slice"]
        slice_index = min(slice_index, scan.plots.n_plots() - 1)
        pairs = [(slice_index, r) for r in range(n_rois)]
        ys, _yerr, failures = self._reduce_fit_points(scan, fitfunc, param_name, pairs)

        if failures:
            logging.warning("ROI grid fit issues (%s/%s ROIs):\n%s",
                            len(failures), n_rois, "\n".join(failures[:10]))
        if not any(np.isfinite(ys)):
            logging.error("ROI grid: no ROI produced a valid fit")
            return
        if num_rows * num_cols != len(ys):
            logging.error(
                "ROI grid %sx%s != %s ROIs; adjust rows/cols in dialog",
                num_rows, num_cols, len(ys),
            )
            self.statusBar().showMessage(
                f"ROI grid: rows×cols must equal {n_rois}", 5000
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
        title = f"ROI grid: {param_name} ({short})"
        rec = recipe if isinstance(recipe, dict) else {
            "type": "heatmap",
            "title": title,
            "scan_name": scan.name,
            "param": param_name,
            "fitfunc": fitfunc,
            "rows": num_rows,
            "cols": num_cols,
            "slice": slice_index,
            "x_index": fit_along,
        }
        rec.setdefault("title", title)
        out = self.show_plot_dock(title, glw, kind="heatmap", recipe=rec)
        self._record_tab_recipe(rec, dock=out)

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
        """Non-destructive: concatenate all loaded scans into a derived floating dock."""
        idxs = list(range(len(self.scans)))
        if len(idxs) < 2:
            logging.error("Need at least two loaded scans to merge")
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

    def _build_dock_encoding_panel(self, dock):
        """Per-dock X / Curve axis / Reduce / Dimensions (same model as Curves)."""
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(2, 2, 2, 2)
        col.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(4)
        enc = SimpleNamespace()

        row.addWidget(QLabel("X:"))
        enc.cob_x_dim = QComboBox()
        enc.cob_x_dim.setToolTip("Horizontal axis: scan parameter, ROI, or scan time.")
        row.addWidget(enc.cob_x_dim)

        enc.lb_curve = QLabel("Curve axis:")
        enc.cob_curve = QComboBox()
        enc.cob_curve.setToolTip("Scan axis used to build 1D curves.")
        row.addWidget(enc.lb_curve)
        row.addWidget(enc.cob_curve)

        enc.lb_reduce = QLabel("Reduce:")
        enc.cob_reduce = QComboBox()
        enc.cob_reduce.addItem("Mean (all)", "mean")
        enc.cob_reduce.addItem("One", "one")
        enc.cob_reduce_val = QComboBox()
        enc.cob_reduce_val.setMinimumWidth(80)
        enc.cob_reduce_val.setEnabled(False)
        row.addWidget(enc.lb_reduce)
        row.addWidget(enc.cob_reduce)
        row.addWidget(enc.cob_reduce_val)
        row.addStretch(1)
        col.addLayout(row)

        enc.dim_bar = QWidget()
        enc.dim_layout = QHBoxLayout(enc.dim_bar)
        enc.dim_layout.setContentsMargins(4, 0, 4, 0)
        enc.dim_layout.setSpacing(6)
        col.addWidget(enc.dim_bar)

        dock.enc_ctrls = enc
        enc.cob_x_dim.currentIndexChanged.connect(
            lambda _=0, d=dock: self._on_dock_encoding_changed(d)
        )
        enc.cob_curve.currentIndexChanged.connect(
            lambda _=0, d=dock: self._on_dock_encoding_changed(d)
        )
        enc.cob_reduce.currentIndexChanged.connect(
            lambda _=0, d=dock: self._on_dock_encoding_changed(d)
        )
        enc.cob_reduce_val.currentIndexChanged.connect(
            lambda _=0, d=dock: self._on_dock_encoding_changed(d)
        )
        return panel

    def _populate_dock_encoding(self, dock):
        scan = dock.state.derived_scan
        enc = getattr(dock, "enc_ctrls", None)
        if scan is None or enc is None:
            return
        self.skip = True
        enc.cob_x_dim.clear()
        enc.cob_curve.clear()
        key_sets = scan.metadata.get("key_sets") or []
        if key_sets:
            for i, key in enumerate(key_sets):
                enc.cob_x_dim.addItem(key[-1], (CURVES_X_AXIS, i))
                enc.cob_curve.addItem(key[-1], i)
        else:
            enc.cob_x_dim.addItem("repetition", (CURVES_X_AXIS, 0))
            enc.cob_curve.addItem("repetition", 0)
        enc.cob_x_dim.addItem("ROI", (CURVES_X_ROI, None))
        enc.cob_x_dim.addItem("scan time", (CURVES_X_TIME, None))
        # Default X = curve axis index from dock.state.x_index
        xi = dock.state.x_index if dock.state.x_index is not None else 0
        if 0 <= xi < enc.cob_curve.count():
            enc.cob_curve.setCurrentIndex(xi)
        for i in range(enc.cob_x_dim.count()):
            data = enc.cob_x_dim.itemData(i)
            if data == (CURVES_X_AXIS, xi):
                enc.cob_x_dim.setCurrentIndex(i)
                break
        self.skip = False
        self._sync_dock_encoding_visibility(dock)
        self._rebuild_dock_dim_bar(dock)

    def _dock_x_selection(self, dock):
        enc = getattr(dock, "enc_ctrls", None)
        if enc is None or enc.cob_x_dim.count() == 0:
            return CURVES_X_AXIS, dock.state.x_index or 0
        data = enc.cob_x_dim.currentData()
        if isinstance(data, (tuple, list)) and len(data) == 2:
            return data[0], data[1]
        return CURVES_X_AXIS, 0

    def _dock_curve_axis_index(self, dock):
        enc = getattr(dock, "enc_ctrls", None)
        if enc is None or enc.cob_curve.count() == 0:
            return dock.state.x_index or 0
        data = enc.cob_curve.currentData()
        return int(data) if data is not None else 0

    def _sync_dock_encoding_visibility(self, dock):
        enc = getattr(dock, "enc_ctrls", None)
        if enc is None:
            return
        kind, axis_i = self._dock_x_selection(dock)
        show = kind in (CURVES_X_ROI, CURVES_X_TIME)
        for w in (enc.lb_curve, enc.cob_curve, enc.lb_reduce, enc.cob_reduce, enc.cob_reduce_val):
            w.setVisible(show)
        if kind == CURVES_X_AXIS and axis_i is not None:
            # Keep curve axis synced to X
            for i in range(enc.cob_curve.count()):
                if enc.cob_curve.itemData(i) == axis_i:
                    if enc.cob_curve.currentIndex() != i:
                        self.skip = True
                        enc.cob_curve.setCurrentIndex(i)
                        self.skip = False
                    break
        if show:
            self._sync_dock_reduce_widgets(dock)

    def _sync_dock_reduce_widgets(self, dock):
        enc = getattr(dock, "enc_ctrls", None)
        scan = dock.state.derived_scan
        if enc is None or scan is None:
            return
        i = self._dock_curve_axis_index(dock)
        st = dock.state.encoding_dim_state.setdefault(f"fit:{i}", {"mode": "mean", "value": None})
        self.skip = True
        mode = st.get("mode") or "mean"
        mi = enc.cob_reduce.findData(mode)
        enc.cob_reduce.setCurrentIndex(mi if mi >= 0 else 0)
        vals = self._unique_axis_values(scan, i)
        enc.cob_reduce_val.clear()
        for v in vals:
            enc.cob_reduce_val.addItem(f"{v:g}", v)
        if st.get("value") is not None and vals:
            nearest = min(vals, key=lambda v: abs(v - float(st["value"])))
            enc.cob_reduce_val.setCurrentIndex(vals.index(nearest))
            st["value"] = nearest
        elif vals:
            st["value"] = vals[0]
        enc.cob_reduce_val.setEnabled(mode == "one")
        self.skip = False

    def _rebuild_dock_dim_bar(self, dock):
        enc = getattr(dock, "enc_ctrls", None)
        scan = dock.state.derived_scan
        if enc is None or scan is None:
            return
        lay = enc.dim_layout
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        kind, x_axis = self._dock_x_selection(dock)
        curve_i = self._dock_curve_axis_index(dock)
        tip = QLabel("Dimensions:")
        tip.setStyleSheet(f"color: {THEME['muted']};")
        tip.setToolTip("Remaining scan axes: One (fixed) or All (overlay).")
        lay.addWidget(tip)
        key_sets = scan.metadata.get("key_sets") or []
        for i, key in enumerate(key_sets):
            if kind == CURVES_X_AXIS and i == x_axis:
                continue
            if kind in (CURVES_X_ROI, CURVES_X_TIME) and i == curve_i:
                continue
            dkey = f"axis:{i}"
            st = dock.state.encoding_dim_state.setdefault(dkey, {"mode": "one", "value": None})
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.addWidget(QLabel(f"{key[-1]}:"))
            mode = QComboBox()
            mode.addItem("One", "one")
            mode.addItem("All", "all")
            mode.setCurrentIndex(0 if st.get("mode") != "all" else 1)
            vals = self._unique_axis_values(scan, i)
            val_cob = QComboBox()
            for v in vals:
                val_cob.addItem(f"{v:g}", v)
            if st.get("value") is not None and vals:
                nearest = min(vals, key=lambda v: abs(v - float(st["value"])))
                val_cob.setCurrentIndex(vals.index(nearest))
                st["value"] = nearest
            elif vals:
                st["value"] = vals[0]
            val_cob.setEnabled(st.get("mode") != "all")

            def _on_mode(m, d=dock, k=dkey, vc=val_cob):
                d.state.encoding_dim_state[k]["mode"] = m
                vc.setEnabled(m == "one")
                self._redraw_derived_dock(d)

            def _on_val(_t, d=dock, k=dkey, vc=val_cob):
                d.state.encoding_dim_state[k]["value"] = vc.currentData()
                self._redraw_derived_dock(d)

            mode.currentIndexChanged.connect(lambda _=0, c=mode: _on_mode(c.currentData()))
            val_cob.currentIndexChanged.connect(_on_val)
            row.addWidget(mode)
            row.addWidget(val_cob)
            lay.addWidget(box)
        lay.addStretch(1)

    def _on_dock_encoding_changed(self, dock):
        if self.skip:
            return
        scan = dock.state.derived_scan
        if scan is None:
            return
        kind, axis_i = self._dock_x_selection(dock)
        curve_i = self._dock_curve_axis_index(dock)
        # Persist reduce mode into encoding_dim_state
        enc = dock.enc_ctrls
        st = dock.state.encoding_dim_state.setdefault(f"fit:{curve_i}", {"mode": "mean", "value": None})
        st["mode"] = enc.cob_reduce.currentData() or "mean"
        st["value"] = enc.cob_reduce_val.currentData()
        if kind == CURVES_X_AXIS and axis_i is not None:
            dock.state.x_index = axis_i
            self.build_plots_for_scan(scan, axis_i)
        else:
            dock.state.x_index = curve_i
            self.build_plots_for_scan(scan, curve_i)
        self._sync_dock_encoding_visibility(dock)
        self._rebuild_dock_dim_bar(dock)
        self._redraw_derived_dock(dock)

    def _dock_matching_slices(self, dock, scan):
        if scan.plots is None or not scan.plots.plots:
            return []
        kind, x_axis = self._dock_x_selection(dock)
        fit_along = scan.plots.x_index
        key_sets = scan.metadata.get("key_sets") or []
        key_axis_indices = [i for i in range(len(key_sets)) if i != fit_along]
        keys = list(scan.plots.plots.keys())
        out = []
        for si, key in enumerate(keys):
            ok = True
            for pos, ax in enumerate(key_axis_indices):
                if kind == CURVES_X_AXIS and ax == x_axis:
                    continue
                st = dock.state.encoding_dim_state.get(f"axis:{ax}", {"mode": "one"})
                if st.get("mode") == "all":
                    continue
                want = st.get("value")
                if want is None or pos >= len(key):
                    continue
                if float(key[pos]) != float(want):
                    ok = False
                    break
            if ok:
                out.append(si)
        return out if out else list(range(len(keys)))

    def _dock_fit_pick(self, dock):
        kind, _ = self._dock_x_selection(dock)
        if kind not in (CURVES_X_ROI, CURVES_X_TIME):
            return None
        i = self._dock_curve_axis_index(dock)
        st = dock.state.encoding_dim_state.get(f"fit:{i}", {"mode": "mean"})
        if st.get("mode") == "one":
            return st.get("value")
        return None

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
        # Seed encoding from Curves so Merge/Global starts with the same cube view
        dock.state.encoding_dim_state = {
            k: dict(v) for k, v in (self.curves_dim_state or {}).items()
        }
        kind_x, axis_x = self._curves_x_selection()
        self._sync_dock_ctrls_from_state(dock)
        self._populate_dock_encoding(dock)
        # Restore Curves X selection on the derived dock
        enc = getattr(dock, "enc_ctrls", None)
        if enc is not None:
            self.skip = True
            target = (kind_x, axis_x if kind_x == CURVES_X_AXIS else None)
            for i in range(enc.cob_x_dim.count()):
                if enc.cob_x_dim.itemData(i) == target:
                    enc.cob_x_dim.setCurrentIndex(i)
                    break
            self.skip = False
            self._sync_dock_encoding_visibility(dock)
            self._rebuild_dock_dim_bar(dock)
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
        if scan is None:
            return
        pw = dock.plot_widget
        if not isinstance(pw, PlotWidget):
            return
        # Ensure plots match curve axis
        curve_i = self._dock_curve_axis_index(dock) if getattr(dock, "enc_ctrls", None) else (dock.state.x_index or 0)
        self._ensure_scan_plots(scan, curve_i)
        if scan.plots is None:
            return

        kind, _ = self._dock_x_selection(dock) if getattr(dock, "enc_ctrls", None) else (CURVES_X_AXIS, curve_i)
        pw.clear()
        dock.state.series_items = []
        show_legend = dock.state.show_legend
        if show_legend:
            pw.addLegend()
        pw.setLabel("left", scan.metadata.get("function", ""), **font)
        n_rois = scan.plots.n_rois
        roi_idxs = list(range(n_rois))
        slices = self._dock_matching_slices(dock, scan) if getattr(dock, "enc_ctrls", None) else [min(dock.state.slice or 0, max(scan.plots.n_plots() - 1, 0))]
        x_pick = self._dock_fit_pick(dock) if getattr(dock, "enc_ctrls", None) else None

        if kind == CURVES_X_ROI:
            pw.setLabel("bottom", "ROI", **font)
            color_i = 0
            keys = list(scan.plots.plots.keys())
            for slice_index in slices:
                key = keys[slice_index] if slice_index < len(keys) else ()
                xs, ys, yerr = [], [], []
                for roi in roi_idxs:
                    yv, ye = self._collapse_curve_point(scan, slice_index, roi, x_pick=x_pick)
                    if not np.isfinite(yv):
                        continue
                    xs.append(float(roi))
                    ys.append(yv)
                    yerr.append(ye if np.isfinite(ye) else 0.0)
                if not xs:
                    continue
                name = None
                if show_legend:
                    name = f"slice[{','.join(f'{v:g}' for v in key)}]" if len(slices) > 1 and key else "data"
                self._add_errorbar_scatter(
                    pw.getPlotItem(),
                    np.asarray(xs), np.asarray(ys), np.asarray(yerr) * 2,
                    y_centers=np.asarray(ys),
                    style=self._series_style(color_i),
                    name=name,
                    view_style=dock.state.style,
                    show_errorbars=dock.state.show_errorbars,
                    series_out=dock.state.series_items,
                )
                color_i += 1
            return

        if kind == CURVES_X_TIME:
            # Single derived scan → one point on time axis is uninteresting; fall back to axis view
            kind = CURVES_X_AXIS

        # Axis mode (default)
        keys_meta = scan.metadata.get("key_sets") or []
        if keys_meta and 0 <= curve_i < len(keys_meta):
            pw.setLabel("bottom", keys_meta[curve_i][-1], **font)
        else:
            pw.setLabel("bottom", "repetition", **font)
        keys = list(scan.plots.plots.keys())
        color_i = 0
        for slice_index in slices:
            key = keys[slice_index] if slice_index < len(keys) else ()
            for roi in roi_idxs:
                x, y = scan.plots.get_data(slice_index, roi)
                if not scan.metadata["key_sets"]:
                    x = np.arange(len(y))
                name = f"ROI {roi}" if show_legend and n_rois > 1 else (f"ROI {roi}" if show_legend else None)
                if name and len(slices) > 1 and key:
                    name = f"{name}[{','.join(f'{v:g}' for v in key)}]"
                style = self._series_style(color_i)
                if "survival" in scan.metadata["function"].lower():
                    self.plot_survival(x, y, style=style, name=name, plot_widget=pw, view_state=dock.state)
                else:
                    self.plot_errorbars(x, y, style=style, name=name, plot_widget=pw, view_state=dock.state)
                fit, *_ = scan.plots.get_fit(slice_index, roi)
                if fit:
                    try:
                        pw.addItem(fit)
                    except Exception:
                        try:
                            fx, fy = fit.getData()
                            pw.plot(fx, fy, pen=mkPen(OVERLAY_COLORS[color_i % len(OVERLAY_COLORS)], width=2))
                        except Exception:
                            pass
                color_i += 1

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

    def batch_fit_selected(self, recipe=None, dock=None):
        """Reduce along Fit along; plot fit params vs outer dim (time / ROI / scan axis)."""
        if not isinstance(recipe, dict):
            recipe = None
        _ = dock
        # Always use Curves encoding (not focused-dock selected_* — Batch button
        # can leave focus on another dock with empty encoding → ROI collapses to [0]).
        scan_idxs = self._curves_scan_indices()
        roi_idxs = self._curves_roi_indices()
        if not scan_idxs:
            logging.error("No scans selected for batch fit")
            self.statusBar().showMessage("Batch fit: select scan(s) in Encoding", 4000)
            return
        if not roi_idxs:
            logging.error("No ROIs selected for batch fit")
            self.statusBar().showMessage("Batch fit: select ROI(s) in Encoding", 4000)
            return
        fitfunc = self._curves_fitfunc()
        if fitfunc not in self.fitter.func_dict:
            logging.error("Unknown fit function: %s", fitfunc)
            return
        fit_params = self.fitter.func_dict[fitfunc]["params"]
        labels = [self.scans[i].name for i in scan_idxs]
        fit_along = self._fit_along_index()
        probe = self.scans[scan_idxs[0]]
        outer_choices = self._outer_dimension_choices(
            fit_along, probe.metadata.get("key_sets") or []
        )

        outer_kind = BatchAnalyzeDialog.OUTER_TIME
        outer_axis = None
        if recipe is None:
            dialog = BatchAnalyzeDialog(
                fit_params, labels, outer_choices, parent=self
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            outer_kind, outer_axis = dialog.selected_outer()
            mode = dialog.selected_mode()
            plot_params = [p for p in fit_params if p in dialog.selected_params()]
            target_label_i = dialog.selected_scan_index()
            if not plot_params:
                return
        else:
            outer_kind = recipe.get("outer")
            outer_axis = recipe.get("outer_axis")
            mode = recipe.get("mode", BatchAnalyzeDialog.MODE_TIME)
            if outer_kind is None:
                if mode == BatchAnalyzeDialog.MODE_AXIS:
                    outer_kind = BatchAnalyzeDialog.OUTER_AXIS
                elif mode == BatchAnalyzeDialog.MODE_ROI:
                    outer_kind = BatchAnalyzeDialog.OUTER_ROI
                else:
                    outer_kind = BatchAnalyzeDialog.OUTER_TIME
            plot_params = recipe.get("params") or fit_params[:1]
            fitfunc = recipe.get("fitfunc", fitfunc)
            fit_params = self.fitter.func_dict[fitfunc]["params"]
            fit_along = int(recipe.get("x_index", fit_along))
            name = recipe.get("scan_name")
            target_label_i = 0
            if name:
                for j, lab in enumerate(labels):
                    if lab == name:
                        target_label_i = j
                        break
            roi_idxs = recipe.get("roi_sel", roi_idxs)

        if outer_kind in (BatchAnalyzeDialog.OUTER_AXIS, BatchAnalyzeDialog.OUTER_ROI) or mode in (
            BatchAnalyzeDialog.MODE_AXIS, BatchAnalyzeDialog.MODE_ROI,
        ):
            scan = self.scans[scan_idxs[target_label_i]]
            self._ensure_scan_plots(scan, fit_along)
            for param in plot_params:
                self._plot_param_scatter(
                    scan, fitfunc, param, mode,
                    outer_axis=outer_axis, rois=roi_idxs,
                    recipe_extra={"outer": outer_kind, "outer_axis": outer_axis},
                )
            return

        # --- outer = scan time ---
        slice_index = self.scrollbar.value() if recipe is None else recipe.get("slice", self.scrollbar.value())
        rows, failures = [], []
        for s_i in scan_idxs:
            scan = self.scans[s_i]
            self._ensure_scan_plots(scan, fit_along)
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
                try:
                    xp, yp, errors = self._series_xy_for_fit(scan, slice_index, roi)
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
                    # Do not append NaN rows — they produce All-NaN scatter warnings

        if failures:
            logging.warning(
                "Batch fit issues (%d skipped, %d ok):\n%s",
                len(failures),
                len(rows),
                "\n".join(failures[:40]) + ("\n…" if len(failures) > 40 else ""),
            )
        if not rows:
            logging.error("Batch fit produced no successful rows")
            self.statusBar().showMessage(
                "Batch fit: no curves with enough points (check ROI / Encoding)",
                6000,
            )
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
                    if not np.isfinite(row[param]):
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
                if not np.any(np.isfinite(ys)):
                    continue
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
            "outer": BatchAnalyzeDialog.OUTER_TIME,
            "outer_axis": None,
            "fitfunc": fitfunc,
            "params": plot_params,
            "scan_sel_names": [self.scans[i].name for i in scan_idxs],
            "roi_sel": roi_idxs,
            "slice": slice_index,
            "x_index": fit_along,
        }
        out = self.show_plot_dock(title, glw, kind="batch", recipe=rec)
        out.state.panel_series = panel_series
        self._record_tab_recipe(rec, dock=out)

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

    def _replay_tab_recipe(self, rec, restore_ui_context=True):
        """Replay an analysis recipe.

        restore_ui_context=True  → experiment load (restore frozen scans/ROI/slice/X)
        restore_ui_context=False → Refresh all (keep current Browser/Curves context)
        """
        rtype = rec.get("type")
        if restore_ui_context:
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
            self.two_d_plot(recipe=rec)
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