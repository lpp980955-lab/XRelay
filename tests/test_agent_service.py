import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import numpy as np
from openpyxl import load_workbook
from saxs_waxs_agent.service import AgentService
from saxs_waxs_agent.__main__ import Protocol


ROOT=Path(__file__).resolve().parents[1]


def detector(path):
    y,x=np.indices((64,64))
    r=np.hypot(x+.5-32,y+.5-32)
    data=(20+500*np.exp(-.5*((r-12)/2)**2)).astype('<u4')
    pixels=data.tobytes()
    off=8+len(pixels)
    desc=b'Rigaku 2D image\0'
    tags=[(256,3,1,64),(257,3,1,64),(258,3,1,32),(259,3,1,1),(262,3,1,0),
          (270,2,len(desc),off+2+9*12+4),(273,4,1,8),(277,3,1,1),(279,4,1,len(pixels))]
    path.write_bytes(b'II*\0'+struct.pack('<I',off)+pixels+struct.pack('<H',len(tags))+
        b''.join(struct.pack('<HHII',*t) for t in tags)+b'\0'*4+desc)


class AgentTests(unittest.TestCase):
    def test_json_radial_sectors_match_direct_core_calculation(self):
        from saxs_waxs_core.integration import AzimuthSector,RadialIntegrationRecipe,integrate_radial_1d
        from saxs_waxs_core.geometry import DetectorGeometry
        from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff
        base=ROOT/'artifacts'/'test_tmp';base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp);detector(root/'ring.tif')
            service=AgentService(root,root/'out')
            geometry=dict(wavelength_m=1e-10,detector_distance_m=.2,pixel_size_x_m=1e-4,pixel_size_y_m=1e-4,beam_center_x_px=32,beam_center_y_px=32)
            try:
                settings=dict(npt=40,sectors=[dict(center_deg=90,width_deg=30)])
                job=service.start('integration',dict(paths=['ring.tif'],geometry=geometry,settings=settings))
                final=self.wait(service,job['job_id']);self.assertEqual(final['errors'],[])
                actual=service.jobs[job['job_id']]['curves'][0][1]
                frame=read_tvx_tiff(root/'ring.tif')
                expected=integrate_radial_1d(frame.data,DetectorGeometry(**geometry),RadialIntegrationRecipe(npt=40,sectors=(AzimuthSector(90,30),)))
                np.testing.assert_allclose(actual.x,expected.x)
                np.testing.assert_allclose(actual.intensity,expected.intensity,equal_nan=True)
                bad=service.start('integration',dict(paths=['ring.tif'],geometry=geometry,settings=dict(sectors=[dict(center_deg=90,width_deg=0)])))
                self.assertTrue(self.wait(service,bad['job_id'])['errors'])
            finally:service.close()

    def test_ruland_dispatch_visits_each_selected_detector_frame(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import threading
        import test_detector_formats as formats
        with tempfile.TemporaryDirectory(dir=ROOT/'artifacts'/'test_tmp') as tmp:
            root=Path(tmp);formats.nexus(root/'stack.nxs',np.arange(360).reshape(3,10,12))
            service=AgentService(root,root/'out')
            job={'stop':threading.Event(),'errors':[],'completed':0}
            try:
                with patch('saxs_waxs_core.ruland_workflow.run_ruland_workflow',return_value=SimpleNamespace(error=None)) as compute,patch('saxs_waxs_ui.ruland_export.export_ruland_workbook',return_value=root/'result.xlsx'):
                    service._ruland(job,{'paths':['stack.nxs'],'q_range':[.1,.2],'q_step':.02,'q_width':.01},root)
                self.assertEqual(job['errors'],[])
                self.assertEqual(job['completed'],3)
                self.assertEqual([call.args[0].metadata.frame_index for call in compute.call_args_list],[0,1,2])
            finally:service.close()

    def test_nexus_frames_and_gfrm_share_agent_processing_routes(self):
        import test_detector_formats as formats
        base=ROOT/'artifacts'/'test_tmp';base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp)
            formats.nexus(root/'stack.nxs',np.arange(360,dtype='uint32').reshape(3,10,12))
            formats.bruker86(root/'native.gfrm',np.arange(120,dtype='uint16').reshape(10,12))
            service=AgentService(root,root/'out')
            def wait(job):
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    state=service.get_job(job['job_id'])
                    if state['state'] not in ('queued','running'):return state
                    time.sleep(.01)
                self.fail('Agent job did not finish')
            try:
                self.assertEqual(service.inspect_source('stack.nxs')['datasets'][0]['frames'],3)
                state=wait(service.start('integration',{'paths':['stack.nxs'],'settings':{'npt':20}}))
                self.assertEqual(state['errors'],[])
                self.assertEqual(state['completed'],3)
                state=wait(service.start('processed_data',{'paths':['native.gfrm'],'output_format':'auto'}))
                self.assertEqual(state['errors'],[])
                directory=Path(state['output']).parent
                self.assertEqual(len(list(directory.glob('*.tif'))),1)
                self.assertIn('lossless TIFF',Path(state['output']).read_text(encoding='utf-8'))
            finally:service.close()
    def test_processed_detector_export_then_integrate_with_embedded_geometry(self):
        from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff
        base=ROOT/'artifacts'/'test_tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp);detector(root/'sample.tif')
            service=AgentService(root,root/'out')
            def finish(jobid):
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    state=service.get_job(jobid)
                    if state['state'] not in ('queued','running'):
                        return state
                    time.sleep(.01)
                self.fail('job did not finish')
            try:
                geometry=dict(wavelength_m=1.2e-10,detector_distance_m=.25,
                    pixel_size_x_m=1e-4,pixel_size_y_m=1e-4,beam_center_x_px=32,beam_center_y_px=32)
                job=service.start('processed_data',dict(paths=['sample.tif'],geometry=geometry))
                state=finish(job['job_id'])
                self.assertEqual(state['errors'],[])
                output=Path(state['output']).parent
                path=next(output.glob('*.tif'))
                frame=read_tvx_tiff(path)
                from saxs_waxs_core.processed_data import embedded_geometry
                self.assertEqual(embedded_geometry(frame).detector_distance_m,.25)
                # A new client selection can include the export directory and
                # continue processing without retyping the geometry.
                continuation=AgentService(output,root/'continued')
                try:
                    job=continuation.start('integration',dict(paths=[path.name],settings={'npt':20}))
                    deadline=time.monotonic()+15
                    while continuation.get_job(job['job_id'])['state'] in ('queued','running') and time.monotonic()<deadline:
                        time.sleep(.01)
                    final=continuation.get_job(job['job_id'])
                    self.assertEqual(final['state'],'completed')
                    self.assertEqual(final['errors'],[])
                    self.assertTrue(Path(final['output']).is_file())
                finally:
                    continuation.close()
            finally:
                service.close()
    def test_same_input_output_root_keeps_sources_and_excludes_active_outputs(self):
        base=ROOT/'artifacts'/'test_tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp)
            (root/'sample.csv').write_text('x,y\n1,2\n',encoding='utf-8')
            run=root/'run'
            run.mkdir()
            (run/'derived.csv').write_text('x,y\n1,3\n',encoding='utf-8')
            service=AgentService(root,root)
            try:
                service.run_directories.add(run)
                self.assertEqual([f['path'] for f in service.list_files()['files']],['sample.csv'])
            finally:
                service.close()

    def test_import_analysis_azimuth_and_image_tools_share_the_workbench_core(self):
        base=ROOT/'artifacts'/'test_tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp)/'in'
            root.mkdir()
            q=np.linspace(.01,.3,150)
            np.savetxt(root/'guinier.csv',np.column_stack((q,100*np.exp(-q*q*16/3))),delimiter=',')
            x=np.arange(.5,360,1.)
            np.savetxt(root/'azimuth.csv',np.column_stack((x,3+100*np.exp(-.5*((x-88)/4)**2)+80*np.exp(-.5*((x-268)/6)**2))),delimiter=',')
            detector(root/'one.tif')
            service=AgentService(root,Path(tmp)/'out')
            try:
                common=dict(x_column=0,y_column=1,intensity_unit='count')
                imported=service.start('import_curves',dict(paths=['guinier.csv'],settings={**common,'kind':'radial_1d','x_unit':'q_nm^-1'}))
                self.assertEqual(self.wait(service,imported['job_id'])['errors'],[])
                analysis=service.start('saxs_analysis',dict(integration_job=imported['job_id'],method='guinier',q_range=[.01,.2]))
                result=self.wait(service,analysis['job_id'])
                self.assertEqual(result['errors'],[])
                book=load_workbook(result['output'],data_only=True)
                values=list(book['参数汇总'].values)
                row=dict(zip(values[0],values[1]))
                self.assertAlmostEqual(row['derived_value'],4,places=6)
                book.close()
                azimuth=service.start('import_curves',dict(paths=['azimuth.csv'],settings={**common,'kind':'azimuthal_1d','x_unit':'deg'}))
                self.assertEqual(self.wait(service,azimuth['job_id'])['errors'],[])
                fitted=service.start('fit',dict(integration_job=azimuth['job_id'],automatic_azimuth=True))
                result=self.wait(service,fitted['job_id'])
                self.assertEqual(result['errors'],[])
                book=load_workbook(result['output'],data_only=True)
                values=list(book['参数汇总'].values)
                records=[dict(zip(values[0],row)) for row in values[1:]]
                self.assertEqual(len(records),2)
                self.assertTrue(all(row['Herman']>.9 for row in records))
                book.close()
                images=service.start('images',dict(paths=['one.tif'],dpi=80,display={'limit_mode':'manual','manual_vmin':0,'manual_vmax':600}))
                result=self.wait(service,images['job_id'])
                self.assertEqual(result['errors'],[])
                output=Path(result['output'])
                self.assertEqual(len(list(output.parent.glob('*.png'))),1)
                self.assertEqual(len(list(output.parent.glob('*.txt'))),1)
                self.assertFalse(list(output.parent.glob('*.json')))
            finally:
                service.close()
    def wait(self,service,jid):
        until=time.monotonic()+15
        while time.monotonic()<until:
            state=service.get_job(jid)
            if state['state'] not in ('queued','running'):
                return state
            time.sleep(.01)
        self.fail('job timed out')

    def test_boundaries_integration_fit_and_shared_export(self):
        base=ROOT/'artifacts'/'test_tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp)/'input'
            root.mkdir()
            path=root/'重命名样品.tif'
            detector(path)
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            service=AgentService(root,Path(tmp)/'output')
            try:
                self.assertEqual(len(service.list_files()['files']),1)
                self.assertIsNone(service.inspect_image(path.name)['exposure_s'])
                with self.assertRaises(ValueError):
                    service.input_path('../')
                geo=dict(wavelength_m=1e-10,detector_distance_m=.2,pixel_size_x_m=1e-4,pixel_size_y_m=1e-4,
                         beam_center_x_px=32,beam_center_y_px=32)
                created=service.start('integration',dict(paths=[path.name],geometry=geo,kind='radial',
                    settings=dict(npt=100,radial_range=[.05,1.0])))
                state=self.wait(service,created['job_id'])
                self.assertEqual(state['state'],'completed',state)
                self.assertEqual(state['errors'],[])
                book=load_workbook(state['output'],data_only=True)
                self.assertEqual(book.sheetnames,['曲线数据','参数汇总','处理说明'])
                self.assertEqual(book['曲线数据'].max_column,2)
                book.close()
                fitted=service.start('fit',dict(integration_job=created['job_id'],settings=dict(model='gaussian_height',x_range=[.1,.8])))
                fit=self.wait(service,fitted['job_id'])
                self.assertEqual(fit['state'],'completed',fit)
                self.assertEqual(fit['errors'],[])
                book=load_workbook(fit['output'],data_only=True)
                sheet=book['曲线数据']
                self.assertEqual(sheet.max_column,6)
                self.assertEqual(sheet.cell(2,6).value,'残差（原始−拟合）')
                source_curve=service.jobs[created['job_id']]['curves'][0][1]
                checked=0
                for row in sheet.iter_rows(min_row=5,values_only=True):
                    _,_,fit_x,fit_y,residual_x,residual_y=row
                    if residual_x is None:
                        continue
                    self.assertEqual(fit_x,residual_x)
                    index=int(np.argmin(np.abs(source_curve.x-fit_x)))
                    self.assertAlmostEqual(source_curve.x[index],fit_x,places=12)
                    np.testing.assert_allclose(residual_y,source_curve.intensity[index]-fit_y,
                        rtol=1e-10,atol=1e-10)
                    checked+=1
                self.assertGreater(checked,5)
                book.close()
                self.assertFalse(list(Path(tmp).rglob('*.json')))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            finally:
                service.close()

    def test_protocol_negotiation_schema_and_stdio_subprocess(self):
        base=ROOT/'artifacts'/'test_tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            service=AgentService(tmp,Path(tmp)/'out')
            try:
                protocol=Protocol(service)
                response=protocol.handle(dict(id=1,method='initialize',params={'protocolVersion':'2025-06-18'}))
                self.assertEqual(response['result']['protocolVersion'],'2025-06-18')
                listed=protocol.handle(dict(id=2,method='tools/list'))
                self.assertIn('start_ruland',[t['name'] for t in listed['result']['tools']])
                invalid=protocol.handle(dict(id=3,method='tools/call',params={'name':'start_integration','arguments':{'paths':[]}}))
                self.assertTrue(invalid['result']['isError'])
                self.assertIsNone(protocol.handle(dict(method='notifications/initialized')))
            finally:
                service.close()
            messages=[dict(jsonrpc='2.0',id=1,method='initialize',params={'protocolVersion':'2025-06-18'}),
                      dict(jsonrpc='2.0',method='notifications/initialized'),dict(jsonrpc='2.0',id=2,method='tools/list')]
            env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONUTF8':'1'}
            process=subprocess.run([sys.executable,'-m','saxs_waxs_agent','--input-root',tmp,'--output-root',str(Path(tmp)/'out')],
                input='\n'.join(map(json.dumps,messages))+'\n',capture_output=True,text=True,encoding='utf-8',env=env,timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
            responses=[json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual([r['id'] for r in responses],[1,2])

    def test_manual_calibration_does_not_require_a_known_distance(self):
        from saxs_waxs_core.calibrants import load_calibrant_library
        base=ROOT/'artifacts'/'test_tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            service=AgentService(tmp,Path(tmp)/'out')
            try:
                result=service.calibrate_three_points(.124e-9,.172e-3,.172e-3,'AgBh',1,[[300,300],[200,400],[100,300]])
                d=load_calibrant_library().resolve('AgBh').peaks[0].d_angstrom*1e-10
                expected=100*.172e-3/np.tan(2*np.arcsin(.124e-9/(2*d)))
                self.assertAlmostEqual(result['geometry']['detector_distance_m'],expected,places=8)
                self.assertAlmostEqual(result['geometry']['beam_center_x_px'],200,places=7)
                self.assertAlmostEqual(result['geometry']['beam_center_y_px'],300,places=7)
            finally:
                service.close()
