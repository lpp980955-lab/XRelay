import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6 import QtWidgets
from saxs_waxs_ui.export_naming import choose_run_directory,choose_export_folder,validate_name


class ExportNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def test_batch_custom_blank_and_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('LFY 一维',True)):
                self.assertEqual(choose_run_directory(None,root,'batch_radial_123'),root/'LFY 一维')
            (root/'batch_radial_123').mkdir()
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('',True)):
                self.assertEqual(choose_run_directory(None,root,'batch_radial_123'),root/'batch_radial_123_2')
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('',False)):
                self.assertIsNone(choose_run_directory(None,root,'batch_radial_123'))
    def test_existing_custom_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'taken').mkdir()
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('taken',True)),patch.object(QtWidgets.QMessageBox,'warning') as warning:
                self.assertIsNone(choose_run_directory(None,temp,'default'))
                warning.assert_called_once()
    def test_image_folder_custom_and_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(QtWidgets.QFileDialog,'getExistingDirectory',return_value=temp):
                with patch.object(QtWidgets.QInputDialog,'getText',return_value=('图片',True)):
                    self.assertEqual(choose_export_folder(None,'export'),str(Path(temp)/'图片'))
                    self.assertTrue((Path(temp)/'图片').is_dir())
                with patch.object(QtWidgets.QInputDialog,'getText',return_value=('',True)):
                    self.assertEqual(choose_export_folder(None,'export'),temp)
    def test_invalid_names(self):
        for name in ('../outside','a/b','..','CON','aux.txt','a.','bad?name'):
            with self.subTest(name=name),self.assertRaises(ValueError):validate_name(name)


if __name__=='__main__':unittest.main()
