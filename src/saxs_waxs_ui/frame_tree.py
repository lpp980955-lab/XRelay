"""Collapsed sample tree with lazy frame children; viewing never checks frames."""
from PySide6 import QtCore,QtWidgets


class SampleItem(QtWidgets.QTreeWidgetItem):
    def __init__(self,label):
        super().__init__([label])
        self.base_label=label
    def checkState(self,column=0):return super().checkState(column)
    def setCheckState(self,*args):
        return super().setCheckState(0,args[0]) if len(args)==1 else super().setCheckState(*args)
    def setToolTip(self,*args):
        return super().setToolTip(0,args[0]) if len(args)==1 else super().setToolTip(*args)


class FrameTree(QtWidgets.QTreeWidget):
    currentRowChanged=QtCore.Signal(int)
    frame_selected=QtCore.Signal(object,int)
    frames_checked=QtCore.Signal(object,object)

    def __init__(self):
        super().__init__()
        self.setHeaderHidden(True)
        self.currentItemChanged.connect(self._current)
        self.itemExpanded.connect(self._expand)
        self.itemChanged.connect(self._checked)

    def count(self):return self.topLevelItemCount()
    def addItem(self,item):self.addTopLevelItem(item)
    def item(self,row):return self.topLevelItem(row)
    def currentRow(self):
        item=self.currentItem()
        if item is not None and item.parent() is not None:item=item.parent()
        return self.indexOfTopLevelItem(item)
    def setCurrentRow(self,row,*args):self.setCurrentItem(self.item(row),0,*args)

    def _current(self,item,previous):
        if item is None:return
        if item.parent() is None:self.currentRowChanged.emit(self.indexOfTopLevelItem(item))
        elif item.frame_index is None:self._append_page(item.parent())
        else:self.frame_selected.emit(item.parent().source_path,item.frame_index)

    def register(self,row,path,count,selected):
        blocker=QtCore.QSignalBlocker(self)
        item=self.item(row)
        previous_count=getattr(item,"frame_count",None)
        item.source_path=path
        item.frame_count=count
        item.frame_selection=set((range(count) if item.checkState()!=QtCore.Qt.CheckState.Unchecked else ()) if selected is None else selected)
        item.setCheckState(QtCore.Qt.CheckState.Checked if len(item.frame_selection)==count else
                           QtCore.Qt.CheckState.PartiallyChecked if item.frame_selection else QtCore.Qt.CheckState.Unchecked)
        if count>1:
            item.setText(0,f"{item.base_label}\n{count}帧 · 已选{len(item.frame_selection)}帧")
            item.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if previous_count!=count:
            item.takeChildren()
        if item.childCount():self._refresh_checks(item)
        del blocker

    def _expand(self,item):
        if item.parent() is not None or item.childCount() or not hasattr(item,"frame_count"):return
        self._append_page(item)

    def _append_page(self,item):
        blocker=QtCore.QSignalBlocker(self)
        if item.childCount() and item.child(item.childCount()-1).frame_index is None:
            item.takeChild(item.childCount()-1)
        start=item.childCount()
        end=min(item.frame_count,start+500)
        for index in range(start,end):
            child=QtWidgets.QTreeWidgetItem(item,[f"帧 {index+1:04d}"])
            child.frame_index=index
            child.setFlags(child.flags()|QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0,QtCore.Qt.CheckState.Checked if index in item.frame_selection else QtCore.Qt.CheckState.Unchecked)
        if end<item.frame_count:
            more=QtWidgets.QTreeWidgetItem(item,["显示更多帧…"])
            more.frame_index=None
        del blocker

    def _refresh_checks(self,item):
        blocker=QtCore.QSignalBlocker(self)
        for index in range(item.childCount()):
            child=item.child(index)
            if child.frame_index is not None:
                child.setCheckState(0,QtCore.Qt.CheckState.Checked if child.frame_index in item.frame_selection else QtCore.Qt.CheckState.Unchecked)
        del blocker

    def _checked(self,item,column):
        parent=item.parent()
        if parent is None:
            if not hasattr(item,'frame_count'):return
            item.frame_selection=set(range(item.frame_count)) if item.checkState(0)==QtCore.Qt.CheckState.Checked else set()
            blocker=QtCore.QSignalBlocker(self)
            item.setText(0,f"{item.base_label}\n{item.frame_count}帧 · 已选{len(item.frame_selection)}帧")
            self._refresh_checks(item)
            del blocker
            self.frames_checked.emit(item.source_path,sorted(item.frame_selection))
            return
        if item.frame_index is None:return
        blocker=QtCore.QSignalBlocker(self)
        if item.checkState(0)==QtCore.Qt.CheckState.Checked:
            parent.frame_selection.add(item.frame_index)
        else:parent.frame_selection.discard(item.frame_index)
        parent.setText(0,f"{parent.base_label}\n{parent.frame_count}帧 · 已选{len(parent.frame_selection)}帧")
        state=(QtCore.Qt.CheckState.Checked if len(parent.frame_selection)==parent.frame_count else
               QtCore.Qt.CheckState.PartiallyChecked if parent.frame_selection else QtCore.Qt.CheckState.Unchecked)
        parent.setCheckState(0,state)
        del blocker
        self.frames_checked.emit(parent.source_path,sorted(parent.frame_selection))

    def selected_frame_items(self):
        return [(item.parent().source_path,item.frame_index) for item in self.selectedItems() if item.parent() is not None and item.frame_index is not None]

    def selected_sample_rows(self):
        return sorted({self.indexOfTopLevelItem(item if item.parent() is None else item.parent()) for item in self.selectedItems()})
