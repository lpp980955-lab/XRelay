"""Processing intent survives navigation; exported acquisitions stay identifiable."""
import csv
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np
from saxs_waxs_core.background import BackgroundMode, BackgroundRecipe
from saxs_waxs_core.batch import run_batch_plan
from saxs_waxs_core.geometry import DetectorGeometry, GeometryProvenance
from saxs_waxs_core.integration import RadialIntegrationRecipe, AzimuthalIntegrationRecipe
from saxs_waxs_core.masking import MaskRecipe
from saxs_waxs_core.processing import BatchProcessingPlan, ProcessingRecipe
from saxs_waxs_core.readers.detector_io import expand_identities, read_frame, selection_label, frame_label
from saxs_waxs_ui.latest_task import PreviewBackgroundWorkspace, integrate_previews


class PreviewInheritanceTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        self.tmp=tempfile.TemporaryDirectory(dir=root/'artifacts')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'stack.h5'
        self.air=self.root/'air.h5'
        with h5py.File(self.path,'w') as f:
            f['entry/data/data']=np.arange(4*32*40,dtype='uint32').reshape(4,32,40)%100+12
        with h5py.File(self.air,'w') as f:
            f['entry/data/data']=np.stack([np.ones((32,40),dtype='uint32')*n for n in (2,5)])
        self.geometry=DetectorGeometry(wavelength_m=1e-10,detector_distance_m=.4,
            pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=20,beam_center_y_px=16,
            image_width_px=40,image_height_px=32,provenance=GeometryProvenance(method='test'))

    def test_background_cache_tracks_sample_recipe_air_frame_and_file_changes(self):
        cache=PreviewBackgroundWorkspace()
        recipe=BackgroundRecipe(mode=BackgroundMode.AUTO_AIR,
            metadata={'assume_equal_exposure_if_missing':True})
        sample=read_frame(self.path,frame_index=0)
        with patch('saxs_waxs_core.readers.detector_io.read_frame',wraps=read_frame) as loader:
            a=cache.apply(sample,recipe,self.air,{'frame_index':0})
            self.assertIs(cache.apply(sample,recipe,self.air,{'frame_index':0}),a)
            np.testing.assert_array_equal(a.data,sample.data.astype(float)-2)
            second=read_frame(self.path,frame_index=2)
            b=cache.apply(second,recipe,self.air,{'frame_index':0})
            np.testing.assert_array_equal(b.data,second.data.astype(float)-2)
            self.assertEqual(loader.call_count,1)
            c=cache.apply(second,recipe,self.air,{'frame_index':1})
            np.testing.assert_array_equal(c.data,second.data.astype(float)-5)
            self.assertEqual(loader.call_count,2)
            with h5py.File(self.air,'r+') as f:
                f['entry/data/data'][1]=7
            d=cache.apply(second,recipe,self.air,{'frame_index':1})
            np.testing.assert_array_equal(d.data,second.data.astype(float)-7)
            self.assertEqual(loader.call_count,3)
            raw=cache.apply(second,replace(recipe,mode=BackgroundMode.NO_BACKGROUND))
            np.testing.assert_array_equal(raw.data,second.data)
        job=dict(frame=sample,geometry=self.geometry,mask=MaskRecipe(),name='missing air',
            recipe=RadialIntegrationRecipe(npt=16),background_recipe=recipe,background_path=None)
        completed,errors=integrate_previews([job],[])
        self.assertFalse(completed)
        self.assertIn('不能回退',errors[0])

    def test_selected_frames_have_actual_numbers_in_radial_and_azimuthal_exports(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        identities,selections=expand_identities([self.path],frame_choices={str(self.path):[1,3]})
        for integration in (RadialIntegrationRecipe(npt=16,input_unit='detector_count'),
                            AzimuthalIntegrationRecipe(radial_range=(.01,.15),npt_azimuth=36,
                                npt_radial=8,input_unit='detector_count')):
            recipe=ProcessingRecipe(geometry=self.geometry,
                background=BackgroundRecipe(mode=BackgroundMode.NO_BACKGROUND),mask=MaskRecipe(),
                integration=integration)
            plan=BatchProcessingPlan(recipe=recipe,sample_sources=identities,
                metadata={'source_selections':selections})
            destination=self.root/integration.__class__.__name__
            result=run_batch_plan(plan,destination)
            self.assertEqual(result.summary['passed'],2)
            manifest=json.loads(result.manifest_path.read_text(encoding='utf-8'))
            labels=[selection_label(plan,i) for i in range(2)]
            self.assertIn('帧0002',labels[0]); self.assertIn('帧0004',labels[1])
            for i,item in enumerate(manifest['items']):
                self.assertEqual(item['display_name'],labels[i])
                self.assertEqual(item['source_selection']['frame_index'],[1,3][i])
                self.assertIn(f'_frame{[2,4][i]:04d}',item['data_file'])
                self.assertEqual(frame_label(read_frame(self.path,**selections[i])),labels[i])
            combined=next(destination.glob('all_samples_*.csv'))
            with combined.open(encoding='utf-8-sig',newline='') as stream:
                header=next(csv.reader(stream))
            self.assertEqual(len(set(header)),4)
            self.assertTrue(all(label in header[2*i] and label in header[2*i+1]
                                for i,label in enumerate(labels)))
            # All subsequent fit workspaces receive these identities unchanged.
            from saxs_waxs_ui.app import FitCurvePreparationWorker
            worker=FitCurvePreparationWorker(plan)
            payloads=[]; failures=[]
            worker.finished.connect(payloads.append)
            worker.failed.connect(failures.append)
            worker.run()
            self.assertFalse(failures)
            self.assertEqual([name for name,_ in payloads[0]['curves']],labels)
            self.assertEqual([c['processing']['source_selection']['frame_index']
                              for c in payloads[0]['contexts']],[1,3])
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).hexdigest())
