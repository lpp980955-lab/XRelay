import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import csv,io,json,itertools,tempfile,time,unittest
from pathlib import Path
import numpy as np
from PIL import Image
from PySide6 import QtCore,QtWidgets,QtTest
from saxs_waxs_core.integration import IntegrationCurve
from saxs_waxs_ui.integration_panel import IntegrationPanel
from saxs_waxs_ui.curve_export_dialog import CurveExportDialog
from saxs_waxs_ui.curve_render import snapshot_plot,render_plot,preview_png,save_curve_export
from saxs_waxs_ui.curve_style import curve_label,CurveStyleDialog,default_style
from saxs_waxs_ui.i18n import language_manager


class CurveExportWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.settings=QtCore.QSettings(str(self.root/'prefs.ini'),QtCore.QSettings.Format.IniFormat)
        self.panel=IntegrationPanel(plot_settings=self.settings)
        x=np.linspace(.1,4,100)
        self.curve=IntegrationCurve(x,np.sin(x)+2,None,np.ones(100),'q_nm^-1','counts','radial_1d',{})
        self.name='sample_master.h5 · 帧0002 [/entry/data/data_000001]'
        self.panel.show_curves([(self.name,self.curve)])
        self.dialog=None

    def drain(self,predicate,timeout=20):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():return
            time.sleep(.01)
        self.fail('Background work did not finish')

    def tearDown(self):
        if self.dialog:
            self.dialog.request_stop()
            self.drain(lambda:not self.dialog._export_preview_tasks.busy and not self.dialog._export_tasks.busy)
            self.dialog.close()
        self.panel.close();self.panel.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents();self.tmp.cleanup()

    def test_style_shared_with_renderer_and_original_identity(self):
        self.assertEqual(curve_label(self.panel,0),'sample · 帧0002')
        key=self.panel._visible_curve_keys[0]
        self.panel._curve_overrides[key]=dict(alias='Specimen 2',color='#ff0000',width=3.,style='dash')
        self.panel._curve_style.update(label_size=15.,tick_size=12.,ticks_in=False,minor_count=2,title='Comparison')
        self.panel._render_curves()
        snapshot=snapshot_plot(self.panel)
        figure,axis,_=render_plot(snapshot,dpi=100)
        try:
            line=axis.lines[0]
            self.assertEqual(line.get_color(),'#ff0000');self.assertEqual(line.get_linewidth(),3.)
            self.assertEqual(line.get_linestyle(),'--');self.assertEqual(axis.get_title(),'Comparison')
            self.assertEqual(axis.get_legend().get_texts()[0].get_text(),'Specimen 2')
            np.testing.assert_array_equal(line.get_ydata(),self.curve.intensity)
        finally:figure.clear()
        target=self.root/'custom-name.csv';save_curve_export(snapshot,target)
        with target.open(encoding='utf-8-sig') as stream:rows=list(csv.reader(stream))
        self.assertIn(self.name,rows[0][0]);self.assertNotIn('Specimen 2',rows[0][0])
        self.assertEqual(len(rows),102)

    def test_four_axis_ticks_defaults_all_combinations_and_saved_style(self):
        self.assertEqual([self.panel._curve_style['ticks_'+side] for side in ('bottom','left','top','right')],[True,True,False,False])
        original=self.curve.intensity.copy()
        self.panel.four_frame_check.setChecked(True)
        for values in itertools.product((False,True),repeat=4):
            expected=dict(zip(('bottom','left','top','right'),values))
            self.panel._curve_style.update({'ticks_'+side:value for side,value in expected.items()})
            self.panel._render_curves()
            for side,visible in expected.items():
                self.assertEqual(self.panel._plot_axes[side].tickPen().style()!=QtCore.Qt.PenStyle.NoPen,visible)
            fig,axis,_=render_plot(snapshot_plot(self.panel),dpi=90)
            try:
                for name,which in (('xaxis',('bottom','top')),('yaxis',('left','right'))):
                    for ticks in (getattr(axis,name).get_major_ticks(),getattr(axis,name).get_minor_ticks()):
                        self.assertTrue(ticks)
                        self.assertEqual(ticks[0].tick1line.get_visible(),expected[which[0]])
                        self.assertEqual(ticks[0].tick2line.get_visible(),expected[which[1]])
                self.assertTrue(axis.xaxis.get_major_ticks()[0].label1.get_visible())
            finally:fig.clear()
        editor=CurveStyleDialog(self.panel)
        editor.controls['ticks_bottom'].setChecked(False);editor.controls['ticks_top'].setChecked(True)
        editor.apply();editor._save_default();editor.close()
        restored=default_style(self.settings)
        self.assertFalse(restored['ticks_bottom']);self.assertTrue(restored['ticks_top'])
        np.testing.assert_array_equal(self.curve.intensity,original)
        self.settings.setValue('curvePlot/default_style',json.dumps(dict(top_ticks=True,frame=True)))
        migrated=default_style(self.settings)
        self.assertTrue(migrated['ticks_top']);self.assertTrue(migrated['ticks_right'])

    def test_menu_language_and_default_persistence(self):
        self.assertEqual(IntegrationPanel._publication_matplotlib_style()['font.size'],9.5)
        manager=language_manager();manager.set_language('zh')
        menu=self.panel._build_curve_menu()
        action=next(a for a in menu.actions() if a.text()=='四边框')
        action.setChecked(True);self.assertTrue(self.panel.four_frame_check.isChecked())
        manager.set_language('en');self.assertEqual(action.text(),'Box frame')
        manager.set_language('zh');self.assertEqual(action.text(),'四边框')
        self.assertTrue(self.panel.plot_options_section.isHidden())
        editor=CurveStyleDialog(self.panel)
        editor.controls['label_size'].setValue(17.);editor.apply();editor._save_default()
        self.assertEqual(default_style(self.settings)['label_size'],17.)
        editor.close();menu.close()

    def test_export_dialog_preview_async_exports_and_closed_lifecycle(self):
        self.dialog=CurveExportDialog(self.panel);d=self.dialog;d.show()
        self.assertTrue(d.isSizeGripEnabled());d.resize(1200,800);self.app.processEvents()
        d.content_combo.setCurrentIndex(d.content_combo.findData('images'))
        self.drain(lambda:not d.canvas.item.pixmap().isNull())
        self.assertGreaterEqual(d.canvas.item.pixmap().width(),2700)
        canvas_size=d.canvas.size()
        self.panel._curve_style['label_size']=18.;self.panel._render_curves()
        self.drain(lambda:not d._export_preview_timer.isActive() and not d._export_preview_tasks.busy)
        self.assertEqual(d.canvas.size(),canvas_size)
        d.dpi_spin.setValue(100);image=self.root/'chosen-image.png';d.export_to(image)
        self.drain(lambda:image.exists() and not d._export_tasks.busy)
        with Image.open(image) as img:self.assertAlmostEqual(img.width,180/25.4*100,delta=1)
        d.content_combo.setCurrentIndex(d.content_combo.findData('data'))
        d.format_combo.setCurrentText('XLSX');target=self.root/'chosen-data.xlsx';d.export_to(target)
        self.drain(lambda:target.exists() and not d._export_tasks.busy)
        from openpyxl import load_workbook
        book=load_workbook(target,read_only=True)
        self.assertEqual(len(book.sheetnames),3);book.close()
        d.reject();self.panel.plot_changed.emit();self.app.processEvents()
        self.assertFalse(d._export_preview_timer.isActive())

    def test_modal_maximized_export_controls_are_not_covered_and_accept_clicks(self):
        self.dialog=CurveExportDialog(self.panel);d=self.dialog
        errors=[]
        def verify():
            try:
                d.showMaximized()
                QtTest.QTest.qWait(150)
                control_rect=QtCore.QRect(d.content_combo.mapTo(d,QtCore.QPoint(0,0)),d.content_combo.size())
                table_rect=QtCore.QRect(d.table.mapTo(d,QtCore.QPoint(0,0)),d.table.size())
                self.assertFalse(control_rect.intersects(table_rect))
                d._display_layout.report()
                from saxs_waxs_ui.runtime_diagnostics import environment
                self.assertFalse(environment()['display']['export_dialog']['overlap'])
                point=d.content_combo.mapToGlobal(d.content_combo.rect().center())
                hit=QtWidgets.QApplication.widgetAt(point)
                self.assertIs(hit,d.content_combo,f'covered by {type(hit).__name__}: {hit.text() if hasattr(hit,"text") else ""}; combo={d.content_combo.geometry()}, parent={d.content_combo.parentWidget().geometry()}')
                QtTest.QTest.mouseClick(d.content_combo,QtCore.Qt.MouseButton.LeftButton)
                QtTest.QTest.keyClick(d.content_combo,QtCore.Qt.Key.Key_Down)
                QtTest.QTest.keyClick(d.content_combo,QtCore.Qt.Key.Key_Return)
                self.assertEqual(d.content_combo.currentData(),'images')
                QtTest.QTest.mouseClick(d.style_button,QtCore.Qt.MouseButton.LeftButton)
                self.app.processEvents()
                self.assertTrue(self.panel._curve_style_dialog.isVisible())
                self.panel._curve_style_dialog.close()
            except BaseException as exc:errors.append(exc)
            finally:d.reject()
        QtCore.QTimer.singleShot(500,verify)
        d.exec()
        if errors:raise errors[0]
