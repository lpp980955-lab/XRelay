"""Compact settings and text preview for external curve import."""
from pathlib import Path
import csv
from PySide6 import QtWidgets


class CurveImportDialog(QtWidgets.QDialog):
    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.files = tuple(files)
        self.setWindowTitle("导入一维曲线")
        self.resize(690, 490)
        self.axis = QtWidgets.QComboBox()
        for label, kind, unit in (("q (nm⁻¹)", "radial_1d", "q_nm^-1"),
                ("q (Å⁻¹)", "radial_1d", "q_A^-1"), ("s (nm⁻¹)", "radial_1d", "s_nm^-1"),
                ("2θ (°)", "radial_1d", "2th_deg"), ("方位角 (°)", "azimuthal_1d", "deg")):
            self.axis.addItem(label, (kind, unit))
        self.encoding = QtWidgets.QComboBox()
        self.encoding.addItem("UTF-8", "utf-8-sig")
        self.encoding.addItem("GB18030（中文）", "gb18030")
        self.header_rows = QtWidgets.QSpinBox()
        self.header_rows.setRange(0, 10000)
        self.header_rows.setToolTip("非空、非注释表头行数；0 自动识别文本，数值型单位行需计入。")
        self.columns = QtWidgets.QLineEdit("1,2")
        self.columns.setToolTip("从1开始；每组可选x,y或x,y,误差，例如 1,2,3;4,5,6。")
        self.intensity_unit = QtWidgets.QLineEdit("a.u.")
        self.sigma_column = QtWidgets.QSpinBox()
        self.sigma_column.setRange(0, 10000)
        self.sigma_column.setSpecialValueText("不导入")
        self.zero_is_missing = QtWidgets.QCheckBox("将零强度标为无效（仅在确认它代表遮挡时使用）")
        self.wavelength = QtWidgets.QDoubleSpinBox()
        self.wavelength.setDecimals(6)
        self.wavelength.setRange(0, 100)
        self.wavelength.setSpecialValueText("未知")
        self.wavelength.setSuffix(" nm")
        self.wavelength.setEnabled(False)
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        form = QtWidgets.QFormLayout()
        form.addRow("横坐标类型", self.axis)
        form.addRow("文件编码", self.encoding)
        form.addRow("表头行数", self.header_rows)
        form.addRow("列对", self.columns)
        form.addRow("强度单位", self.intensity_unit)
        form.addRow("共用误差列（可选）", self.sigma_column)
        form.addRow("", self.zero_is_missing)
        form.addRow("波长（2θ换算）", self.wavelength)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        hint = QtWidgets.QLabel("列从1开始，多组用分号隔开；例如 1,2;3,4 或 1,2,3;4,5,6（第三列为误差）。数字单位行需计入表头行数。当前拟合不按误差加权。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QtWidgets.QLabel(f"首个文件预览：{Path(files[0]).name}（最多20行）"))
        self.column_legend = QtWidgets.QLabel()
        self.column_legend.setWordWrap(True)
        layout.addWidget(self.column_legend)
        layout.addWidget(self.preview, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                             QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.encoding.currentIndexChanged.connect(self.update_preview)
        self.axis.currentIndexChanged.connect(lambda: self.wavelength.setEnabled(self.axis.currentData()[1] == "2th_deg"))
        self.update_preview()

    def validate_and_accept(self):
        try:
            self.settings()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "设置无效", str(exc))
            return
        self.accept()

    def update_preview(self):
        try:
            with Path(self.files[0]).open(encoding=self.encoding.currentData()) as stream:
                lines = []
                for _ in range(20):
                    line = stream.readline(4096)
                    if not line:
                        break
                    lines.append(line.rstrip("\r\n"))
            self.preview.setPlainText("\n".join(lines))
            content = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
            if content:
                try:
                    delimiter = csv.Sniffer().sniff("\n".join(content), delimiters=",\t;").delimiter
                    columns = next(csv.reader([content[0]], delimiter=delimiter))
                except csv.Error:
                    columns = content[0].split()
                headings = "　|　".join(f"{index}: {value.strip()[:25]}" for index, value in enumerate(columns[:12], 1))
                self.column_legend.setText("首行列号：" + headings + ("　|　…" if len(columns) > 12 else ""))
            else:
                self.column_legend.setText("没有可预览的非空行")
        except (OSError, UnicodeDecodeError) as exc:
            self.preview.setPlainText(f"预览失败：{exc}")
            self.column_legend.setText("无法识别列号")

    def settings(self):
        pairs = []
        try:
            for group in self.columns.text().replace("；", ";").replace("，", ",").split(";"):
                values = [int(value.strip()) for value in group.split(",")]
                if len(values) not in (2, 3) or min(values) < 1 or len(set(values)) != len(values):
                    raise ValueError
                pairs.append(tuple(values))
            if len(set(pairs)) != len(pairs) or not self.intensity_unit.text().strip():
                raise ValueError
        except ValueError as exc:
            raise ValueError("请输入不重复的正整数列组和强度单位，例如 1,2;3,4 或 1,2,3;4,5,6。") from exc
        kind, unit = self.axis.currentData()
        wavelength = self.wavelength.value() if unit == "2th_deg" and self.wavelength.value() > 0 else None
        sigma_column = self.sigma_column.value() or None
        if sigma_column is not None and any(len(pair) == 2 and sigma_column in pair for pair in pairs):
            raise ValueError("误差列不能与横纵坐标列相同")
        return kind, unit, self.encoding.currentData(), self.header_rows.value(), pairs, self.intensity_unit.text().strip(), wavelength, sigma_column, self.zero_is_missing.isChecked()
