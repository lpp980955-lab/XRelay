"""Report loaded replaceable-library locations; never read experimental data."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

def write_report(destination):
    from PySide6 import QtCore
    from fabio.ext import mar345_IO
    from .branding import resource_path,VERSION
    qt_path=None
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetModuleHandleW.argtypes=[ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype=ctypes.c_void_p
        kernel.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
        kernel.GetModuleFileNameW.restype=ctypes.c_uint32
        handle=kernel.GetModuleHandleW('Qt6Core.dll')
        if not handle:raise OSError('Loaded Qt6Core module was not found')
        buffer=ctypes.create_unicode_buffer(32768)
        if not kernel.GetModuleFileNameW(handle,buffer,len(buffer)):raise ctypes.WinError(ctypes.get_last_error())
        qt_path=buffer.value
    licence=resource_path('licenses/XRelay-LICENSE.txt')
    report=dict(application='XRelay',version=VERSION,licence='LicenseRef-XRelay-Free-Use-1.0',
        author_zh='张久天',author_en='Jiutian Zhang',qt_version=QtCore.qVersion(),
        qt_core_path=qt_path,pyside_core_path=QtCore.__file__,fabio_lgpl_extension_path=mar345_IO.__file__,
        original_licence_sha256=hashlib.sha256(licence.read_bytes()).hexdigest(),
        scope='loaded library locations and bundled original licence; not a legal compliance certificate')
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
    return report

if __name__=='__main__':write_report(sys.argv[1])
