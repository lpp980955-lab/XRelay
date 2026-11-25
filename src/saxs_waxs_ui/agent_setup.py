"""Provider-neutral connection configuration; no remote AI calls from the GUI."""
from pathlib import Path
import json
import sys
from PySide6 import QtCore, QtWidgets


def configuration_path(value):
    path=Path(value).expanduser().resolve()
    protected=(Path(__file__).resolve().parents[2]/"data").resolve()
    if path == protected or protected in path.parents:
        raise ValueError("不能写入原始数据目录")
    return path


def connection_config(input_root,output_root):
    source=Path(input_root).expanduser().resolve(strict=True)
    output=Path(output_root).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("请选择数据文件夹")
    protected=(Path(__file__).resolve().parents[2]/"data").resolve()
    if output == protected or protected in output.parents:
        raise ValueError("请选择原始数据目录之外的输出文件夹")
    args = ["--input-root", str(source), "--output-root", str(output)]
    if getattr(sys, "frozen", False):
        command = Path(sys.executable).with_name("XRelay-Agent.exe")
        if not command.is_file():
            raise ValueError("找不到随工作台打包的 XRelay-Agent.exe")
        config = {"command": str(command), "args": args}
    else:
        config = {
            "command": sys.executable,
            "args": ["-m", "saxs_waxs_agent", *args],
            "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONUTF8": "1"},
        }
    return {"mcpServers": {"saxs-waxs-workbench": config}}



