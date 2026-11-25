"""Bounded, asynchronous, read-only MCP initialization and tool discovery."""
import json,time
from PySide6 import QtCore


class StdioProbe(QtCore.QObject):
    progress=QtCore.Signal(str)
    finished=QtCore.Signal(object)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.process=None;self.running=False;self._buffer=b'';self._stderr=b'';self._result=None
        self._reason=None;self._stage=0;self._started=0.;self._stdout_bytes=0
        self.timer=QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._timeout)

    def start(self,entry,timeout_ms=30000):
        if self.running:raise ValueError('已有本地服务测试正在运行')
        if self.process is not None:self.process.deleteLater()
        self._buffer=b'';self._stderr=b'';self._result=None;self._reason=None;self._stage=0;self._stdout_bytes=0
        self._started=time.monotonic();self.running=True
        process=QtCore.QProcess(self);self.process=process
        env=QtCore.QProcessEnvironment.systemEnvironment()
        for key,value in entry.get('env',{}).items():env.insert(key,value)
        process.setProcessEnvironment(env);process.setProgram(entry['command']);process.setArguments(entry['args'])
        process.started.connect(self._initialize)
        process.readyReadStandardOutput.connect(self._read)
        process.readyReadStandardError.connect(self._read_errors)
        process.errorOccurred.connect(self._error)
        process.finished.connect(self._exited)
        self.progress.emit('正在启动本地 Agent…')
        self.timer.start(timeout_ms);process.start()

    def _send(self,message):
        self.process.write((json.dumps(dict(jsonrpc='2.0',**message),ensure_ascii=False)+'\n').encode('utf-8'))

    def _initialize(self):
        self.progress.emit('正在协商 MCP 协议…')
        self._send(dict(id=1,method='initialize',params=dict(protocolVersion='2025-06-18',capabilities={},clientInfo=dict(name='XRelay connection test',version='1'))))

    def _read_errors(self):
        self._stderr=(self._stderr+bytes(self.process.readAllStandardError()))[-4096:]

    def _read(self):
        raw=bytes(self.process.readAllStandardOutput());self._stdout_bytes+=len(raw);self._buffer+=raw
        if self._stdout_bytes>2*1024*1024:self._stop('invalid_response','服务响应超过大小限制');return
        while b'\n' in self._buffer and self._reason is None:
            line,self._buffer=self._buffer.split(b'\n',1)
            if not line.strip():continue
            try:
                message=json.loads(line.decode('utf-8'))
                if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':raise ValueError('非 JSON-RPC 2.0 响应')
                if 'error' in message:raise ValueError('服务返回协议错误')
                if 'id' not in message:continue
                if self._stage==0 and message['id']==1:
                    result=message['result'];info=result['serverInfo'];protocol=result['protocolVersion']
                    if info['name']!='saxs-waxs-workbench':raise ValueError('服务身份不匹配')
                    if protocol not in ('2024-11-05','2025-03-26','2025-06-18'):raise ValueError('不支持的协议版本')
                    if 'tools' not in result.get('capabilities',{}):raise ValueError('服务未声明工具能力')
                    self._result=dict(server=info['name'],server_version=info['version'],protocol=protocol)
                    self._stage=1;self.progress.emit('正在发现工具…')
                    self._send(dict(method='notifications/initialized'))
                    self._send(dict(id=2,method='tools/list'))
                elif self._stage==1 and message['id']==2:
                    tools=message['result']['tools'];names=[item['name'] for item in tools]
                    if len(set(names))!=len(names) or not {'get_workflow_guide','list_files','inspect_image'}.issubset(names):raise ValueError('工具列表缺少必要工具或包含重复项')
                    self._result.update(tools=names,tool_count=len(names));self._stage=2
                    self.progress.emit('正在结束只读测试进程…');self.process.closeWriteChannel()
                else:raise ValueError('响应顺序或请求编号异常')
            except (ValueError,KeyError,TypeError,UnicodeError) as exc:
                self._stop('invalid_response','服务响应异常：'+str(exc))

    def _stop(self,category,message):
        if not self.running:return
        self._reason=(category,message)
        if self.process.state()!=QtCore.QProcess.ProcessState.NotRunning:self.process.kill()
        else:self._finish(False,category,message)

    def cancel(self):self._stop('cancelled','本地服务测试已取消')

    def _timeout(self):self._stop('timeout','本地服务测试超时；请检查程序路径、目录权限或增加超时。')

    def _error(self,error):
        if error==QtCore.QProcess.ProcessError.FailedToStart:
            self._finish(False,'failed_to_start','服务启动失败：'+self.process.errorString())

    def _exited(self,code,status):
        self._read();self._read_errors()
        if not self.running:return
        if self._reason:self._finish(False,*self._reason)
        elif status!=QtCore.QProcess.ExitStatus.NormalExit or code!=0:
            self._finish(False,'process_failed','服务异常退出：'+self._stderr.decode('utf-8',errors='replace'))
        elif self._stage!=2 or self._buffer.strip():self._finish(False,'incomplete','服务提前结束，握手或工具发现未完成。')
        else:self._finish(True,'passed','本地服务测试通过；客户端实际连接仍待验证。')

    def _finish(self,success,category,message):
        if not self.running:return
        self.running=False;self.timer.stop()
        result=dict(self._result or {},success=success,category=category,message=message,
            elapsed_seconds=round(time.monotonic()-self._started,3),client_connected=False)
        from .runtime_diagnostics import record
        record('agent_probe',success=success,category=category,elapsed_seconds=result['elapsed_seconds'],tool_count=result.get('tool_count',0))
        self.finished.emit(result)
