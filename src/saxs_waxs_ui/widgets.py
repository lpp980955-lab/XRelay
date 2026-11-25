"""Small reusable widgets for a compact, progressive-disclosure UI."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


def scrollable_content(content: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    """Keep expanded forms readable instead of shrinking controls to fit."""
    content.layout().setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
    from .adaptive_layout import ResponsiveScrollArea
    scroll = ResponsiveScrollArea(content)
    return scroll


def install_panel_scroll(panel: QtWidgets.QWidget, content: QtWidgets.QWidget) -> None:
    layout = QtWidgets.QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    panel.content_scroll = scrollable_content(content)
    layout.addWidget(panel.content_scroll)


class CollapsibleSection(QtWidgets.QWidget):
    """A lightweight inline drawer that keeps uncommon controls out of the way."""

    def __init__(
        self,
        title: str,
        content: QtWidgets.QWidget,
        *,
        expanded: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.toggle_button = QtWidgets.QToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.toggle_button.setAutoRaise(True)
        self.content = content
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        self.content.setVisible(expanded)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content)
        self.toggle_button.toggled.connect(self.set_expanded)
        self.set_expanded(expanded)

    @property
    def expanded(self) -> bool:
        return self.toggle_button.isChecked()

    @QtCore.Slot(bool)
    def set_expanded(self, expanded: bool) -> None:
        blocker = QtCore.QSignalBlocker(self.toggle_button)
        self.toggle_button.setChecked(expanded)
        del blocker
        self.toggle_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow
            if expanded
            else QtCore.Qt.ArrowType.RightArrow
        )
        self.content.setVisible(expanded)
        if self.content.layout() is not None:
            self.content.layout().activate()
        self.updateGeometry()
