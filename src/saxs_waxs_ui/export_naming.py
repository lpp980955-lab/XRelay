"""Optional user names for export directories; empty keeps legacy defaults."""
from pathlib import Path
import re
from PySide6 import QtWidgets


def validate_name(name):
    if name in {'.','..'} or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.endswith((' ','.')):
        raise ValueError('名称不能包含路径分隔符、特殊字符或末尾的空格/句点。')
    stem=name.split('.')[0].upper()
    if stem in {'CON','PRN','AUX','NUL'} or re.fullmatch(r'(COM|LPT)[1-9]',stem):
        raise ValueError('该名称是 Windows 保留名称，请换一个。')


def choose_run_directory(parent,base,default_name):
    value,ok=QtWidgets.QInputDialog.getText(parent,'命名导出文件夹',
        f'文件夹名称（留空沿用默认名称：{default_name}）')
    if not ok:return None
    name=value.strip()
    try:
        if name:validate_name(name)
    except ValueError as exc:
        QtWidgets.QMessageBox.warning(parent,'名称无效',str(exc));return None
    target=Path(base)/(name or default_name)
    if name and target.exists():
        QtWidgets.QMessageBox.warning(parent,'文件夹已存在','请使用新的名称，避免混入已有结果。');return None
    index=2
    while target.exists():
        target=Path(base)/f'{default_name}_{index}';index+=1
    return target


def choose_export_folder(parent,title,initial=''):
    folder=QtWidgets.QFileDialog.getExistingDirectory(parent,title,initial)
    if not folder:return ''
    name,ok=QtWidgets.QInputDialog.getText(parent,'命名导出文件夹',
        '新文件夹名称（留空直接使用刚选的文件夹，保持原命名规则）')
    if not ok:return ''
    name=name.strip()
    if not name:return folder
    try:
        validate_name(name)
        target=Path(folder)/name
        target.mkdir(exist_ok=False)
        return str(target)
    except (OSError,ValueError) as exc:
        QtWidgets.QMessageBox.warning(parent,'无法创建导出文件夹',str(exc));return ''