class AgentSetupDialog(QtWidgets.QDialog):
    def __init__(self,parent=None):
        super().__init__(parent)
        from .agent_clients import PROFILES
        from .agent_probe import StdioProbe
        from .adaptive_layout import ResponsiveScrollArea
        self.setWindowTitle("AI Agent 接口")
        self.resize(920,700);self.setSizeGripEnabled(True)
        self._entry=None;self._closing=False;self.last_result=None;self.path_buttons=[]
        self.input_path=QtWidgets.QLineEdit();self.output_path=QtWidgets.QLineEdit()
        if parent is not None and hasattr(parent,"browser"):
            self.input_path.setText(parent.browser.path_edit.text())
        self.client=QtWidgets.QComboBox()
        for key,p in PROFILES.items():self.client.addItem(p['name'],key)
        self.timeout=QtWidgets.QSpinBox();self.timeout.setRange(5,120);self.timeout.setValue(30);self.timeout.setSuffix(" s")
        content=QtWidgets.QWidget();body=QtWidgets.QVBoxLayout(content)
        self.note=QtWidgets.QLabel("人工处理完全离线；此处生成本地 MCP 连接配置。模型、账号及联网行为由所用客户端管理。")
        self.note.setWordWrap(True);body.addWidget(self.note)
        form=QtWidgets.QFormLayout();form.addRow("客户端",self.client)
        for label,edit in (("数据文件夹",self.input_path),("输出文件夹",self.output_path)):
            row=QtWidgets.QHBoxLayout();row.addWidget(edit,1)
            button=QtWidgets.QPushButton("浏览…");button.clicked.connect(lambda _,e=edit:self._browse(e))
            self.path_buttons.append(button)
            row.addWidget(button);form.addRow(label,row)
        form.addRow("本地测试超时",self.timeout);body.addLayout(form)
        self.controls=[]
        actions=QtWidgets.QGridLayout()
        for i,(name,callback) in enumerate((("生成连接配置",self.generate),("复制配置",self.copy_config),("保存配置…",self.save),("合并预览…",self.show_merge))):
            button=QtWidgets.QPushButton(name);button.clicked.connect(callback);actions.addWidget(button,i//2,i%2);self.controls.append(button)
        body.addLayout(actions)
        self.config_state=QtWidgets.QLabel("配置：尚未生成")
        self.service_state=QtWidgets.QLabel("本地服务：尚未测试")
        self.client_state=QtWidgets.QLabel("客户端连接：待在客户端会话中验证")
        for label in (self.config_state,self.service_state,self.client_state):label.setWordWrap(True);body.addWidget(label)
        testrow=QtWidgets.QHBoxLayout()
        self.test_button=QtWidgets.QPushButton("测试本地服务");self.cancel_button=QtWidgets.QPushButton("取消测试")
        self.test_button.clicked.connect(self.test_service);self.cancel_button.clicked.connect(self.cancel_test)
        self.cancel_button.setEnabled(False);testrow.addWidget(self.test_button);testrow.addWidget(self.cancel_button);body.addLayout(testrow)
        self.preview=QtWidgets.QPlainTextEdit();self.preview.setReadOnly(True);self.preview.setMinimumHeight(150)
        self.help=QtWidgets.QPlainTextEdit();self.help.setReadOnly(True);self.help.setMinimumHeight(150)
        tabs=QtWidgets.QTabWidget();tabs.addTab(self.preview,"连接配置");tabs.addTab(self.help,"接入与验证步骤");body.addWidget(tabs,1)
        layout=QtWidgets.QVBoxLayout(self);layout.addWidget(ResponsiveScrollArea(content),1)
        close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close);close.rejected.connect(self.reject);layout.addWidget(close)
        self.probe=StdioProbe(self);self.probe.progress.connect(self.service_state.setText);self.probe.finished.connect(self._tested)
        self.client.currentIndexChanged.connect(self.invalidate)
        self.input_path.textChanged.connect(self.invalidate);self.output_path.textChanged.connect(self.invalidate)
        self.invalidate()
        screen=self.screen()
        if screen:self.resize(min(self.width(),screen.availableGeometry().width()-24),min(self.height(),screen.availableGeometry().height()-48))

    def profile(self):return self.client.currentData()

    def invalidate(self,*_):
        self._entry=None;self.last_result=None;self.preview.clear()
        self.config_state.setText("配置：尚未生成")
        self.service_state.setText("本地服务：尚未测试")
        from .i18n import language_manager
        self.retranslate_agent_ui(language_manager().language)

    def retranslate_agent_ui(self,language):
        from .agent_clients import instructions
        value=instructions(self.profile(),language)
        if self.help.toPlainText()!=value:self.help.setPlainText(value)

    def generate(self):
        from .agent_clients import render_config,SERVER
        self._entry=None;self.preview.clear()
        try:
            if not self.input_path.text().strip() or not self.output_path.text().strip():raise ValueError("请选择数据文件夹和输出文件夹")
            entry=connection_config(self.input_path.text(),self.output_path.text())["mcpServers"][SERVER]
            self.preview.setPlainText(render_config(entry,self.profile()));self._entry=entry
            self.config_state.setText("配置：已生成；未改写任何客户端设置")
        except (ValueError,OSError) as exc:self.config_state.setText(str(exc))
        return self._entry is not None

    def copy_config(self):
        if self.generate():QtWidgets.QApplication.clipboard().setText(self.preview.toPlainText())

    def save(self):
        from .agent_clients import PROFILES
        if not self.generate():return
        profile=PROFILES[self.profile()];extension=profile['format']
        path,_=QtWidgets.QFileDialog.getSaveFileName(self,"保存Agent连接配置",profile['file'],f"{extension.upper()} (*.{extension})")
        if path:
            try:
                with configuration_path(path).open('x',encoding='utf-8') as stream:stream.write(self.preview.toPlainText())
                self.config_state.setText("配置：已保存；请手动合并到客户端，保留已有设置")
            except (ValueError,OSError) as exc:self.config_state.setText(str(exc))

    def show_merge(self):
        from .agent_clients import merge_preview
        if not self.generate():return
        d=QtWidgets.QDialog(self);d.setWindowTitle("配置合并预览");d.resize(720,520);d.setSizeGripEnabled(True)
        layout=QtWidgets.QVBoxLayout(d)
        note=QtWidgets.QLabel("粘贴已有配置（可先去除密钥）；只在内存预览，不读取或改写客户端文件。修改前请自行备份原配置。")
        note.setWordWrap(True);layout.addWidget(note)
        original=QtWidgets.QPlainTextEdit();original.setPlaceholderText("粘贴已有配置")
        merged=QtWidgets.QPlainTextEdit();merged.setReadOnly(True)
        layout.addWidget(original,1);layout.addWidget(merged,1)
        state=QtWidgets.QLabel();state.setWordWrap(True);layout.addWidget(state)
        def update():
            try:merged.setPlainText(merge_preview(original.toPlainText(),self.preview.toPlainText(),self.profile()));state.setText("合并预览已生成；同名冲突会阻止覆盖。")
            except (ValueError,TypeError,AttributeError) as exc:merged.clear();state.setText(str(exc))
        button=QtWidgets.QPushButton("预览合并");button.clicked.connect(update);layout.addWidget(button)
        copy=QtWidgets.QPushButton("复制合并结果");copy.clicked.connect(lambda:QtWidgets.QApplication.clipboard().setText(merged.toPlainText()));layout.addWidget(copy)
        d.exec()

    def _browse(self,edit):
        value=QtWidgets.QFileDialog.getExistingDirectory(self,"选择文件夹",edit.text())
        if value:edit.setText(value)

    def _busy(self,busy):
        for widget in (self.input_path,self.output_path,self.client,self.timeout,*self.path_buttons,*self.controls,self.test_button):widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def test_service(self):
        if self.probe.running or not self.generate():return
        self._busy(True);self.last_result=None
        self.probe.start(self._entry,self.timeout.value()*1000)

    def cancel_test(self):self.probe.cancel()

    def _tested(self,result):
        self.last_result=result;self._busy(False)
        message=result['message']
        if result['success']:message+=f" 工具 {result['tool_count']} 项，协议 {result['protocol']}，版本 {result['server_version']}。"
        self.service_state.setText(message)
        if self._closing:QtCore.QTimer.singleShot(0,self.reject)

    def reject(self):
        if self.probe.running:
            self._closing=True;self.probe.cancel();return
        super().reject()

    def closeEvent(self,event):
        if self.probe.running:
            event.ignore();self._closing=True;self.probe.cancel()
        else:super().closeEvent(event)
