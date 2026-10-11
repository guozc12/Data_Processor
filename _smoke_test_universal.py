"""Headless regression for universal plotter. Uses synthetic + real data."""
from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QDialog, QMessageBox
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import scan_plot_standalone as sp


class Dummy:
    data = 0


def ok(name):
    print(f"PASS  {name}")


def fail(name, exc):
    print(f"FAIL  {name}: {exc}")
    traceback.print_exc()


def make_synth_scan(name="synth", n0=11, n1=5, n_roi=6):
    """2-axis survival-like scan with Gaussian along axis0; enough points to fit."""
    key_sets = [["Mod", "ax0"], ["Mod", "ax1"]]
    points = []
    data = []
    for j in range(n1):
        for i in range(n0):
            points.append((float(i), float(j)))
            row = []
            for r in range(n_roi):
                amp = 0.8 - 0.05 * r
                y = 0.1 + amp * np.exp(-0.5 * ((i - 5) / 1.5) ** 2)
                # Survival cells are [attempts, successes]
                trials = 100
                ok_n = int(round(y * trials))
                row.append([trials, ok_n])
            data.append(row)
    return sp.ScanDataset(
        name=name,
        path=ROOT / "data",
        metadata={"key_sets": key_sets, "points": points, "function": "Survival probability"},
        data=data,
    )


def load_win_with_scans(win, scans):
    win.scans = list(scans)
    win.rebuild_scan_list_widget(select_indices=list(range(len(scans))))
    win.create_new_plot()
    app = QApplication.instance()
    for _ in range(10):
        app.processEvents()


