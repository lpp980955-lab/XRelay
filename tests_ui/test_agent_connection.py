"""Client fragments, async stdio failures and a real source-service handshake."""
import json,os,sys,tempfile,time,tomllib,unittest
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6 import QtCore,QtWidgets
from saxs_waxs_ui.agent_clients import PROFILES,SERVER,render_config,merge_preview
from saxs_waxs_ui.agent_probe import StdioProbe
from saxs_waxs_ui.agent_setup import AgentSetupDialog,connection_config
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class AgentConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait(self,predicate,seconds=20):
        until=time.monotonic()+seconds
        while not predicate() and time.monotonic()<until:
            self.app.processEvents();time.sleep(.01)
        self.assertTrue(predicate(),'async operation timed out')

    def test_client_formats_roundtrip_chinese_and_space_paths(self):
        entry={'command':r'C:\中文 软件\XRelay-Agent.exe','args':['--input-root',r'D:\我的 数据\原图'],'env':{'NAME':'中文 "quoted"'}}
        for profile,meta in PROFILES.items():
            with self.subTest(profile=profile):
                text=render_config(entry,profile)
                data=tomllib.loads(text) if meta['format']=='toml' else json.loads(text)
                actual=data['mcp_servers' if meta['format']=='toml' else 'mcpServers'][SERVER]
                for key,value in entry.items():self.assertEqual(actual[key],value)
                self.assertNotIn('trust',actual)

    def test_frozen_config_has_no_source_python_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            release=Path(tmp);(release/'XRelay-Agent.exe').touch()  # Configuration only; never executed.
            with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(release/'XRelay.exe')):
                entry=connection_config(release,release/'out')["mcpServers"][SERVER]
            self.assertEqual(Path(entry['command']),release/'XRelay-Agent.exe')
            self.assertNotIn('env',entry);self.assertNotIn('-m',entry['args'])

    def test_language_switch_changes_help_not_configuration(self):
        from saxs_waxs_ui.i18n import language_manager
        d=AgentSetupDialog();d.input_path.setText(str(ROOT/'artifacts'));d.output_path.setText(str(ROOT/'artifacts'/'test_tmp'))
        d.generate();before=d.preview.toPlainText();manager=language_manager()
        try:
            manager.set_language('en')
            self.assertIn('UNVERIFIED',d.help.toPlainText())
            self.assertEqual(d.preview.toPlainText(),before)
            manager.set_language('zh');self.assertIn('客户端',d.help.toPlainText())
            self.assertEqual(d.preview.toPlainText(),before)
        finally:manager.set_language('zh');d.close();d.deleteLater();self.app.processEvents()

    def test_merge_preserves_unrelated_settings_and_rejects_conflict(self):
        entry={'command':'agent','args':['one']}
        for profile,original in [('generic','{"theme":"dark","mcpServers":{"existing":{"command":"other"}}}'),('codex','# keep comment\nmodel="existing"\n[mcp_servers.other]\ncommand="other"\n')]:
            fragment=render_config(entry,profile);merged=merge_preview(original,fragment,profile)
            self.assertIn('other',merged)
            self.assertEqual(merge_preview(merged,fragment,profile),merged)
            with self.assertRaises(ValueError):merge_preview(merged,render_config({**entry,'command':'changed'},profile),profile)
        self.assertTrue(merge_preview(original,render_config(entry,'codex'),'codex').startswith(original.rstrip()))

    def probe(self,entry,timeout=3000):
        probe=StdioProbe();results=[];ticks=[]
        timer=QtCore.QTimer();timer.setInterval(10);timer.timeout.connect(lambda:ticks.append(1));timer.start()
        probe.finished.connect(results.append);probe.start(entry,timeout)
        self.wait(lambda:bool(results));timer.stop()
        self.assertFalse(probe.running)
        self.assertFalse(results[0]['client_connected'])
        probe.deleteLater();self.app.processEvents()
        return results[0],ticks

    def test_failures_are_bounded_and_do_not_block_event_loop(self):
        for script,category in [('import time;time.sleep(10)','timeout'),('print("not json",flush=True)','invalid_response'),('pass','incomplete'),('raise SystemExit(7)','process_failed')]:
            with self.subTest(category=category):
                result,ticks=self.probe({'command':sys.executable,'args':['-u','-c',script]},180 if category=='timeout' else 3000)
                self.assertEqual(result['category'],category);self.assertFalse(result['success'])
                if category=='timeout':self.assertGreater(len(ticks),2)
        result,_=self.probe({'command':str(ROOT/'does-not-exist.exe'),'args':[]})
        self.assertEqual(result['category'],'failed_to_start')

    def test_wrong_identity_and_missing_tools_are_rejected(self):
        for result in [dict(serverInfo={'name':'other','version':'1'},protocolVersion='2025-06-18',capabilities={'tools':{}})]:
            script='import json,sys;sys.stdin.readline();print('+repr(json.dumps({'jsonrpc':'2.0','id':1,'result':result}))+',flush=True);sys.stdin.read()'
            state,_=self.probe({'command':sys.executable,'args':['-u','-c',script]})
            self.assertEqual(state['category'],'invalid_response')
        script='''import json,sys
sys.stdin.readline()
print(json.dumps(dict(jsonrpc='2.0',id=1,result=dict(serverInfo=dict(name='saxs-waxs-workbench',version='test'),protocolVersion='2025-06-18',capabilities=dict(tools={})))),flush=True)
sys.stdin.readline();sys.stdin.readline()
print(json.dumps(dict(jsonrpc='2.0',id=2,result=dict(tools=[]))),flush=True)
sys.stdin.read()
'''
        state,_=self.probe({'command':sys.executable,'args':['-u','-c',script]})
        self.assertEqual(state['category'],'invalid_response')

    def test_real_service_dialog_generation_invalidation_and_cancel_close(self):
        base=ROOT/'artifacts'/'test_tmp';base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base,prefix='连接 空格 ') as tmp:
            d=AgentSetupDialog();d.input_path.setText(tmp);d.output_path.setText(str(Path(tmp)/'输出 文件'))
            d.client.setCurrentIndex(d.client.findData('codex'));self.assertTrue(d.generate())
            self.assertIn('mcp_servers',d.preview.toPlainText())
            d.test_service();self.wait(lambda:d.last_result is not None,35)
            self.assertTrue(d.last_result['success'],d.last_result)
            self.assertEqual(d.last_result['tool_count'],15)
            self.assertFalse((Path(tmp)/'输出 文件').exists(),'read-only probe created output directory')
            d.output_path.setText(str(Path(tmp)/'另一个 输出'));self.assertEqual(d.preview.toPlainText(),'')
            d.probe.start({'command':sys.executable,'args':['-c','import time;time.sleep(20)']})
            d.show();self.app.processEvents();d.reject()
            self.wait(lambda:not d.probe.running)
            self.app.processEvents();self.assertFalse(d.isVisible())
            d.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
