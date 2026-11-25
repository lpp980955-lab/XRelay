"""Small shared desktop theme; no fixed control or scientific-plot sizes."""
STYLE = """
QMainWindow { background: #f4f6f8; }
QPushButton, QToolButton { border: 1px solid #cbd5df; border-radius: 5px; padding: 5px 9px; }
QPushButton:hover, QToolButton:hover { border-color: #257787; background: #edf6f7; }
QPushButton:disabled, QToolButton:disabled { color: #9aa5af; border-color: #e0e5ea; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { border: 1px solid #cbd5df; border-radius: 4px; padding: 3px; }
QTabWidget::pane { border: 1px solid #dde3e9; }
QTabBar::tab { padding: 5px 9px; }
QHeaderView::section { padding: 4px; background: #edf2f5; border: 1px solid #dde3e9; }
QToolTip { color: #243746; background: #fffde9; border: 1px solid #d6dce1; }
"""


def apply_theme(widget):
    import os
    from pathlib import Path
    from PySide6 import QtWidgets,QtGui
    app=QtWidgets.QApplication.instance()
    if not getattr(app,"_workbench_font",False):
        if os.name == "nt":
            font=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"/"msyh.ttc"
            if font.is_file():
                QtGui.QFontDatabase.addApplicationFont(str(font))
                app.setFont(QtGui.QFont("Microsoft YaHei",10))
        app._workbench_font=True
    widget.setStyleSheet(STYLE)
