"""Curated scientific colormaps and a small auditable custom-palette editor."""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from saxs_waxs_core.colormap_data import (
    DEFAULT_CUSTOM_COLORS,
    STATIC_PALETTES,
    load_colorcet_rgb,
)


@dataclass(frozen=True, slots=True)
class ColormapEntry:
    key: str
    label: str
    category: str
    note: str


COLORMAP_CATALOG = (
    ColormapEntry("viridis", "Viridis", "Sequential", "感知均匀，默认推荐"),
    ColormapEntry("cividis", "Cividis", "Sequential", "色觉缺陷友好"),
    ColormapEntry("inferno", "Inferno", "Sequential", "暗背景、高动态范围"),
    ColormapEntry("magma", "Magma", "Sequential", "暗背景、低值细节"),
    ColormapEntry("plasma", "Plasma", "Sequential", "高对比彩色顺序"),
    ColormapEntry("CET-L3", "CET L3", "Sequential", "感知均匀线性色图"),
    ColormapEntry("CET-L8", "CET L8", "Sequential", "感知均匀线性色图"),
    ColormapEntry("CET-L17", "CET L17", "Sequential", "感知均匀线性色图"),
    ColormapEntry("gray", "Gray", "Detector", "传统衍射图显示"),
    ColormapEntry("hot", "Hot", "Detector", "黑–红–黄–白"),
    ColormapEntry("turbo", "Turbo", "Detector", "高对比；非单调亮度"),
    ColormapEntry("jet", "Jet", "Detector", "兼容传统显示；不推荐定量出版"),
    ColormapEntry("blue_yellow_red", "Blue-Yellow-Red", "Detector", "衍射强度高对比"),
    ColormapEntry("coolwarm", "Coolwarm", "Diverging", "围绕中值的发散色图"),
    ColormapEntry("RdBu", "RdBu", "Diverging", "围绕中值的红蓝发散"),
    ColormapEntry("CET-D1", "CET D1", "Diverging", "感知均匀发散色图"),
    ColormapEntry("CET-D4", "CET D4", "Diverging", "感知均匀发散色图"),
    ColormapEntry("CET-D8", "CET D8", "Diverging", "感知均匀发散色图"),
    ColormapEntry("CET-C1", "CET C1", "Cyclic", "周期色图"),
    ColormapEntry("CET-C2", "CET C2", "Cyclic", "周期色图"),
    ColormapEntry("CET-C6", "CET C6", "Cyclic", "周期色图"),
    ColormapEntry("CET-I1", "CET I1", "Special", "近等亮度；不作为默认强度图"),
    ColormapEntry("custom", "Custom", "Custom", "2–8个颜色节点，线性插值"),
)

COLORMAP_KEYS = tuple(entry.key for entry in COLORMAP_CATALOG)

def make_colormap(
    key: str,
    *,
    invert: bool = False,
    custom_colors: tuple[str, ...] = DEFAULT_CUSTOM_COLORS,
) -> pg.ColorMap:
    if key == "custom":
        colors = custom_colors
    elif key in STATIC_PALETTES:
        colors = STATIC_PALETTES[key]
    elif key.startswith("CET-"):
        rgb = load_colorcet_rgb(key)
        if invert:
            rgb = tuple(reversed(rgb))
        positions = np.linspace(0.0, 1.0, len(rgb))
        qcolors = [QtGui.QColor.fromRgbF(*color) for color in rgb]
        return pg.ColorMap(positions, qcolors, name=f"{key}{'_r' if invert else ''}")
    else:
        result = copy.deepcopy(pg.colormap.get(key))
        result.name = f"{key}{'_r' if invert else ''}"
        if invert:
            result.reverse()
        return result
    if invert:
        colors = tuple(reversed(colors))
    qcolors = [QtGui.QColor(color) for color in colors]
    positions = np.linspace(0.0, 1.0, len(qcolors))
    return pg.ColorMap(positions, qcolors, name=f"{key}{'_r' if invert else ''}")


def gradient_pixmap(
    key: str,
    *,
    custom_colors: tuple[str, ...] = DEFAULT_CUSTOM_COLORS,
    width: int = 150,
    height: int = 18,
) -> QtGui.QPixmap:
    color_map = make_colormap(key, custom_colors=custom_colors)
    table = color_map.getLookupTable(nPts=max(2, width), alpha=False)
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    for x in range(width):
        red, green, blue = (int(value) for value in table[x][:3])
        color = QtGui.QColor(red, green, blue)
        for y in range(height):
            image.setPixelColor(x, y, color)
    return QtGui.QPixmap.fromImage(image)


class CustomColormapDialog(QtWidgets.QDialog):
    """Edit two to eight ordered color nodes with a live gradient preview."""

    def __init__(
        self,
        colors: tuple[str, ...] = DEFAULT_CUSTOM_COLORS,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("自定义配色")
        self.setModal(True)
        self._colors = list(colors)
        self.preview = QtWidgets.QLabel()
        self.preview.setMinimumSize(360, 28)
        self.preview.setScaledContents(True)
        self.nodes_layout = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("增加颜色")
        self.remove_button = QtWidgets.QPushButton("删除末端")
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("颜色按从低强度到高强度排列："))
        layout.addLayout(self.nodes_layout)
        layout.addWidget(self.preview)
        layout.addLayout(actions)
        layout.addWidget(self.buttons)
        self.add_button.clicked.connect(self._add_color)
        self.remove_button.clicked.connect(self._remove_color)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._rebuild()

    @property
    def colors(self) -> tuple[str, ...]:
        return tuple(self._colors)

    def _rebuild(self) -> None:
        while self.nodes_layout.count():
            item = self.nodes_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for index, color in enumerate(self._colors):
            button = QtWidgets.QPushButton(f"{index + 1}\n{color}")
            button.setMinimumWidth(72)
            button.setStyleSheet(f"QPushButton {{ background: {color}; color: {'white' if QtGui.QColor(color).lightness() < 120 else 'black'}; }}")
            button.clicked.connect(lambda _checked=False, i=index: self._choose_color(i))
            self.nodes_layout.addWidget(button)
        self.preview.setPixmap(gradient_pixmap("custom", custom_colors=self.colors, width=720, height=40))
        self.add_button.setEnabled(len(self._colors) < 8)
        self.remove_button.setEnabled(len(self._colors) > 2)

    def _choose_color(self, index: int) -> None:
        selected = QtWidgets.QColorDialog.getColor(QtGui.QColor(self._colors[index]), self)
        if selected.isValid():
            self._colors[index] = selected.name(QtGui.QColor.NameFormat.HexRgb).upper()
            self._rebuild()

    def _add_color(self) -> None:
        if len(self._colors) < 8:
            self._colors.append(self._colors[-1])
            self._rebuild()

    def _remove_color(self) -> None:
        if len(self._colors) > 2:
            self._colors.pop()
            self._rebuild()
