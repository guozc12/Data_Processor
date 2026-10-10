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

from PyQt6.QtWidgets import QMainWindow, QDialog, QFileDialog, QApplication
from PyQt6.QtGui import QFont
from PyQt6.QtCore import Qt
from pyqtgraph import ErrorBarItem

import numpy as np
from scipy.stats import sem
from statsmodels.stats.proportion import proportion_confint
from scipy.cluster.hierarchy import fcluster, linkage

import logging
import io
import win32clipboard
from PIL import Image

# from artiq.applets.simple import SimpleApplet
# from sipyco.pc_rpc import Client

import matplotlib.pyplot as plt

FOLDER = "R:/Strontium/Images" #default folder to look for csv files
style_sheet = {"pen": None, "symbol": 'o', "symbolPen": None, "symbolBrush": (31,120,180)}
font = {'font-size': '18px', 'color': '#C8C8C8'}

from numpy import array

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

from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtGui import QIntValidator
class HeatmapDialog(QDialog):
    def __init__(self, fit_options, n_rois, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Heatmap")
        self.setModal(True)

        main_layout = QVBoxLayout()

        # --- First row: Fit parameter + N_rois ---
        top_layout = QHBoxLayout()

        top_layout.addWidget(QLabel("Fit parameter:"))
        self.cob_fit = QComboBox()
        self.cob_fit.addItems(fit_options)
        top_layout.addWidget(self.cob_fit)

        top_layout.addWidget(QLabel("N rois:"))
        self.n_rois_label = QLabel(str(n_rois))
        top_layout.addWidget(self.n_rois_label)

        main_layout.addLayout(top_layout)

        # --- Second row: Rows / Cols ---
        grid_layout = QHBoxLayout()

        int_validator = QIntValidator(0, 10**9, self)

        grid_layout.addWidget(QLabel("Rows:"))
        self.rows_edit = QLineEdit()
        self.rows_edit.setValidator(int_validator)
        self.rows_edit.setText(str(int(n_rois**0.5)))
        grid_layout.addWidget(self.rows_edit)

        grid_layout.addWidget(QLabel("Cols:"))
        self.cols_edit = QLineEdit()
        self.cols_edit.setValidator(int_validator)
        self.cols_edit.setText(str(int(n_rois**0.5)))
        grid_layout.addWidget(self.cols_edit)

        main_layout.addLayout(grid_layout)

        # --- Buttons ---
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
        x = np.array(x)
        y = np.array(y)
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
        
        #execute fit
        if len(p0) == 0:
            popt, perr = [], []
        else:
            try:
                popt, pcov = curve_fit(func, x, y, p0, sigma=yerr)
            except RuntimeError:
                popt, pcov = curve_fit(func, x, y, [1]*len(p0), sigma=yerr)
            perr = np.sqrt(np.diag(pcov))

        # insert fixed parameters back
        if fixed is not None:
            for i in range(len(fixed)):
                if fixed[i]:
                    popt = np.insert(popt, i, guess[i])
                    perr = np.insert(perr, i, 0)
        
        xi = np.linspace(x.min(), x.max(), 200)
        yi = self.func_dict[fitfunc]['f'](xi, *popt)
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
        self.lb_fit.setToolTip(_translate("MainWindow", "Fit results"))
        self.lb_x.setText(_translate("MainWindow", "X - Axis"))
        self.sb_roi.setToolTip(_translate("MainWindow", "Data of ROI to be displayed"))
        self.lb_roi.setText(_translate("MainWindow", "ROI"))
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

        #prevent applet from killing itself when docked
        app = QApplication.instance()
        app.setQuitOnLastWindowClosed(False)

        self.data_name = args.data
        self.setupUi(self)
        self.skip = True
        self.cob_x.currentIndexChanged.connect(self.set_x)
        self.cob_action.currentIndexChanged.connect(self.perform_action)
        self.scrollbar.valueChanged.connect(self.change_plot)
        self.sb_roi.valueChanged.connect(self.draw_plot)
        self.bt_fit.clicked.connect(self.perform_fit)
        self.bt_clear.clicked.connect(self.clear_fit)
        self.bt_export.clicked.connect(self.export_plot)
        self.bt_import.clicked.connect(self.import_data)
        self.bt_global.clicked.connect(self.globalify)
        self.bt_fit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bt_fit.customContextMenuRequested.connect(self.advanced_fit)

        f = QFont("Arial", 12)
        f.setBold(True)
        self.pw_plot.getAxis("left").setStyle(tickFont=f)
        self.pw_plot.getAxis("bottom").setStyle(tickFont=f)
        self.pw_plot.showAxis('top', show=True)
        self.pw_plot.showAxis('right', show=True)
        self.pw_plot.getAxis('top').setStyle(showValues=False)
        self.pw_plot.getAxis('right').setStyle(showValues=False)
        self.pw_plot.getAxis('right').setWidth(20)

        self.metadata = {"key_sets": [], "points": [], "function": ""}
        self.data = []
        self.fitter = Fitter()
        self.n_rois = 1
        self.cob_fit.addItems(self.fitter.func_dict.keys())
    
        # self.req = req
        # self.remote_dataset = Client("127.0.0.1", 3251, "dataset_db")

    def closeEvent(self, event):
        #mainly just prevents a warning in artiq when closing applet
        QApplication.instance().quit()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            screenshot = self.pw_plot.grab()
            clipboard = QApplication.clipboard()
            clipboard.setPixmap(screenshot)
        if event.key() == Qt.Key.Key_A and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            x, y = self.get_current_data()
            self.copy_to_clipboard(x=x, y=y)
        if event.key() == Qt.Key.Key_Q and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.two_d_plot()

    def perform_action(self, index):
        match index:
            case 0:
                return
            case 1: #copy image
                screenshot = self.pw_plot.grab()
                clipboard = QApplication.clipboard()
                clipboard.setPixmap(screenshot)
            case 2: #copy raw data
                x, y = self.get_current_data()
                self.copy_to_clipboard(x=x, y=y)
            case 3: #2d plot
                self.two_d_plot()
            case 4: #plot fit parameter
                self.parameter_plot()
            case 5: #generate heat map
                self.generate_heatmap()

        self.cob_action.setCurrentIndex(0)

    def add_all_points(self):
        for i, result in enumerate(self.data):
            self.plots.add_point(self.metadata["points"][i], result)

    def get_plot_indices(self):
        return self.scrollbar.value(), self.sb_roi.value()

    def get_current_data(self):
        return self.plots.get_data(*self.get_plot_indices())
        
    def set_x(self, index):
        if self.skip:
            return
        
        self.plots = Plots(index)
        
        func = self.metadata["function"]
        if func != "Global survival":
            self.plots.set_n_rois(len(self.data[0]))

        for point in self.metadata["points"]:
            self.plots.add_plot(point)
            self.plots.resort_keys()
        
        self.scrollbar.setRange(0, self.plots.n_plots()-1)

        self.pw_plot.setLabel("left", func, **font)
        if self.metadata["key_sets"]:
            self.pw_plot.setLabel("bottom", self.metadata["key_sets"][index][-1], **font)
        else:
            self.pw_plot.setLabel("bottom", "repetition", **font)

        self.add_all_points()
        self.draw_plot()
        self.set_title()

    def change_plot(self):
        self.draw_plot()
        self.set_title()
          
    def draw_plot(self):
        if self.skip:
            return
        self.pw_plot.clear()
        
        x, y = self.get_current_data()
        if not self.metadata["key_sets"]:
            x = np.arange(len(y))

        if "survival" in self.metadata["function"].lower():
            self.plot_survival(x, y)
        else:
            self.plot_errorbars(x, y)

        fit, params, popt, perr = self.plots.get_fit(*self.get_plot_indices())
        if fit:
            self.pw_plot.addItem(fit)
            self.write_fit_params(params, popt, perr)
        else:
            self.lb_fit.setText("")
    
    def plot_survival(self, x, y):
        
        unique_x, means, err = self.get_survival_rate(x,y)
        if np.nansum(err) != 0:
            lower = err[0]
            upper = err[1]
            idx = (lower > 0) | (upper > 0)

            heights = lower[idx] + upper[idx]
            y_centers = means[idx] + (upper[idx] - lower[idx]) / 2

            error_bars = ErrorBarItem(x=unique_x[idx], y=y_centers, height=heights, beam=0.007*(unique_x.max()-unique_x.min()))
            self.pw_plot.addItem(error_bars)

        self.pw_plot.plot(unique_x, means, **style_sheet)

    def plot_errorbars(self, x, y):
        unique_x, means, err = self.get_errorbars(x, y)
        
        if err.sum() != 0:
            idx = err > 0
            error_bars = ErrorBarItem(x=unique_x[idx], y=means[idx], height=err[idx]*2, beam=0.007*(unique_x.max()-unique_x.min()))
            self.pw_plot.addItem(error_bars)
        self.pw_plot.plot(unique_x, means, **style_sheet)
    
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
        if len(self.data[0]) < 2:
            logging.error("Data is already global")
            return
        if self.metadata["function"] == "Survival probability":
            self.metadata["function"] = "Global survival"
        else:
            self.metadata["function"] = "Global " + self.metadata["function"]
        data = []
        for point in self.data:
            if isinstance(data, list):
                data.append([[sum(x[0] for x in point), sum(x[1] for x in point)]])
            else:
                data.append([sum(point)])
        self.data = data
        self.create_new_plot(metadata=self.metadata)

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
        i = self.scrollbar.value()
        constants = list(self.plots.plots.keys())[i]
        title = ""
        k = -1
        for j in constants:
            k += 2 if self.plots.x_index == k+1 else 1
            title += self.metadata["key_sets"][k][-1] + f" = {j}, "
        self.pw_plot.setTitle(title[:-2], **font)
    
    def create_new_plot(self, metadata=None):
        if metadata is None:
            # self.metadata = self.remote_dataset.get("scan_plot_metadata")
            pass
        else:
            self.metadata = metadata
        
        #skip redrawing of the plot during these steps
        self.skip = True
        self.cob_x.clear()
        self.cob_x.addItems([key[-1] for key in self.metadata["key_sets"]])
        self.sb_roi.setRange(0, len(self.data[0])-1)
        self.skip = False
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

    def clear_fit(self):
        fit, _, _, _ = self.plots.get_fit(*self.get_plot_indices())
        if fit:
            self.pw_plot.removeItem(fit)
            self.plots.remove_fit(*self.get_plot_indices())
            self.lb_fit.setText("")
    
    def perform_fit(self, arg, func=None, guess=None, fixed=None):       
        self.clear_fit()
        if func is None:
            func = self.cob_fit.currentText()
        x, y = self.get_current_data()
        errors = None
        if "survival" in self.metadata["function"].lower():
            x, y, errors = self.get_survival_rate(x, y)
            errors = (errors[0] + errors[1])/2
            errors = errors[~np.isnan(errors)]
        x = x[~np.isnan(y)]    
        y = y[~np.isnan(y)]
        x, y, popt, perr = self.fitter.perform_fit(x, y, func, errors, guess, fixed)
        fit = self.pw_plot.plot(x, y)

        params = self.fitter.func_dict[func]["params"]
        self.plots.add_fit(*self.get_plot_indices(), fit, params, popt, perr)
        self.write_fit_params(params, popt, perr)

    def advanced_fit(self):
        dialog = FittingDialog(self.fitter.func_dict)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            func, guess, fixed, custom = dialog.get_results()
        else:
            return
        self.perform_fit(None, func, guess, fixed)

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
        extent = [global_x[0] - x_step/2, global_x[-1] + x_step/2, y_vals[-1] + y_step/2, y_vals[0] - y_step/2]

        plt.clf()
        plt.imshow(data, extent=extent, aspect="auto")
        plt.axis("off")
        self.copy_figure_to_clipboard(plt.gcf())
        plt.axis("on")
        plt.colorbar()
        plt.xlabel(self.cob_x.currentText())
        
        y_keys = [key[-1] for i, key in enumerate(self.metadata["key_sets"]) if i != self.plots.x_index]
        plt.ylabel(", ".join(y_keys) if y_keys else "index")
        plt.show()

    def export_plot(self):
        x, y = self.get_current_data()

        if "survival" in self.metadata["function"].lower():
            unique_x, means, err = self.get_survival_rate(x, y)
            lower = err[0]
            upper = err[1]
            idx = (lower > 0) | (upper > 0)
            heights = (lower[idx] + upper[idx]) / 2
            y_centers = means[idx] + (upper[idx] - lower[idx]) / 2

        else:
            unique_x, means, err = self.get_errorbars(x, y)
            heights = err
            y_centers = means

        self.copy_to_clipboard(x=unique_x, y=means, yerr=heights, yerr_center=y_centers)
        idx = heights > 0
        plt.scatter(unique_x, means)
        plt.errorbar(unique_x[idx], y_centers[idx], yerr=heights[idx], capsize=3, fmt="none")
        plt.xlabel(self.cob_x.currentText())
        plt.ylabel(self.metadata["function"])

        fit, _, _, _ = self.plots.get_fit(*self.get_plot_indices())
        if fit:
            x, y = fit.getData()
            plt.plot(x, y, c="r")

        plt.show()
    
    def copy_to_clipboard(self, **kwargs):
        headers = "\t".join(kwargs.keys())
        rows = "\n".join("\t".join(map(str, row)) for row in zip(*kwargs.values()))
        clipboard_data = headers + "\n" + rows + "\n"
        clipboard = QApplication.clipboard()
        clipboard.setText(clipboard_data)
        
    def parameter_plot(self):

        xs = [key[-1] for key in self.metadata["key_sets"]] + ["ROI"]
        del xs[self.plots.x_index]
        fitfunc = self.cob_fit.currentText()
        fit_params = self.fitter.func_dict[fitfunc]["params"]

        dialog = ParameterDialog(fit_params, xs)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            fit_value, x_value = dialog.get_values()
        else:
            return
        
        ys = []
        yerr = []
        if x_value == len(xs) - 1:
            roi = range(self.plots.n_rois)
            index = [self.scrollbar.value()]*len(roi)
            xs = roi
            plt.xlabel("ROI")
        else:
            index = range(self.scrollbar.maximum()+1)
            roi = [self.sb_roi.value()]*len(index)
            plt.xlabel(xs[x_value])
            xs = [v[x_value] for v in self.plots.plots.keys()]
            
        for i, r in zip(index, roi):
            xp, yp = self.plots.get_data(i, r)
            errors = None
            if "survival" in self.metadata["function"].lower():
                xp, yp, errors = self.get_survival_rate(xp, yp)
                errors = (errors[0] + errors[1])/2
                xp = xp[~np.isnan(yp)]    
                yp = yp[~np.isnan(yp)] 

            x, y, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc)
            ys.append(popt[fit_value])
            yerr.append(perr[fit_value])
            fit = self.pw_plot.plot(x, y)
            self.plots.add_fit(i, r, fit, fit_params, popt, perr)
        
        self.draw_plot()
        self.copy_to_clipboard(x=xs, y=ys, yerr=yerr)
        plt.errorbar(xs, ys, capsize=3, fmt="o")
        plt.grid()
        plt.ylabel(fit_params[fit_value])
        plt.show()
    
    def generate_heatmap(self):
        n_rois = self.plots.n_rois
        if n_rois < 2:
            logging.error("Cannot make heatmap of global data")

        fitfunc = self.cob_fit.currentText()
        fit_params = self.fitter.func_dict[fitfunc]["params"]

        dialog = HeatmapDialog(fit_params, n_rois)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            fit_value, num_rows, num_cols = dialog.get_values()
        else:
            return
        
        ys = []
        for i, r in zip([self.scrollbar.value()]*n_rois, range(n_rois)):
            xp, yp = self.plots.get_data(i, r)
            if "survival" in self.metadata["function"].lower():
                xp, yp, _ = self.get_survival_rate(xp, yp)
                xp = xp[~np.isnan(yp)]    
                yp = yp[~np.isnan(yp)] 

            x, y, popt, perr = self.fitter.perform_fit(xp, yp, fitfunc)
            ys.append(popt[fit_value])
            fit = self.pw_plot.plot(x, y)
            self.plots.add_fit(i, r, fit, fit_params, popt, perr)
        
        self.draw_plot()

        arr = np.asarray(ys).reshape((num_rows, num_cols))
        keys = [f"c{i}" for i in range(num_cols)]
        self.copy_to_clipboard(**dict(zip(keys, arr.T)))
        plt.imshow(arr)
        plt.colorbar()
        plt.show()

    def count_clusters(self, values, threshold=3):
        if len(values) == 1:
            return 1
        values = np.sort(values).reshape(-1, 1) 
        clusters = fcluster(linkage(values, method='single'), threshold, criterion='distance')
        return len(np.unique(clusters))
    
    def import_data(self):
        files = self.open_files()
        if files is None: return
        metadata = {"key_sets": [], "points": [], "function": ""}
        data = []
        try:
            for file in files:
                with open(file, "r") as f:
                    header = f.readline()
                    if "Module:" in header:
                        metadata["key_sets"] = [s.strip().split(".") for s in header.strip().split(", ")]
                        for line in f:
                            line = line.strip()
                            if line:
                                metadata["points"].append(self.parse_points(line))

                    elif header.strip() == "":
                        for line in f:
                            line = line.strip()
                            if line:
                                metadata["points"].append(self.parse_points(line))
                    else:
                        metadata["function"] = header.strip()
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("["):
                                data.append(self.parse_bracketed_points(line))
                            else:
                                data.append(self.parse_points(line))
        except Exception:
            logging.exception("Invalid CSV files were imported")
            return
        self.data = data
        self.create_new_plot(metadata=metadata)

    def parse_points(self, line):
        return [float(v) for v in line.split(", ")]

    def parse_bracketed_points(self, line):
        points = []
        for chunk in line.split("],"):
            chunk = chunk.strip().lstrip("[").rstrip("]")
            if chunk:
                x, y = chunk.split(",")
                points.append([float(x), float(y)])
        return points

    def open_files(self):
        # options = QFileDialog.Options()
        files, _ = QFileDialog.getOpenFileNames(self,"Pick log and logResults file",
                                                            FOLDER,
                                                            "CSV Data File (*.csv)",)
                                                            # options=options)
        if len(files) == 2:
            return files
        else:
            logging.error("Exactly 2 files need to be selected: log.csv and logResults.csv")
            return None
    
    def copy_figure_to_clipboard(self, fig):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.02)
        buf.seek(0)

        img = Image.open(buf).convert("RGB")

        output = io.BytesIO()
        img.save(output, "BMP")
        data = output.getvalue()[14:]  # remove BMP header
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
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()