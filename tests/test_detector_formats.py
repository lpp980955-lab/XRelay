from pathlib import Path
import tempfile
import unittest
import struct
import hashlib
import numpy as np
import h5py
from PIL import Image,TiffImagePlugin
from fabio.edfimage import EdfImage
from fabio.cbfimage import CbfImage
from fabio.bruker100image import Bruker100Image
from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff,TiffReaderError
from saxs_waxs_core.readers.detector_io import (SourceSelectionRequired,source_selections,
    source_catalog,expand_identities)
from saxs_waxs_core.processed_data import export_processed_batch,embedded_geometry
from saxs_waxs_core.masking import build_mask

ROOT=Path(__file__).resolve().parents[1]


def bruker86(path,values):
    a=np.asarray(values,dtype='<u2')
    header={'FORMAT':'        86','HDRBLKS':'5','NPIXELB':'2','NOVERFL':'0','NROWS':str(a.shape[0]),'NCOLS':str(a.shape[1])}
    text=''.join((key.ljust(7)+':'+value).ljust(80) for key,value in header.items())
    path.write_bytes(text.encode('ascii').ljust(2560,b' ')+a.tobytes())


def bruker100(path,values):
    a=np.asarray(values)
    under=a[a<=0].astype('<i2')
    first=np.minimum(a[a>=255],65535).astype('<u2')
    second=a[a>=65535].astype('<u4')
    raw=np.clip(a,0,255).astype('uint8')
    header={'FORMAT':'100','HDRBLKS':'5','NPIXELB':'1 2','NOVERFL':f'{len(under)} {len(first)} {len(second)}',
            'NEXP':'1 1 0 0 0','NROWS':str(a.shape[0]),'NCOLS':str(a.shape[1]),'CUMULAT':'2.0'}
    text=''.join((key.ljust(7)+':'+value).ljust(80) for key,value in header.items())
    with path.open('wb') as stream:
        stream.write(text.encode('ascii').ljust(2560,b' '));stream.write(raw.tobytes())
        for array in (under,first,second):
            value=array.tobytes();stream.write(value+b'\0'*((-len(value))%16))


def nexus(path,values,*,explicit_center=True,compressed='gzip'):
    with h5py.File(path,'w') as f:
        entry=f.create_group('entry');entry.attrs['NX_class']='NXentry'
        data=entry.create_group('data');data.attrs.update(NX_class='NXdata',signal='data')
        data.attrs['axes']=['frame','y','x'] if values.ndim==3 else ['y','x']
        data.create_dataset('data',data=values,compression=compressed)
        detector=entry.create_group('instrument/detector');detector.attrs['NX_class']='NXdetector'
        if explicit_center:detector.attrs['center_convention']='canonical_top_left_edge'
        for key,value,unit in [('distance',234.,'mm'),('x_pixel_size',.1,'mm'),('y_pixel_size',120.,'um'),
                               ('beam_center_x',values.shape[-1]*.4,'pixel'),('beam_center_y',values.shape[-2]*.6,'pixel'),
                               ('count_time',2.,'s')]:
            detector.create_dataset(key,data=value).attrs['units']=unit
        mask=np.zeros(values.shape[-2:],dtype='uint32');mask[0,1]=1
        detector.create_dataset('pixel_mask',data=mask)
        beam=entry.create_group('instrument/beam');beam.attrs['NX_class']='NXbeam'
        beam.create_dataset('incident_wavelength',data=1.24).attrs['units']='angstrom'


class DetectorFormatTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'artifacts'/'test_tmp';base.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    def test_independent_bruker86_integer_bytes_and_renamed_content(self):
        values=(np.arange(13)[:,None]*100+np.arange(17)).astype('uint16')
        path=self.root/'not-an-example.gfrm';bruker86(path,values)
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        frame=read_tvx_tiff(path)
        self.assertEqual(frame.metadata.format_id,'bruker86_v1')
        np.testing.assert_array_equal(frame.data,values)
        copied=self.root/'misleading.edf';copied.write_bytes(path.read_bytes())
        np.testing.assert_array_equal(read_tvx_tiff(copied).data,values)
        self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())

    def test_bruker100_overflow_underflow(self):
        values=np.array([[-3,0,1,254],[255,256,65535,65536],[70000,100000,1000000,20000000]],dtype='int32')
        path=self.root/'different.sfrm'
        bruker100(path,values)
        frame=read_tvx_tiff(path)
        self.assertEqual(frame.metadata.format_id,'bruker100_v1')
        np.testing.assert_array_equal(frame.data,values)

    def test_cbf_signed_values_and_edf_float64_multiframe(self):
        a=np.array([[0,-8,500],[2**20,2**25,-2**18]],dtype='int32')
        cbf=self.root/'signal.cbf';CbfImage(a).write(str(cbf))
        np.testing.assert_array_equal(read_tvx_tiff(cbf).data,a)
        edf=self.root/'signals.edf'
        image=EdfImage(a.astype(float)/7,header={'exposure_time_s':'2'})
        image.append_frame(data=(a+17).astype(float)/11,header={'exposure_time_s':'3'})
        image.write(str(edf))
        with self.assertRaises(SourceSelectionRequired):read_tvx_tiff(edf)
        selections=source_selections(edf)
        self.assertEqual(len(selections),2)
        np.testing.assert_array_equal(read_tvx_tiff(edf,**selections[1]).data,(a+17).astype(float)/11)
        result=export_processed_batch([edf],self.root/'edf_output')
        self.assertEqual(result['completed'],2)
        self.assertEqual({Path(r['output']).suffix for r in result['items']},{'.edf'})
        restored=read_tvx_tiff(self.root/'edf_output'/result['items'][1]['output'])
        np.testing.assert_array_equal(restored.data,(a+17).astype(float)/11)

    def test_scientific_tiff_dtypes_compression_rgb_rejected(self):
        for kind in ('uint16','int16','int32','float32'):
            values=np.arange(120).reshape(10,12).astype(kind)
            if kind.startswith('float') or kind.startswith('int'):values=values-60
            path=self.root/(kind+'.tiff')
            Image.fromarray(values).save(path,compression='tiff_lzw')
            frame=read_tvx_tiff(path)
            np.testing.assert_array_equal(frame.data,values)
        path=self.root/'rgb.tif';Image.fromarray(np.zeros((10,12,3),dtype='uint8')).save(path)
        with self.assertRaises(TiffReaderError):read_tvx_tiff(path)

    def test_nexus_stack_units_geometry_mask_and_lossless_same_container(self):
        a=np.arange(3*10*12,dtype='uint32').reshape(3,10,12)
        path=self.root/'no-fixed-location.h5';nexus(path,a)
        with self.assertRaises(SourceSelectionRequired):read_tvx_tiff(path)
        frame=read_tvx_tiff(path,frame_index=2)
        np.testing.assert_array_equal(frame.data,a[2])
        g=embedded_geometry(frame)
        self.assertAlmostEqual(g.detector_distance_m,.234)
        self.assertAlmostEqual(g.wavelength_m,1.24e-10)
        self.assertTrue(build_mask(frame).excluded[0,1])
        ids,selections=expand_identities([path])
        self.assertEqual(len(ids),3)
        self.assertEqual([s['frame_index'] for s in selections],[0,1,2])
        result=export_processed_batch([path],self.root/'nexus_output')
        self.assertEqual(result['completed'],3)
        for n,row in enumerate(result['items']):
            self.assertEqual(Path(row['output']).suffix,'.h5')
            restored=read_tvx_tiff(self.root/'nexus_output'/row['output'])
            mask=build_mask(frame).excluded
            np.testing.assert_array_equal(restored.data[~mask],a[n][~mask])
            self.assertEqual(embedded_geometry(restored),g)

    def test_ambiguous_nexus_units_links_and_explicit_dataset(self):
        path=self.root/'ambiguous.nxs'
        with h5py.File(path,'w') as file:
            file['detectorA/data']=np.arange(12).reshape(3,4)
            file['detectorB/data']=np.arange(12,24).reshape(3,4)
        with self.assertRaises(SourceSelectionRequired):read_tvx_tiff(path)
        frame=read_tvx_tiff(path,dataset='/detectorB/data')
        np.testing.assert_array_equal(frame.data,np.arange(12,24).reshape(3,4))
        path=self.root/'missing-convention.nxs';nexus(path,np.arange(120).reshape(10,12),explicit_center=False)
        self.assertIsNone(embedded_geometry(read_tvx_tiff(path)))
        outside=self.root.parent/'not_inside_this_test.nxs'
        master=self.root/'unsafe.h5'
        with h5py.File(master,'w') as f:f['outside']=h5py.ExternalLink(str(outside),'/data')
        with self.assertRaisesRegex(TiffReaderError,'leaves'):source_catalog(master)

    def test_eiger_external_chunk_series_expands_every_frame(self):
        master=self.root/'master.h5'
        for n in (1,2):
            with h5py.File(self.root/f'chunk{n}.h5','w') as f:f['data']=np.full((2,3,4),n,dtype='uint16')
        with h5py.File(master,'w') as f:
            data=f.create_group('entry/data');data.attrs['NX_class']='NXdata'
            for n in (1,2):data[f'data_{n:06d}']=h5py.ExternalLink(f'chunk{n}.h5','/data')
        choices=source_selections(master)
        self.assertEqual(len(choices),4)
        np.testing.assert_array_equal(read_tvx_tiff(master,**choices[-1]).data,np.full((3,4),2))
        from saxs_waxs_core.readers.detector_io import expand_identities,verify_dependencies,dependency_snapshot
        details={}
        identities,expanded=expand_identities([self.root/'chunk1.h5',master,self.root/'chunk2.h5'],details=details)
        self.assertEqual(len(identities),4)
        self.assertEqual(len(details['excluded']),2)
        output=export_processed_batch([master,self.root/'chunk1.h5',self.root/'chunk2.h5'],self.root/'all_chunks')
        self.assertEqual(output['completed'],4)
        snapshot=dependency_snapshot(source_catalog(master))
        with h5py.File(self.root/'chunk1.h5','a') as file:file.attrs['changed']='after selection'
        with self.assertRaisesRegex(TiffReaderError,'changed'):verify_dependencies(snapshot)

    def test_nested_detector_mask_and_declared_saturation(self):
        from saxs_waxs_core.masking import build_mask
        values=np.array([[1,2,3],[4,5,100]],dtype='uint32')
        path=self.root/'renamed.h5'
        nexus(path,values)
        with h5py.File(path,'a') as file:
            detector=file['entry/instrument/detector']
            nested=detector.create_group('detectorSpecific')
            mask=np.zeros(values.shape,dtype='uint32')
            mask[1,1]=8
            nested.create_dataset('pixel_mask',data=mask)
            detector.create_dataset('saturation_value',data=100)
        frame=read_tvx_tiff(path)
        np.testing.assert_array_equal(frame.data,values)
        self.assertFalse(frame.data.flags.writeable)
        self.assertTrue(frame.metadata.detector_mask[0,1])
        self.assertTrue(frame.metadata.detector_mask[1,1])
        self.assertEqual(frame.metadata.count_cutoff,100)
        self.assertTrue(build_mask(frame).excluded[1,2])
        self.assertEqual(len(frame.metadata.raw_tags['detector_mask_sources']),2)

    def test_explicit_frame_selection_controls_batch_and_numerical_export(self):
        from saxs_waxs_core.readers.detector_io import expand_identities
        from saxs_waxs_core.processed_data import export_processed_batch
        path=self.root/'arbitrary_stack.h5'
        nexus(path,np.arange(60,dtype='uint32').reshape(5,3,4))
        selected={str(path.resolve()):[4,1,1]}
        identities,selections=expand_identities([path],frame_choices=selected)
        self.assertEqual(len(identities),2)
        self.assertEqual([s['frame_index'] for s in selections],[1,4])
        result=export_processed_batch([path],self.root/'selected_frames',frame_choices=selected)
        self.assertEqual(result['completed'],2)
        self.assertEqual([row['frame_index'] for row in result['items']],[1,4])
        for row in result['items']:
            restored=read_tvx_tiff(Path(result['directory'])/row['output'])
            expected=np.arange(60,dtype='uint32').reshape(5,3,4)[row['frame_index']]
            valid=np.isfinite(restored.data)
            np.testing.assert_array_equal(restored.data[valid],expected[valid])
        for invalid in ([],[5],[-1],[True]):
            with self.assertRaises(TiffReaderError):
                expand_identities([path],frame_choices={str(path.resolve()):invalid})

    def test_missing_external_chunk_reports_its_identity(self):
        folder=self.root/'中文目录'
        folder.mkdir()
        master=folder/'主文件.h5'
        with h5py.File(master,'w') as file:
            data=file.create_group('entry/data')
            data.attrs['NX_class']='NXdata'
            data['data_000001']=h5py.ExternalLink('缺失分块.h5','/data')
        with self.assertRaisesRegex(TiffReaderError,'data_000001'):
            source_selections(master)

    def test_corrupt_compressed_frame_isolated_from_neighbours(self):
        folder=self.root/'中文坏帧'
        folder.mkdir()
        path=folder/'主文件.h5'
        expected=np.arange(36,dtype='uint32').reshape(3,3,4)
        with h5py.File(path,'w') as file:
            data=file.create_group('entry/data')
            data.attrs.update(NX_class='NXdata',signal='data')
            ds=data.create_dataset('data',data=expected,chunks=(1,3,4),
                compression='gzip',fletcher32=True)
            ds.id.write_direct_chunk((1,0,0),b'corrupted compressed frame',filter_mask=0)
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(len(source_selections(path)),3)
        with self.assertRaises(TiffReaderError):
            read_tvx_tiff(path,frame_index=1)
        result=export_processed_batch([path],self.root/'isolated_failure')
        self.assertEqual([r['status'] for r in result['items']],['completed','failed','completed'])
        for row in (result['items'][0],result['items'][2]):
            restored=read_tvx_tiff(Path(result['directory'])/row['output'])
            np.testing.assert_array_equal(restored.data,expected[row['frame_index']])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_hdf_hardlink_alias_does_not_duplicate_frames(self):
        path=self.root/'aliases.h5'
        with h5py.File(path,'w') as file:
            array=file.create_dataset('raw',data=np.arange(240).reshape(2,10,12))
            group=file.create_group('entry/data');group.attrs.update(NX_class='NXdata',signal='data')
            group['data']=array
        self.assertEqual(len(source_selections(path)),2)

    def test_cbf_gfrm_safe_conversion_is_explicit_and_lossless(self):
        a=np.arange(120).reshape(10,12).astype('uint16')
        path=self.root/'raw.gfrm';bruker86(path,a)
        result=export_processed_batch([path],self.root/'converted',output_format='auto')
        self.assertEqual(result['completed'],1)
        row=result['items'][0]
        self.assertTrue(row['conversion'])
        self.assertEqual(Path(row['output']).suffix,'.tif')
        np.testing.assert_array_equal(read_tvx_tiff(self.root/'converted'/row['output']).data,a)
        rejected=export_processed_batch([path],self.root/'strict',output_format='source')
        self.assertEqual(rejected['failed'],1)
        self.assertIn('select lossless TIFF',rejected['items'][0]['reason'])

    def test_multiframe_tiff_and_orientation_are_explicit(self):
        a=np.arange(120,dtype='int32').reshape(10,12)-50
        path=self.root/'stack.tif'
        Image.fromarray(a).save(path,save_all=True,append_images=[Image.fromarray(a+100)])
        with self.assertRaises(SourceSelectionRequired):read_tvx_tiff(path)
        np.testing.assert_array_equal(read_tvx_tiff(path,frame_index=1).data,a+100)
        with self.assertRaises(TiffReaderError):read_tvx_tiff(path,frame_index=2)
        tags=TiffImagePlugin.ImageFileDirectory_v2();tags[274]=3
        rotated=self.root/'orientation3.tif';Image.fromarray(a).save(rotated,tiffinfo=tags)
        with self.assertRaisesRegex(TiffReaderError,'Orientation'):read_tvx_tiff(rotated)

    def test_external_gzip_bzip2_detector_wrappers_and_discovery(self):
        import gzip,bz2
        from saxs_waxs_core.discovery import discover_image_files
        a=np.arange(120,dtype='int32').reshape(10,12)-70
        plain=self.root/'signal.edf';EdfImage(a).write(str(plain))
        wrapped=[]
        for suffix,compress in [('gz',gzip.compress),('bz2',bz2.compress)]:
            path=self.root/f'signal.edf.{suffix}';path.write_bytes(compress(plain.read_bytes()));wrapped.append(path)
            np.testing.assert_array_equal(read_tvx_tiff(path).data,a)
        (self.root/'notes.txt.gz').write_bytes(gzip.compress(b'not a detector'))
        found={item.path.name for item in discover_image_files(self.root)}
        self.assertIn('signal.edf.gz',found);self.assertNotIn('notes.txt.gz',found)
        result=export_processed_batch(wrapped,self.root/'wrapper_exports')
        self.assertEqual(result['completed'],2)
        self.assertTrue(all(row['conversion'] for row in result['items']))

    def test_hdf_soft_links_virtual_data_and_precision_fail_closed(self):
        from saxs_waxs_core.background import apply_background,BackgroundRecipe,BackgroundError
        path=self.root/'soft.h5';a=np.arange(120).reshape(10,12)
        with h5py.File(path,'w') as f:
            f['raw']=a
            group=f.create_group('entry/data');group.attrs.update(NX_class='NXdata',signal='data')
            group['data']=h5py.SoftLink('/raw')
        np.testing.assert_array_equal(read_tvx_tiff(path).data,a)
        path=self.root/'virtual.h5'
        with h5py.File(path,'w',libver='latest') as f:
            f['raw']=a
            group=f.create_group('entry/data');group.attrs.update(NX_class='NXdata',signal='data')
            layout=h5py.VirtualLayout(a.shape,dtype=a.dtype)
            layout[:]=h5py.VirtualSource('.','/raw',shape=a.shape)
            group.create_virtual_dataset('data',layout)
        np.testing.assert_array_equal(read_tvx_tiff(path).data,a)
        path=self.root/'cycle.h5'
        with h5py.File(path,'w') as f:f['a']=h5py.SoftLink('/b');f['b']=h5py.SoftLink('/a')
        with self.assertRaisesRegex(TiffReaderError,'Cyclic'):source_catalog(path)
        path=self.root/'large-count.h5'
        a=np.array([[2**53+1,0],[3,4]],dtype='uint64');nexus(path,a)
        frame=read_tvx_tiff(path)
        self.assertEqual(int(frame.data[0,0]),2**53+1)
        with self.assertRaisesRegex(BackgroundError,'exact float64'):
            apply_background(frame,None,BackgroundRecipe(mode='no_background'))

    def test_bitshuffle_lz4_hdf_from_fresh_reader_process(self):
        import hdf5plugin
        import os,subprocess,sys
        a=np.arange(240,dtype='uint32').reshape(2,10,12)
        path=self.root/'bitshuffle.h5'
        with h5py.File(path,'w') as f:
            group=f.create_group('entry/data');group.attrs.update(NX_class='NXdata',signal='data')
            group.create_dataset('data',data=a,**hdf5plugin.Bitshuffle())
        result=subprocess.run([sys.executable,'-c',
            'import sys,numpy as n;from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff;'
            'n.testing.assert_array_equal(read_tvx_tiff(sys.argv[1],frame_index=1).data,n.arange(240,dtype="uint32").reshape(2,10,12)[1])',str(path)],
            env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONUTF8':'1'},capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unsupported_dataset_axes_units_and_unsafe_virtual_sources(self):
        path=self.root/'modules.h5'
        with h5py.File(path,'w') as f:
            group=f.create_group('entry/data');group.attrs.update(NX_class='NXdata',signal='data',axes=['module','y','x'])
            group['data']=np.arange(240).reshape(2,10,12)
        with self.assertRaises(TiffReaderError):read_tvx_tiff(path,frame_index=0)
        path=self.root/'virtual_escape.h5'
        with h5py.File(path,'w',libver='latest') as f:
            layout=h5py.VirtualLayout((10,12),dtype='uint32')
            layout[:]=h5py.VirtualSource(str(self.root.parent/'outside.h5'),'/data',shape=(10,12))
            f.create_virtual_dataset('data',layout)
        with self.assertRaisesRegex(TiffReaderError,'outside'):source_catalog(path)

    def test_stored_rate_units_are_preserved_without_renormalization(self):
        from saxs_waxs_core.background import BackgroundRecipe,BackgroundError,apply_background
        path=self.root/'rates.h5';a=np.arange(120,dtype=float).reshape(10,12)/7
        nexus(path,a)
        with h5py.File(path,'a') as file:file['entry/data/data'].attrs['units']='counts/s'
        frame=read_tvx_tiff(path)
        result=apply_background(frame,None,BackgroundRecipe(mode='no_background'))
        self.assertEqual(result.output_unit,'detector_count/s')
        np.testing.assert_array_equal(result.data,a)
        with self.assertRaises(BackgroundError):apply_background(frame,frame,BackgroundRecipe())
        exported=export_processed_batch([path],self.root/'rate_export')
        output=read_tvx_tiff(self.root/'rate_export'/exported['items'][0]['output'])
        expected=a.copy();expected[build_mask(frame).excluded]=np.nan
        np.testing.assert_array_equal(output.data,expected)
        self.assertEqual(output.metadata.intensity_unit,'detector_count/s')

    def test_detector_counter_names_are_discovered_by_content_not_examples(self):
        from saxs_waxs_core.discovery import discover_image_files
        from saxs_waxs_core.viewer import load_supported_frame
        a=np.arange(120,dtype='uint16').reshape(10,12)
        path=self.root/'measurement.0007';bruker86(path,a)
        (self.root/'plain.json').write_text('{"not_a_detector": 1}',encoding='utf-8')
        names={item.path.name for item in discover_image_files(self.root)}
        self.assertIn(path.name,names);self.assertNotIn('plain.json',names)
        np.testing.assert_array_equal(load_supported_frame(path).data,a)
