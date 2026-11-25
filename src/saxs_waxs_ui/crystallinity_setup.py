"""Manual WAXS phase assignments and physical-coordinate peak bounds."""
from PySide6 import QtWidgets
import numpy as np


def suggest_peak_definitions(curve, lower, upper):
    """Propose editable bounds from resolved maxima, leaving phase identity open."""
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks, peak_widths

    x = np.asarray(curve.x)
    y = np.asarray(curve.intensity)
    within = (lower <= x) & (x <= upper)
    x, y = x[within], y[within]
    if x.size < 9 or not np.isfinite(y).all() or not np.all(np.diff(x) > 0):
        return ()
    smooth = gaussian_filter1d(y.astype(float), 1.0)
    peaks, properties = find_peaks(smooth, prominence=0.05 * np.ptp(smooth), distance=3)
    if not peaks.size:
        return ()
    _, _, left, right = peak_widths(smooth, peaks)
    indices = np.arange(x.size)
    definitions = []
    for i in np.argsort(properties["prominences"])[-8:]:
        center = float(x[peaks[i]])
        width = float(np.interp(right[i], indices, x) - np.interp(left[i], indices, x))
        if width <= 0:
            continue
        definitions.append({
            "center_bounds": (max(lower, center - width / 2), min(upper, center + width / 2)),
            "fwhm_bounds": (width / 3, width * 3),
        })
    return sorted(definitions, key=lambda item: item["center_bounds"][0])


def validate_peak_definitions(definitions):
    definitions = [dict(item) for item in definitions]
    if not 2 <= len(definitions) <= 8:
        raise ValueError("晶峰与非晶峰合计需要2至8个分量")
    if {d["role"] for d in definitions} != {"crystalline", "amorphous"}:
        raise ValueError("需人工指定至少一个晶峰与一个非晶峰")
    for d in definitions:
        lo, hi = d["center_bounds"]
        if not np.isfinite((lo, hi)).all() or lo >= hi:
            raise ValueError("每个峰都需有效的峰位下限和上限")
        if d["model"] not in ("gaussian_area", "lorentzian_area", "pseudo_voigt_area"):
            raise ValueError("请选择受支持的峰型")
        if d.get("fwhm_bounds") is not None:
            a, b = d["fwhm_bounds"]
            if not np.isfinite((a, b)).all() or not 0 < a < b:
                raise ValueError("FWHM上下限需为正且递增")
    return sorted(definitions, key=lambda d: sum(d["center_bounds"])/2)


class CrystallinitySetupDialog(QtWidgets.QDialog):
    def __init__(self, definitions=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("WAXS晶峰 / 非晶峰定义")
        self.resize(900, 400)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["归属", "峰型", "峰位下限", "峰位上限", "FWHM下限", "FWHM上限"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.note = QtWidgets.QLabel("峰位和宽度可从当前曲线建议，请审核并指定晶峰或非晶峰。面积比参考 Liu 2025 SI。峰位范围必填，宽度范围可留空；单位与当前曲线横坐标相同。提交后代表曲线试拟合，整批沿用相同约束。")
        self.note.setWordWrap(True)
        add, remove = QtWidgets.QPushButton("添加峰"), QtWidgets.QPushButton("删除所选峰")
        add.clicked.connect(lambda: self.add_peak())
        remove.clicked.connect(self._remove)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("应用并试拟合")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)
        for d in (*definitions, *({} for _ in range(max(0, 2 - len(definitions))))):
            self.add_peak(d)

    def add_peak(self, definition=None):
        if self.table.rowCount() >= 8:
            return
        d = definition or {}
        row = self.table.rowCount()
        self.table.insertRow(row)
        role = QtWidgets.QComboBox()
        for label, value in (("请选择", None), ("结晶峰", "crystalline"), ("非晶峰", "amorphous")):
            role.addItem(label, value)
        role.setCurrentIndex(max(0, role.findData(d.get("role"))))
        model = QtWidgets.QComboBox()
        for label, value in (("Gaussian", "gaussian_area"), ("Lorentzian", "lorentzian_area"), ("Pseudo-Voigt", "pseudo_voigt_area")):
            model.addItem(label, value)
        model.setCurrentIndex(max(0, model.findData(d.get("model", "gaussian_area"))))
        self.table.setCellWidget(row, 0, role)
        self.table.setCellWidget(row, 1, model)
        values = (*d.get("center_bounds", (None, None)), *(d.get("fwhm_bounds") or (None, None)))
        for col, value in enumerate(values, 2):
            self.table.setItem(row, col, QtWidgets.QTableWidgetItem("" if value is None else str(value)))

    def _remove(self):
        if self.table.rowCount() > 2 and self.table.currentRow() >= 0:
            self.table.removeRow(self.table.currentRow())

    def _accept(self):
        try:
            definitions = []
            for row in range(self.table.rowCount()):
                text = [self.table.item(row, col).text().strip() for col in range(2, 6)]
                definitions.append({"role": self.table.cellWidget(row, 0).currentData(),
                    "model": self.table.cellWidget(row, 1).currentData(),
                    "center_bounds": tuple(map(float, text[:2])),
                    "fwhm_bounds": tuple(map(float, text[2:])) if any(text[2:]) else None})
            self.definitions = validate_peak_definitions(definitions)
        except ValueError as exc:
            self.note.setText(f"设置无效：{exc}；峰位上下限必填。")
            return
        self.accept()