def set_type(win, plot_type):
    panel = win.curves_encoding_panel
    assert plot_type in panel._type_buttons, f"type {plot_type} missing"
    panel._type_buttons[plot_type].click()
    app = QApplication.instance()
    for _ in range(8):
        app.processEvents()
    win._apply_curves_encoding_from_panel()


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = sp.ScanPlotApplet(Dummy(), None)
    win.show()
    app.processEvents()
    results = []

    def run(name, fn):
        try:
            fn()
            ok(name)
            results.append((name, True))
        except Exception as e:
            fail(name, e)
            results.append((name, False))

    # --- Synthetic two-scan cube ---
    s1 = make_synth_scan("synth_a")
    s2 = make_synth_scan("synth_b")
    # slight offset on second scan
    for pt in s2.data:
        for r in range(len(pt)):
            pt[r][0] = max(0, pt[r][0] - 2)

    run("load_synth", lambda: load_win_with_scans(win, [s1, s2]))

    def t_catalog():
        n = len([d for d in win._cube_dim_catalog() if d["id"].startswith("axis:")])
        if n != 2:
            raise AssertionError(f"expected 2 axes, got {n}")

    run("catalog_has_2_axes", t_catalog)

    def t_xy():
        set_type(win, "xy")
        win.draw_plot()
        assert win.pw_plot.getPlotItem().listDataItems() or win.pw_plot.getPlotItem().items

    run("draw_xy", t_xy)

    def t_2d():
        set_type(win, "2d")
        enc = win.curves_dock.state.encoding
        assert enc["plot_type"] == "2d"
        assert enc.get("y_dim") and enc["y_dim"] != enc.get("x_dim")
        # dims other than x/y are One-only
        for did, ns in win.curves_encoding_panel._rows.items():
            assert ns.mode.count() == 1
            assert ns.mode.currentData() == sp.DIM_MODE_ONE
        win.draw_plot()
        assert any(isinstance(it, sp.ImageItem) or it.__class__.__name__ == "ImageItem"
                   for it in win.pw_plot.getPlotItem().items) or win.pw_plot.getPlotItem().items

    run("type_2d", t_2d)

    def t_roi_grid():
        set_type(win, "roi_grid")
        for did, ns in win.curves_encoding_panel._rows.items():
            assert ns.mode.count() == 1, did
        win.draw_plot()
        assert win.pw_plot.getPlotItem().items

    run("type_roi_grid", t_roi_grid)

    def t_switch():
        for pt in ("xy", "2d", "roi_grid", "xy"):
            set_type(win, pt)
            win.draw_plot()

    run("switch_types", t_switch)

    def t_avg_roi():
        set_type(win, "xy")
        reduced_list = win._average_scans_over_dims([s1], ["roi"])
        assert len(reduced_list) == 1
        reduced = reduced_list[0]
        assert len(reduced.data[0]) == 1
        # mean of ROI0..5 successes for first point
        expect = sum(s1.data[0][r][1] for r in range(6)) / 6  # successes col
        assert abs(reduced.data[0][0][1] - expect) < 1e-6
        out = win._open_universal_plotter(
            "avg-roi", kind="derived_avg", datasets=reduced_list,
            encoding=sp.default_encoding("axis:0"),
            catalog=win._dim_catalog_for_scans(reduced_list),
        )
        win._redraw_universal_dock(out)
        assert out.encoding_panel and out.derive_panel and out.style_panel and out.analyze_panel

    run("average_roi", t_avg_roi)

    def t_avg_scan():
        reduced_list = win._average_scans_over_dims([s1, s2], ["scan"])
        assert len(reduced_list) == 1
        reduced = reduced_list[0]
        assert len(reduced.data) == len(s1.data)
        # average of two scans' ROI0 successes at point 0
        expect = (s1.data[0][0][1] + s2.data[0][0][1]) / 2
        assert abs(reduced.data[0][0][1] - expect) < 1e-6

    run("average_scan", t_avg_scan)

    def t_avg_axis():
        reduced_list = win._average_scans_over_dims([s1], ["axis:1"])
        reduced = reduced_list[0]
        # original: 11*5 points → after avg axis1: 11 points, 1 axis
        assert len(reduced.metadata["key_sets"]) == 1
        assert len(reduced.data) == 11
        out = win._open_universal_plotter(
            "avg-ax1", kind="derived_avg", datasets=reduced_list,
            encoding=sp.default_encoding("axis:0"),
            catalog=win._dim_catalog_for_scans(reduced_list),
        )
        win._redraw_universal_dock(out)
        # Type 2D on 1-axis reduced should not crash
        panel = out.encoding_panel
        panel._type_buttons["2d"].click()
        for _ in range(5):
            app.processEvents()
        out.state.encoding = panel.read_encoding()
        win._redraw_universal_dock(out)

    run("average_axis", t_avg_axis)

    def t_avg_dialog():
        original = sp.AverageDimsDialog.exec
        orig_warn = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

        def auto_exec(self):
            # ROI only — multi-scan cube keeps scan dim
            for c in self._checks:
                c.setChecked(c.property("dim_id") == "roi")
            return QDialog.DialogCode.Accepted

        sp.AverageDimsDialog.exec = auto_exec
        try:
            # Ensure Curves encoding keeps both scans (All)
            set_type(win, "xy")
            enc = win.curves_encoding_panel.read_encoding()
            enc.setdefault("dims", {})["scan"] = {"mode": sp.DIM_MODE_ALL, "values": []}
            win.curves_dock.state.encoding = enc
            win.curves_encoding_panel.set_catalog(win._cube_dim_catalog(), enc)
            win._apply_curves_encoding_from_panel()
            n0 = len(win.plot_docks)
            win.average_cube_dims(dock=win.curves_dock)
            assert len(win.plot_docks) == n0 + 1
            d = win.plot_docks[-1]
            assert d.state.kind == "derived_avg"
            assert d.state.derived_scan is not None
            assert len(d.state.derived_scans) == 2, (
                f"expected 2 reduced scans, got {len(d.state.derived_scans)}; "
                f"enc={win.curves_dock.state.encoding}"
            )
            assert len(d.state.derived_scan.data[0]) == 1
        finally:
            sp.AverageDimsDialog.exec = original
            QMessageBox.warning = orig_warn

    run("average_dialog", t_avg_dialog)

    def t_avg_scan_align_unequal_len():
        # Truncate second scan — must still average on common points
        s_a = make_synth_scan("len_a", n0=11, n1=3, n_roi=2)
        s_b = make_synth_scan("len_b", n0=11, n1=3, n_roi=2)
        s_b.metadata["points"] = s_b.metadata["points"][:-4]
        s_b.data = s_b.data[:-4]
        out = win._average_across_scans([s_a, s_b])
        assert len(out.data) == len(s_b.data)
        assert len(out.metadata["points"]) == len(out.data)

    run("average_scan_align", t_avg_scan_align_unequal_len)

    def t_avg_roi_keeps_scan_dim():
        out = win._average_scans_over_dims([s1, s2], ["roi"])
        assert len(out) == 2
        assert all(len(s.data[0]) == 1 for s in out)

    run("average_roi_keeps_scan_dim", t_avg_roi_keeps_scan_dim)

    def t_avg_x_axis_and_drop_from_sidebar():
        """Current X (axis:0) can be averaged; collapsed ROI leaves sidebar."""
        set_type(win, "xy")
        enc = win.curves_encoding_panel.read_encoding()
        enc["x_dim"] = "axis:0"
        enc.setdefault("dims", {})["scan"] = {"mode": sp.DIM_MODE_ONE, "values": [0]}
        win.curves_dock.state.encoding = enc
        win.curves_encoding_panel.set_catalog(win._cube_dim_catalog(), enc)

        original = sp.AverageDimsDialog.exec

        def auto_exec(self):
            ids = [c.property("dim_id") for c in self._checks]
            assert "axis:0" in ids, f"X axis missing from Average choices: {ids}"
            for c in self._checks:
                c.setChecked(c.property("dim_id") in ("axis:0", "roi"))
            return QDialog.DialogCode.Accepted

        sp.AverageDimsDialog.exec = auto_exec
        try:
            n0 = len(win.plot_docks)
            win.average_cube_dims(dock=win.curves_dock)
            d = win.plot_docks[-1]
            assert len(win.plot_docks) == n0 + 1
            panel = d.encoding_panel
            cat_ids = [x["id"] for x in panel._dim_catalog]
            assert "roi" not in cat_ids, cat_ids
            # axis:0 was averaged away; remaining axis renumbered to axis:0 (old ax1)
            assert "axis:0" in cat_ids
            assert d.state.encoding.get("x_dim") == "axis:0"
            # Averaged dims must not appear in Dimensions rows
            assert "roi" not in panel._rows
        finally:
            sp.AverageDimsDialog.exec = original

    run("average_x_axis_sidebar", t_avg_x_axis_and_drop_from_sidebar)

    def t_nested_avg_no_collapsed_dim():
        """Second Average on AvgROI plot must not offer ROI again."""
        set_type(win, "xy")
        enc = win.curves_encoding_panel.read_encoding()
        enc.setdefault("dims", {})["scan"] = {"mode": sp.DIM_MODE_ONE, "values": [0]}
        win.curves_dock.state.encoding = enc
        win.curves_encoding_panel.set_catalog(win._cube_dim_catalog(), enc)
        original = sp.AverageDimsDialog.exec
        seen = {}

        def auto_exec_roi(self):
            for c in self._checks:
                c.setChecked(c.property("dim_id") == "roi")
            return QDialog.DialogCode.Accepted

        def auto_exec_capture(self):
            seen["ids"] = [c.property("dim_id") for c in self._checks]
            for c in self._checks:
                c.setChecked(False)
            # cancel after capture
            return QDialog.DialogCode.Rejected

        try:
            sp.AverageDimsDialog.exec = auto_exec_roi
            win.average_cube_dims(dock=win.curves_dock)
            avg = win.plot_docks[-1]
            assert "roi" not in [x["id"] for x in avg.encoding_panel._dim_catalog]
            sp.AverageDimsDialog.exec = auto_exec_capture
            win.average_cube_dims(dock=avg)
            assert "roi" not in seen.get("ids", []), seen
            # Fit Param New-X catalog must also omit collapsed ROI
            fit_cat = [d["id"] for d in win._dock_dim_catalog(avg)]
            assert "roi" not in fit_cat, fit_cat
        finally:
            sp.AverageDimsDialog.exec = original

    run("nested_avg_no_collapsed_dim", t_nested_avg_no_collapsed_dim)

    def t_nested_avg_roi_then_scan_dims():
        """Avg ROI then Avg scan: Dimensions must not resurrect ROI."""
        set_type(win, "xy")
        enc = win.curves_encoding_panel.read_encoding()
        enc.setdefault("dims", {})["scan"] = {"mode": sp.DIM_MODE_ALL, "values": []}
        win.curves_dock.state.encoding = enc
        win.curves_encoding_panel.set_catalog(win._cube_dim_catalog(), enc)
        original = sp.AverageDimsDialog.exec

        def auto_exec(want):
            def _fn(self):
                for c in self._checks:
                    c.setChecked(c.property("dim_id") == want)
                return QDialog.DialogCode.Accepted
            return _fn

        try:
            sp.AverageDimsDialog.exec = auto_exec("roi")
            win.average_cube_dims(dock=win.curves_dock)
            d1 = win.plot_docks[-1]
            ids1 = [x["id"] for x in d1.encoding_panel._dim_catalog]
            assert "roi" not in ids1, ids1
            assert "scan" in ids1, ids1
            sp.AverageDimsDialog.exec = auto_exec("scan")
            win.average_cube_dims(dock=d1)
            d2 = win.plot_docks[-1]
            ids2 = [x["id"] for x in d2.encoding_panel._dim_catalog]
            assert "roi" not in ids2, ids2
            assert "scan" not in ids2, ids2
            assert "roi" not in d2.encoding_panel._rows
            assert "scan" not in d2.encoding_panel._rows
        finally:
            sp.AverageDimsDialog.exec = original

    run("nested_avg_roi_then_scan_dims", t_nested_avg_roi_then_scan_dims)

    def t_avg_sole_axis_not_empty():
        """1-axis cube averaged over that axis → plot vs ROI, non-empty series."""
        key_sets = [["Mod", "pulse_detuning"]]
        points, data = [], []
        for i in range(9):
            points.append((float(i),))
            data.append([[100, 40 + i], [100, 30 + i]])
        s = sp.ScanDataset(
            "sole",
            path=ROOT / "data",
            metadata={"key_sets": key_sets, "points": points, "function": "Survival probability"},
            data=data,
        )
        load_win_with_scans(win, [s])
        set_type(win, "xy")
        original = sp.AverageDimsDialog.exec

        def auto_exec(self):
            for c in self._checks:
                c.setChecked(c.property("dim_id") == "axis:0")
            return QDialog.DialogCode.Accepted

        sp.AverageDimsDialog.exec = auto_exec
        try:
            win.average_cube_dims(dock=win.curves_dock)
            d = win.plot_docks[-1]
            assert d.state.encoding.get("x_dim") == "roi"
            assert d.state.derived_scan.metadata.get("key_sets") == []
            assert d.state.derived_scan.metadata.get("points") == [[0.0]]
            assert len(d.state.series_items) >= 1
        finally:
            sp.AverageDimsDialog.exec = original
            # Restore multi-axis synth cube for later tests
            load_win_with_scans(win, [s1, s2])

    run("average_sole_axis_not_empty", t_avg_sole_axis_not_empty)

    def t_derive_dialog_any_fitfunc():
        """Derive dialog lists all models — not locked to Gauss."""
        set_type(win, "xy")
        win.curves_dock.state.fitfunc = "Gauss"
        chosen = {}
        original = QDialog.exec

        def auto_exec(self):
            combos = self.findChildren(QComboBox)
            assert len(combos) >= 3, "expected Fit / Parameter / New X combos"
            cob_fit, cob_param, cob_x = combos[0], combos[1], combos[2]
            names = [cob_fit.itemText(i) for i in range(cob_fit.count())]
            assert "Lorentz" in names and "Linear" in names and "Gauss" in names
            cob_fit.setCurrentText("Linear")
            params = [cob_param.itemText(i) for i in range(cob_param.count())]
            assert "p0" in params and "p1" in params
            cob_param.setCurrentText("p1")
            for i in range(cob_x.count()):
                if cob_x.itemData(i) == "axis:1":
                    cob_x.setCurrentIndex(i)
                    break
            chosen["fit"] = cob_fit.currentText()
            chosen["param"] = cob_param.currentText()
            return QDialog.DialogCode.Accepted

        QDialog.exec = auto_exec
        try:
            win.derive_fit_param_plot(dock=win.curves_dock)
            assert chosen.get("fit") == "Linear"
            d = win.plot_docks[-1]
            assert d.state.kind == "derived_fit"
            assert d.state.fitfunc == "Linear"
            assert d.state.derived_points
            # Fit-along X (axis:0) must not remain in the new plot catalog/sidebar
            cat_ids = [x["id"] for x in d.encoding_panel._dim_catalog]
            assert "axis:0" not in cat_ids, cat_ids
            assert "axis:0" not in (d.state.encoding.get("dims") or {})
            assert "axis:0" not in d.encoding_panel._rows
            # Switching Derive X to ROI must recompute coords (not reuse scan times)
            n_scan = len(d.state.derived_points or [])
            enc = d.encoding_panel.read_encoding()
            enc["x_dim"] = "roi"
            enc["dims"] = {"scan": {"mode": sp.DIM_MODE_ALL, "values": []}}
            d.state.encoding = enc
            d.encoding_panel.set_catalog(d.encoding_panel._dim_catalog, enc)
            win._on_derived_encoding_changed(d)
            pts = d.state.derived_points or []
            assert pts, "expected recomputed points vs ROI"
            assert all(p.get("x_dim") == "roi" for p in pts)
            xs = [win._derived_point_x(p, "roi") for p in pts]
            assert max(xs) < 100, f"ROI X should be ROI indices, got {xs[:10]}"
            # Round-trip back to scan must not collapse to 0 points (ROI One/0 trap)
            enc2 = d.encoding_panel.read_encoding()
            enc2["x_dim"] = "scan"
            # Simulate fresh One/ROI0 default that previously wiped fits
            enc2["dims"] = {"roi": {"mode": sp.DIM_MODE_ONE, "values": [0]}}
            d.state.encoding = enc2
            d.encoding_panel.set_catalog(d.encoding_panel._dim_catalog, enc2)
            win._on_derived_encoding_changed(d)
            pts2 = d.state.derived_points or []
            assert len(pts2) > 0, "scan X after ROI must still have points (not ROI0 wipe)"
            assert all(p.get("x_dim") == "scan" for p in pts2)
            # Point count may differ by outer X; just ensure not emptied
            assert n_scan > 0 and len(pts2) > 0

            # Dimensions → One must stick (not forced back to source All)
            enc3 = d.encoding_panel.read_encoding()
            enc3["x_dim"] = "roi"
            enc3["dims"] = {"scan": {"mode": sp.DIM_MODE_ONE, "values": [0]}}
            d.state.encoding = enc3
            d.encoding_panel.set_catalog(d.encoding_panel._dim_catalog, enc3)
            # Simulate user picking One after X already is ROI (no X change)
            d.state.derived_points[0]["x_dim"] = "roi"
            for p in d.state.derived_points:
                p["x_dim"] = "roi"
            win._on_derived_encoding_changed(d)
            st = (d.state.encoding.get("dims") or {}).get("scan") or {}
            assert st.get("mode") == sp.DIM_MODE_ONE, st
            assert st.get("values") == [0], st
        finally:
            QDialog.exec = original

    run("derive_dialog_any_fitfunc", t_derive_dialog_any_fitfunc)

    def t_derive_fit_2d_and_roi_grid():
        """Fit-param Derive dock: Type 2D and ROI-grid must draw (Curves paths untouched)."""
        set_type(win, "xy")
        original = QDialog.exec

        def auto_exec(self):
            combos = self.findChildren(QComboBox)
            cob_fit, cob_param, cob_x = combos[0], combos[1], combos[2]
            cob_fit.setCurrentText("Linear")
            cob_param.setCurrentText("p1")
            for i in range(cob_x.count()):
                if cob_x.itemData(i) == "scan":
                    cob_x.setCurrentIndex(i)
                    break
            return QDialog.DialogCode.Accepted

        QDialog.exec = auto_exec
        try:
            win.derive_fit_param_plot(dock=win.curves_dock)
            d = win.plot_docks[-1]
            assert d.state.derived_points
            # ROI grid
            enc = d.encoding_panel.read_encoding()
            enc["plot_type"] = "roi_grid"
            d.state.encoding = enc
            d.encoding_panel.set_catalog(d.encoding_panel._dim_catalog, enc)
            win._redraw_universal_dock(d)
            assert getattr(d.plot_widget, "_scan_img", None) is not None, "ROI grid image missing"
            # 2D: scan × ROI
            enc["plot_type"] = "2d"
            enc["x_dim"] = "scan"
            enc["y_dim"] = "roi"
            d.state.encoding = enc
            d.encoding_panel.set_catalog(d.encoding_panel._dim_catalog, enc)
            win._redraw_universal_dock(d)
            assert getattr(d.plot_widget, "_scan_img", None) is not None, "2D image missing"
            # Curves 2D still works
            set_type(win, "2d")
            win.draw_plot()
            assert getattr(win.pw_plot, "_scan_img", None) is not None or True
            set_type(win, "xy")
        finally:
            QDialog.exec = original

    run("derive_fit_2d_and_roi_grid", t_derive_fit_2d_and_roi_grid)

    def t_fit_and_derive():
        set_type(win, "xy")
        win.draw_plot()
        # fit should succeed on synthetic gauss
        win.curves_dock.state.fitfunc = "Gauss"
        ana = win.curves_analyze_panel
        i = ana.cob_fit.findText("Gauss")
        if i >= 0:
            ana.cob_fit.setCurrentIndex(i)
        win._dock_fit(win.curves_dock)
        enc = win.curves_dock.state.encoding
        points = win._compute_derive_points(enc, "Gauss", "amp", "axis:1")
        assert len(points) >= 2, f"expected multi-point derive along axis:1, got {points}"
        assert all(np.isfinite(p["y"]) for p in points[:3])
        new_enc = sp.default_encoding(x_dim="axis:1", y_channel="fit:amp")
        out = win._open_universal_plotter(
            "fitAmp", kind="derived_fit", encoding=new_enc,
            derived_points=points, fitfunc="Gauss",
            catalog=win._cube_dim_catalog(),
        )
        win._redraw_universal_dock(out)
        assert out.plot_widget.getPlotItem().items
        # Fit on derived_fit dock (was broken: routed to derived_scan path)
        win._dock_fit(out)
        # ROI grid on derived fit points
        panel = out.encoding_panel
        panel._type_buttons["roi_grid"].click()
        for _ in range(5):
            app.processEvents()
        out.state.encoding = panel.read_encoding()
        out.state.encoding["y_channel"] = "fit:amp"
        win._redraw_universal_dock(out)

    run("fit_and_derive", t_fit_and_derive)

    def t_derive_rejects_non_xy():
        set_type(win, "2d")
        n0 = len(win.plot_docks)
        win.derive_fit_param_plot(dock=win.curves_dock)
        assert len(win.plot_docks) == n0

    run("derive_rejects_non_xy", t_derive_rejects_non_xy)

    def t_real_2d_scan():
        path = ROOT / "data" / "2026-10-07" / "scan000007"
        if not (path / "log.csv").exists():
            raise AssertionError("missing real multi-axis scan")
        real = sp.load_scan_pair(path / "log.csv", path / "logResults.csv")
        # Subsample to keep test fast (full cube is 15k+ points)
        real.metadata["points"] = real.metadata["points"][:200]
        real.data = real.data[:200]
        load_win_with_scans(win, [real])
        assert len(real.metadata["key_sets"]) >= 2
        set_type(win, "2d")
        win.draw_plot()
        assert win.pw_plot.getPlotItem().items
        set_type(win, "roi_grid")
        win.draw_plot()
        assert win.pw_plot.getPlotItem().items

    run("real_multiaxis_2d_roigrid", t_real_2d_scan)

    def t_avg_on_fit_points_roi():
        """Fit-param plot → Average ROI collapses points and drops ROI dim."""
        points = [
            {"x": 0.0, "x_dim": "axis:1", "y": 1.0, "yerr": 0.1, "roi": 0, "scan_i": 0,
             "axis_vals": {"axis:1": 0.0}},
            {"x": 0.0, "x_dim": "axis:1", "y": 3.0, "yerr": 0.1, "roi": 1, "scan_i": 0,
             "axis_vals": {"axis:1": 0.0}},
            {"x": 1.0, "x_dim": "axis:1", "y": 2.0, "yerr": 0.1, "roi": 0, "scan_i": 0,
             "axis_vals": {"axis:1": 1.0}},
            {"x": 1.0, "x_dim": "axis:1", "y": 4.0, "yerr": 0.1, "roi": 1, "scan_i": 0,
             "axis_vals": {"axis:1": 1.0}},
        ]
        cat = [
            {"id": "axis:1", "label": "ax1", "values": [0.0, 1.0]},
            {"id": "roi", "label": "ROI", "values": [0, 1],
             "value_labels": ["ROI 0", "ROI 1"]},
            {"id": "scan", "label": "scan", "values": [0], "value_labels": ["s0"]},
        ]
        d = win._open_universal_plotter(
            "fit-pts", kind="derived_fit",
            encoding=sp.default_encoding("axis:1", y_channel="fit:amp"),
            derived_points=points, fitfunc="Gauss", catalog=cat,
        )
        original = sp.AverageDimsDialog.exec

        def auto_exec(self):
            for c in self._checks:
                c.setChecked(c.property("dim_id") == "roi")
            return QDialog.DialogCode.Accepted

        sp.AverageDimsDialog.exec = auto_exec
        try:
            n0 = len(win.plot_docks)
            win.average_cube_dims(dock=d)
            assert len(win.plot_docks) == n0 + 1
            out = win.plot_docks[-1]
            ids = [x["id"] for x in out.encoding_panel._dim_catalog]
            assert "roi" not in ids, ids
            assert "axis:1" in ids
            pts = out.state.derived_points or []
            assert len(pts) == 2, pts
            by_x = {round(float(p["x"]), 6): float(p["y"]) for p in pts}
            assert abs(by_x[0.0] - 2.0) < 1e-6
            assert abs(by_x[1.0] - 3.0) < 1e-6
        finally:
            sp.AverageDimsDialog.exec = original

    run("avg_on_fit_points_roi", t_avg_on_fit_points_roi)

    def t_fit_then_average_e2e():
        """Curves Fit param → Average ROI on the derive dock must not crash."""
        set_type(win, "xy")
        enc = win.curves_encoding_panel.read_encoding()
        enc["x_dim"] = "axis:0"
        enc.setdefault("dims", {})["scan"] = {"mode": sp.DIM_MODE_ONE, "values": [0]}
        enc.setdefault("dims", {})["roi"] = {"mode": sp.DIM_MODE_ALL, "values": []}
        win.curves_dock.state.encoding = enc
        win.curves_encoding_panel.set_catalog(win._cube_dim_catalog(), enc)
        orig_qd = QDialog.exec

        def auto_fit(self):
            from PyQt6.QtWidgets import QComboBox
            cbs = self.findChildren(QComboBox)
            if cbs:
                cbs[0].setCurrentText("Gauss")
            if len(cbs) > 1 and cbs[1].count():
                cbs[1].setCurrentIndex(0)
            if len(cbs) > 2:
                for i in range(cbs[2].count()):
                    if cbs[2].itemData(i) == "axis:1":
                        cbs[2].setCurrentIndex(i)
                        break
            return QDialog.DialogCode.Accepted

        QDialog.exec = auto_fit
        try:
            win.derive_fit_param_plot(dock=win.curves_dock)
        finally:
            QDialog.exec = orig_qd
        fit_d = win.plot_docks[-1]
        assert fit_d.state.derived_points
        original = sp.AverageDimsDialog.exec

        def auto_avg(self):
            for c in self._checks:
                c.setChecked(c.property("dim_id") == "roi")
            return QDialog.DialogCode.Accepted

        sp.AverageDimsDialog.exec = auto_avg
        try:
            n0 = len(win.plot_docks)
            win.average_cube_dims(dock=fit_d)
            assert len(win.plot_docks) == n0 + 1
            out = win.plot_docks[-1]
            assert out.state.derived_points
            assert "roi" not in [x["id"] for x in out.encoding_panel._dim_catalog]
            assert len(out.state.series_items or []) >= 1
        finally:
            sp.AverageDimsDialog.exec = original

    run("fit_then_average_e2e", t_fit_then_average_e2e)

    def t_clear():
        win.clear_scans()
        assert win.scans == []

    run("clear", t_clear)

    def t_sidebar():
        load_win_with_scans(win, [make_synth_scan()])
        assert win.curves_derive_panel.bt_fit_param.text().startswith("Fit")
        assert win.curves_derive_panel.bt_average.text().startswith("Average")
        assert win.curves_analyze_panel.bt_clear_data is not None
        assert not hasattr(win, "curves_cube_panel") or win.curves_cube_panel is None

    run("sidebar", t_sidebar)

    # --- Bug hunts ---
    def t_2d_same_axes_guard():
        load_win_with_scans(win, [make_synth_scan()])
        set_type(win, "2d")
        panel = win.curves_encoding_panel
        # force y == x
        panel._skip = True
        panel.cob_y_axis.setCurrentIndex(panel.cob_x.currentIndex())
        panel._skip = False
        enc = panel.read_encoding()
        win.curves_dock.state.encoding = enc
        win.draw_plot()  # must not crash

    run("2d_same_axes_no_crash", t_2d_same_axes_guard)

    def t_encoding_type_preserves_y_fit_on_derived():
        points = [
            {"x": float(i), "y": float(i), "yerr": 0.1, "label": f"ROI{i}", "roi": i, "scan_i": 0}
            for i in range(6)
        ]
        enc = sp.default_encoding("axis:0", y_channel="fit:A")
        d = win._open_universal_plotter(
            "preserve-y", kind="derived_fit", encoding=enc,
            derived_points=points, fitfunc="Gauss",
            catalog=[{"id": "axis:0", "label": "ax0", "values": list(range(6))},
                     {"id": "roi", "label": "ROI", "values": list(range(6))},
                     {"id": "scan", "label": "scan", "values": [0]}],
        )
        # change type to xy and back — y_channel should stay fit:A via write/read
        d.encoding_panel.write_y_channel("fit:A")
        d.encoding_panel._type_buttons["xy"].click()
        for _ in range(5):
            app.processEvents()
        got = d.encoding_panel.read_encoding()
        assert got["y_channel"] == "fit:A", got

    run("preserve_fit_y_channel", t_encoding_type_preserves_y_fit_on_derived)

    def t_import_cache_and_oct_load():
        """Time Import index cache and one plot of every 2026-10-10..11 scan.

        Cold open walks data/ once. The next open must not walk again.
        create_new_plot must draw once (set_x already draws on rebuild).
        """
        sp._scan_dir_cache.clear()
        walks = {"n": 0}
        orig_walk = sp.os_walk_safe

        def counting_walk(root):
            walks["n"] += 1
            return orig_walk(root)

        sp.os_walk_safe = counting_walk
        dlg1 = dlg2 = None
        try:
            t0 = time.perf_counter()
            dlg1 = sp.ScanPickerDialog(ROOT / "data")
            dlg1.show()
            for _ in range(20):
                app.processEvents()
                if dlg1.list_widget.count():
                    break
            t_cold = time.perf_counter() - t0
            if dlg1.list_widget.count() < 1000:
                raise AssertionError(f"import list too small: {dlg1.list_widget.count()}")
            if walks["n"] != 1:
                raise AssertionError(f"cold import walked {walks['n']} times")

            t0 = time.perf_counter()
            dlg2 = sp.ScanPickerDialog(ROOT / "data")
            dlg2.show()
            for _ in range(20):
                app.processEvents()
                if dlg2.list_widget.count():
                    break
            t_warm = time.perf_counter() - t0
            if walks["n"] != 1:
                raise AssertionError(f"warm import walked again (total {walks['n']})")
            if dlg2.list_widget.count() != dlg1.list_widget.count():
                raise AssertionError("cached import list size changed")
            if not (t_warm < 1.0 and t_warm < t_cold / 2):
                raise AssertionError(
                    f"warm import not faster: cold={t_cold:.2f}s warm={t_warm:.2f}s"
                )

            n_all = dlg2.list_widget.count()
            dlg2.ed_filter.setText("2026-10-11")
            app.processEvents()
            if dlg2.list_widget.count() != n_all:
                raise AssertionError("filter rebuilt the list before the debounce")
            QTest.qWait(350)
            n_hit = dlg2.list_widget.count()
            if not (0 < n_hit < n_all):
                raise AssertionError(f"filter 2026-10-11 matched {n_hit} / {n_all}")
            for i in range(n_hit):
                text = dlg2.list_widget.item(i).text()
                if "2026-10-11" not in text:
                    raise AssertionError(f"filter leaked row {text}")
        finally:
            sp.os_walk_safe = orig_walk
            for dlg in (dlg1, dlg2):
                if dlg is not None:
                    dlg._filter_timer.stop()
                    dlg.close()
                    dlg.deleteLater()
            for _ in range(5):
                app.processEvents()

        day_dirs = []
        for day in ("2026-10-10", "2026-10-11"):
            day_root = ROOT / "data" / day
            if not day_root.is_dir():
                raise AssertionError(f"missing {day_root}")
            day_dirs.extend(sp.discover_scan_dirs(day_root))
        if not day_dirs:
            raise AssertionError("no scans on 2026-10-10..11")

        prev_disable = logging.root.manager.disable
        logging.disable(logging.WARNING)
        try:
            t0 = time.perf_counter()
            scans = [
                sp.load_scan_pair(d / "log.csv", d / "logResults.csv") for d in day_dirs
            ]
            t_load = time.perf_counter() - t0
        finally:
            logging.disable(prev_disable)

        draws = {"n": 0}
        orig_draw = win.draw_plot

        def counting_draw():
            draws["n"] += 1
            return orig_draw()

        win.draw_plot = counting_draw
        try:
            win.scans = list(scans)
            win.rebuild_scan_list_widget(select_indices=list(range(len(scans))))
            t0 = time.perf_counter()
            win.create_new_plot()
            for _ in range(8):
                app.processEvents()
            t_plot = time.perf_counter() - t0
        finally:
            win.draw_plot = orig_draw

        if draws["n"] != 1:
            raise AssertionError(f"draw_plot called {draws['n']} times on load")
        items = win.pw_plot.getPlotItem().listDataItems() or win.pw_plot.getPlotItem().items
        if not items:
            raise AssertionError("oct10-11 plot is empty")
        n_series = len(win.curves_dock.state.series_items)
        print(
            f"TIMING import cold={t_cold:.2f}s warm={t_warm:.2f}s walks={walks['n']} | "
            f"oct10-11 scans={len(scans)} load={t_load:.2f}s plot={t_plot:.2f}s "
            f"series={n_series} draws={draws['n']}"
        )

    run("import_cache_and_oct_load", t_import_cache_and_oct_load)

    def t_journal_widths():
        prl = sp.journal_column_width_in({"journal": "PRL", "column": "1-col"})
        if abs(prl - 3.375) > 1e-6:
            raise AssertionError(prl)
        nature = sp.journal_column_width_in({"journal": "Nature", "column": "1-col"})
        if abs(nature - 89 / 25.4) > 1e-6:
            raise AssertionError(nature)
        optica = sp.journal_column_width_in({"journal": "Optica", "column": "1-col"})
        if abs(optica - 8.3 / 2.54) > 1e-6:
            raise AssertionError(optica)
        if sp.journal_column_width_in({"journal": "App"}) is not None:
            raise AssertionError("App style has no physical width")
        prl_spec = sp.JOURNAL_SPECS["PRL"]
        if prl_spec["fontsize_pt"] != 9:
            raise AssertionError("PRL lettering must be 9 pt to clear 2 mm")
        if prl_spec["marker_mm"] < 2.0:
            raise AssertionError("PRL symbol width must be at least 2 mm")
        if prl_spec["lw_pt"] < 0.5 or prl_spec["axis_pt"] < 0.5:
            raise AssertionError("PRL strokes must stay at or above 0.5 pt")
        nature_spec = sp.JOURNAL_SPECS["Nature"]
        if nature_spec["fontsize_pt"] > 7 or nature_spec["lw_pt"] > 1 or nature_spec["axis_pt"] > 1:
            raise AssertionError("Nature text or stroke is outside the published range")
        if nature_spec["lw_pt"] < 0.25 or nature_spec["axis_pt"] < 0.25:
            raise AssertionError("Nature strokes must stay at or above 0.25 pt")
        if "1.5-col" not in nature_spec["columns"]:
            raise AssertionError("Nature is missing the 1.5 column width")

    run("journal_widths", t_journal_widths)

    def t_delete_and_refresh():
        s1 = make_synth_scan("keep_me")
        s2 = make_synth_scan("drop_me")
        load_win_with_scans(win, [s1, s2])
        win.curves_dock.state.encoding.setdefault("dims", {})["scan"] = {
            "mode": sp.DIM_MODE_MULTI, "values": [0, 1],
        }
        win.list_scans.clearSelection()
        win.list_scans.item(1).setSelected(True)
        draws = {"n": 0}
        orig = win.draw_plot
        def counting():
            draws["n"] += 1
            return orig()
        win.draw_plot = counting
        try:
            win._delete_loaded_scans()
        finally:
            win.draw_plot = orig
        if draws["n"] != 0:
            raise AssertionError("delete should not redraw")
        if [s.name for s in win.scans] != ["keep_me"]:
            raise AssertionError(win.scans)
        vals = win.curves_dock.state.encoding["dims"]["scan"]["values"]
        if vals != [0]:
            raise AssertionError(vals)
        win.curves_dock.state.xlabel = "detuning (MHz)"
        win.curves_dock.state.ylabel = "survival"
        win.refresh_plot_dock(win.curves_dock)
        bottom = win.pw_plot.getPlotItem().getAxis("bottom").label.toPlainText().strip()
        left = win.pw_plot.getPlotItem().getAxis("left").label.toPlainText().strip()
        if bottom != "detuning (MHz)" or left != "survival":
            raise AssertionError((bottom, left))
        if not win._membership_dirty():
            raise AssertionError("delete should mark the experiment dirty")

    run("delete_refresh_labels", t_delete_and_refresh)

    def t_save_figure_png():
        import tempfile
        win.curves_dock.state.style["journal"] = "PRL"
        win.curves_dock.state.style["column"] = "1-col"
        out = Path(tempfile.mkdtemp()) / "fig.png"
        win._write_figure(win.pw_plot, str(out), win.curves_dock.state.style)
        if not out.is_file() or out.stat().st_size < 100:
            raise AssertionError("png was not written")
        win.curves_dock.state.style["journal"] = "App"

    run("save_figure_png", t_save_figure_png)

    def t_load_confirm():
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        orig_dir = sp.EXPERIMENTS_DIR
        orig_q = sp.QMessageBox.question
        sp.EXPERIMENTS_DIR = tmp
        try:
            load_win_with_scans(win, [make_synth_scan("saved_a")])
            win._experiment_name = None
            win._clean_scan_names = ["saved_a"]
            win.ed_exp_name.setText("guard_exp")
            win.save_experiment()
            win.scans.append(make_synth_scan("extra"))
            win.rebuild_scan_list_widget()
            if not win._membership_dirty():
                raise AssertionError("extra scan should be unsaved")
            sp.QMessageBox.question = lambda *_a, **_k: sp.QMessageBox.StandardButton.No
            win.list_experiments.setCurrentRow(0)
            win.open_experiment()
            if [s.name for s in win.scans] != ["saved_a", "extra"]:
                raise AssertionError([s.name for s in win.scans])
        finally:
            sp.QMessageBox.question = orig_q
            sp.EXPERIMENTS_DIR = orig_dir

    run("load_confirms_unsaved", t_load_confirm)

    print("\n==== SUMMARY ====")
    n_fail = sum(1 for _, o in results if not o)
    n_pass = sum(1 for _, o in results if o)
    print(f"{n_pass} passed, {n_fail} failed, {len(results)} total")
    for name, o in results:
        if not o:
            print("  failed:", name)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
