from .export_naming import choose_export_folder
from saxs_waxs_core.analysis_export import export_analysis_workbook


def choose_analysis_export(panel):
    from pathlib import Path
    from PySide6 import QtWidgets
    from .fit_export import FitExportDialog
    status=getattr(panel,"label",None) or panel.summary
    if not panel.results and not panel.batch_started:
        status.setText("请先计算，再导出结果。")
        return
    dialog=FitExportDialog(panel,scope_items=(("当前曲线","current"),("全部曲线","batch")),
        note_text="默认一个工作簿，三张表：曲线数据、参数汇总、处理说明。保留完整原始曲线，导出不重新计算。")
    if panel.batch_started:
        dialog.scope_combo.setCurrentIndex(1)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return
    indices=[panel.selector.currentIndex()] if dialog.scope_combo.currentData() == "current" else list(range(len(panel.curves)))
    try:
        if dialog.content_combo.currentData() == "data":
            path,_=QtWidgets.QFileDialog.getSaveFileName(panel,"保存分析结果","analysis.xlsx","Excel (*.xlsx)")
            if path:
                export_analysis_workbook(panel,Path(path),indices=indices)
                status.setText("已导出：曲线数据、参数汇总、处理说明。")
        else:
            folder=choose_export_folder(panel,"保存分析图片")
            if folder:
                panel.start_image_export(Path(folder),indices,image_format=dialog.format_combo.currentText().lower())
    except (ValueError,OSError) as exc:
        status.setText(f"导出失败：{exc}")
