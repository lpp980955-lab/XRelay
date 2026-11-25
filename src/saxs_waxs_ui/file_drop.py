"""Local file/folder drops routed through the existing import functions."""
from pathlib import Path
from PySide6 import QtCore, QtWidgets


def input_files(paths, accepts):
    files={}
    for value in paths:
        path=Path(value).expanduser().absolute()
        entries=path.rglob('*') if path.is_dir() else (path,)
        for entry in entries:
            if entry.is_file() and accepts(entry):
                files[str(entry.resolve()).casefold()]=entry
    return sorted(files.values(),key=lambda p:str(p).casefold())


def choose_input(parent, paths, accepts, title):
    candidates=input_files(paths,accepts)
    if not candidates:
        raise ValueError('拖入的文件或文件夹中没有可用于此入口的文件')
    if len(candidates)==1:
        return candidates[0]
    names=[str(p) for p in candidates]
    chosen,accepted=QtWidgets.QInputDialog.getItem(parent,title,'选择要导入的文件',names,0,False)
    return candidates[names.index(chosen)] if accepted else None


class FileDrop(QtCore.QObject):
    def __init__(self,widget,callback,on_error,descendants=True):
        super().__init__(widget)
        self.callback,self.on_error=callback,on_error
        # Include scroll viewports and editors so they do not consume URL text.
        for target in [widget,*(widget.findChildren(QtWidgets.QWidget) if descendants else [])]:
            target.setAcceptDrops(True)
            target.installEventFilter(self)
        widget.setToolTip((widget.toolTip()+'\n可拖入本地文件或文件夹').strip())

    def eventFilter(self,watched,event):
        if event.type() not in (QtCore.QEvent.Type.DragEnter,QtCore.QEvent.Type.DragMove,QtCore.QEvent.Type.Drop):
            return False
        mime=event.mimeData()
        urls=mime.urls() if mime.hasUrls() else []
        if not urls or any(not url.isLocalFile() for url in urls):
            event.ignore()
            return True
        if event.type()==QtCore.QEvent.Type.Drop:
            try:
                self.callback([Path(url.toLocalFile()) for url in urls])
            except (OSError,ValueError,RuntimeError) as exc:
                self.on_error(f'导入失败：{exc}')
        event.acceptProposedAction()
        return True


def install_file_drop(widget,callback,on_error,*,descendants=True):
    drop=FileDrop(widget,callback,on_error,descendants)
    # Keep the Python wrapper alive for Qt's virtual eventFilter callback.
    widget._file_drop=drop
    return drop
