"""Portable numerical detector exports, with versioned in-file processing metadata.

Same container, not a rendered picture or a promise of byte-identical vendor tags.
TIFF uses IEEE float64; d*TREK specifies float IEEE (float32), explicitly audited.
Masked pixels are NaN, never zero or a negative sentinel that hides real negatives.
"""
from dataclasses import replace
from pathlib import Path
import base64
import hashlib
import json
import os
import struct
import tempfile
import numpy as np

from .background import BackgroundMode,BackgroundRecipe,apply_background
from .geometry import geometry_from_document,geometry_to_document
from .masking import MaskRecipe,build_mask,user_mask_layers_from_descriptors,load_external_mask_layer
from .models import ImageFrame,ImageMetadata

SCHEMA='saxs_waxs_processed_detector/1'
KEY='SWB_METADATA'
# Register by decoded format identity, not extension or an instrument filename.
WRITERS={'tvx_tiff_signed_int32_v1':'tiff','rigaku_tiff_uint32_v1':'tiff',
         'dtrek_uint32_v1':'dtrek','workbench_processed_tiff_v1':'tiff',
         'workbench_processed_dtrek_v1':'dtrek','dtrek_v1':'dtrek',
         'scientific_tiff_v1':'tiff','pilatus_tiff_v1':'tiff','marccd_tiff_v1':'tiff','rigaku_tiff_v1':'tiff',
         'edf_v1':'edf','hdf5_detector_v1':'nexus','workbench_processed_edf_v1':'edf',
         'workbench_processed_nexus_v1':'nexus'}


def output_spec(sample,output_format='source'):
    if output_format not in ('source','auto','tiff','nexus'):
        raise ValueError('Output format must be source, auto, tiff or nexus')
    container=WRITERS.get(sample.metadata.format_id)
    conversion=None
    if output_format in ('tiff','nexus'):
        container=output_format
        conversion=f'Explicit {output_format} numerical export'
    elif container is None and output_format=='auto':
        container='tiff'
        conversion=f'{sample.metadata.format_id}: reviewed writer cannot preserve floating intensity and NaN MASK; lossless TIFF selected'
    if container is None:
        raise ValueError(f'No validated same-format writer for {sample.metadata.format_id}; select lossless TIFF or NeXus')
    suffix={'tiff':'.tif','dtrek':'.img','edf':'.edf','nexus':'.nxs'}[container]
    original=sample.metadata.source_path.suffix.lower()
    if original in ('.gz','.bz2'):
        if output_format=='source':raise ValueError('Compressed-wrapper numerical writing is not validated; select lossless TIFF or NeXus')
        if output_format=='auto':
            container='tiff';suffix='.tif'
            conversion='External compression wrapper: use validated lossless TIFF numerical export'
    if container=='tiff' and original in ('.tif','.tiff'):suffix=original
    if container=='nexus' and original in ('.h5','.hdf5','.nxs','.nx5'):suffix=original
    if conversion is None and original!=suffix:
        conversion=f'Standardized {container} suffix for content-detected source (original suffix: {original})'
    return container,suffix,conversion


def embedded_geometry(frame):
    value=frame.metadata.embedded_processing.get('geometry') or frame.metadata.geometry_document
    return geometry_from_document(value) if value else None


def _payload(document):
    return base64.b64encode(json.dumps(document,ensure_ascii=True,allow_nan=False,
        separators=(',',':')).encode('ascii')).decode('ascii').rstrip('=')


def _file_hash(path):
    checksum=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def _decode(value):
    if len(value)>16*1024*1024:
        raise ValueError('Processed metadata exceeds size limit')
    document=json.loads(base64.b64decode(value+'='*((-len(value))%4),validate=True))
    if not isinstance(document,dict) or document.get('schema') != SCHEMA:
        raise ValueError('Unsupported processed detector metadata')
    return document


def read_processed_frame(path):
    """Return None for other profiles; malformed marked exports fail explicitly."""
    path=Path(path)
    from .readers.detector_io import is_hdf5
    if is_hdf5(path):
        import h5py
        with h5py.File(path,'r') as file:
            payload=file.attrs.get(KEY)
            if payload is None:return None
            document=_decode(str(payload))
            data=np.array(file['/entry/data/data'][()],copy=True)
            if '/entry/instrument/detector/pixel_mask' in file:
                mask=np.array(file['/entry/instrument/detector/pixel_mask'][()]!=0)
                if mask.shape!=data.shape or not np.array_equal(mask,np.isnan(data)):
                    raise ValueError('NeXus pixel_mask differs from the numerical MASK')
            header={KEY:str(payload)}
            container='nexus'
        return _processed_frame(path,data,header,document,container)
    with path.open('rb') as stream:
        prefix=stream.read(4096)
    if prefix.startswith(b'\x1f\x8b'):
        import gzip
        with gzip.open(path,'rb') as stream:prefix=stream.read(4096)
    elif prefix.startswith(b'BZh'):
        import bz2
        with bz2.open(path,'rb') as stream:prefix=stream.read(4096)
    if KEY.encode() not in prefix:
        return None
    import fabio
    image=fabio.open(str(path))
    try:
        if image.nframes!=1:
            raise ValueError('Processed detector schema requires exactly one raster per file')
        header=dict(image.header)
        data=np.array(image.data,copy=True)
        if type(image).__name__ == 'DtrekImage':
            document=_decode(header[KEY].strip())
            container='dtrek'
        elif type(image).__name__=='EdfImage':
            document=_decode(header[KEY].strip())
            container='edf'
        else:
            description=header.get('imageDescription','')
            line=next(line for line in description.splitlines() if line.startswith('# '+KEY+' '))
            document=_decode(line.split(' ',2)[2].strip())
            container='tiff'
    finally:
        image.close()
    return _processed_frame(path,data,header,document,container)


def _processed_frame(path,data,header,document,container):
    if document.get('shape') != list(data.shape) or data.ndim != 2:
        raise ValueError('Processed dimensions differ from embedded metadata')
    if data.dtype.kind != 'f' or str(data.dtype) != document.get('dtype'):
        raise ValueError('Processed pixel type differs from embedded metadata')
    if int(np.count_nonzero(np.isnan(data))) != document.get('masked_pixel_count') or np.isinf(data).any():
        raise ValueError('Processed MASK does not match embedded metadata')
    geometry=document.get('geometry')
    if geometry:
        g=geometry_from_document(geometry)
        if (g.image_height_px,g.image_width_px) != data.shape:
            raise ValueError('Embedded geometry dimensions differ from raster')
    else:
        g=None
    # No detector cutoff or negative sentinel applies to corrected intensities.
    return ImageFrame(data,ImageMetadata(source_path=path.absolute(),
        format_id=f'workbench_processed_{container}_v1',support_level='quantitative_candidate',
        width_px=data.shape[1],height_px=data.shape[0],dtype=str(data.dtype),
        pixel_size_x_m=g.pixel_size_x_m if g else document.get('pixel_size_x_m'),
        pixel_size_y_m=g.pixel_size_y_m if g else document.get('pixel_size_y_m'),
        exposure_time_s=document.get('sample_exposure_s'),software='XRelay',
        raw_tags=header,embedded_processing=document,intensity_unit=document['intensity_unit']))


def _write_tiff(path,data,description):
    """Single-strip baseline TIFF with IEEE float64, native row/column order.

    FabIO's installed writer silently casts float64 to float32, so write the small
    standard IFD directly. Independent FabIO decoding validates this writer.
    """
    desc=description.encode('ascii')+b'\0'
    tags=[(256,4,1,data.shape[1]),(257,4,1,data.shape[0]),(258,3,1,64),
          (259,3,1,1),(262,3,1,1),(270,2,len(desc),0),(273,4,1,0),
          (274,3,1,1),(277,3,1,1),(278,4,1,data.shape[0]),
          (279,4,1,data.nbytes),(339,3,1,3)]
    desc_offset=8+2+12*len(tags)+4
    offset=desc_offset+len(desc)
    if offset+data.nbytes>=2**32:
        raise ValueError('Classic TIFF exceeds 4 GiB; split this dataset before exporting')
    tags=[(t,k,n,desc_offset if t==270 else offset if t==273 else v) for t,k,n,v in tags]
    with path.open('wb') as stream:
        stream.write(b'II*\0'+struct.pack('<I',8)+struct.pack('<H',len(tags)))
        for tag in tags:
            stream.write(struct.pack('<HHII',*tag))
        stream.write(b'\0'*4+desc)
        stream.write(np.asarray(data,dtype='<f8').tobytes())


def _description(document,geometry):
    text='# '+KEY+' '+_payload(document)+'\n'
    if geometry:
        text+=f'# Pixel_size {geometry.pixel_size_x_m:.17g} m x {geometry.pixel_size_y_m:.17g} m\n'
        text+=f'# Detector_distance {geometry.detector_distance_m:.17g} m\n'
        text+=f'# Wavelength {geometry.wavelength_m*1e10:.17g} A\n'
        text+=f'# Beam_xy ({geometry.beam_center_x_px-.5:.17g}, {geometry.beam_center_y_px-.5:.17g}) pixels\n'
    text+='# Intensity_unit '+document['intensity_unit']+'\n# Masked_pixels NaN; exclude nonfinite pixels\n'
    return text


def _write_nexus(path,values,document,geometry):
    import h5py
    with h5py.File(path,'w') as file:
        file.attrs[KEY]=_payload(document)
        file.attrs['default']='entry'
        entry=file.create_group('entry');entry.attrs.update(NX_class='NXentry',default='data')
        process=entry.create_group('process');process.attrs['NX_class']='NXprocess'
        process['program']='XRelay'
        process['processing_metadata']=json.dumps(document,ensure_ascii=True,allow_nan=False)
        signal=entry.create_group('data');signal.attrs.update(NX_class='NXdata',signal='data')
        signal.create_dataset('data',data=values,compression='gzip',shuffle=True).attrs['units']=document['intensity_unit']
        instrument=entry.create_group('instrument');instrument.attrs['NX_class']='NXinstrument'
        detector=instrument.create_group('detector');detector.attrs['NX_class']='NXdetector'
        detector['data']=signal['data']
        detector.create_dataset('pixel_mask',data=np.isnan(values).astype('uint32'))
        if geometry:
            detector.attrs['center_convention']='canonical_top_left_edge'
            for key,value,unit in [('distance',geometry.detector_distance_m,'m'),
                ('x_pixel_size',geometry.pixel_size_x_m,'m'),('y_pixel_size',geometry.pixel_size_y_m,'m'),
                ('beam_center_x',geometry.beam_center_x_px,'pixel'),('beam_center_y',geometry.beam_center_y_px,'pixel')]:
                detector.create_dataset(key,data=value).attrs['units']=unit
            beam=instrument.create_group('beam');beam.attrs['NX_class']='NXbeam'
            beam.create_dataset('incident_wavelength',data=geometry.wavelength_m).attrs['units']='m'


def export_processed_frame(sample,path,*,background=None,background_recipe=None,mask_recipe=None,
                           geometry=None,protected_roots=(),output_format='source',prefer_embedded_geometry=True):
    """Export one explicit snapshot; never overwrite any existing path."""
    path=Path(path).expanduser().resolve()
    protected=(Path(__file__).resolve().parents[2]/'data',*protected_roots)
    for root in protected:
        root=Path(root).resolve()
        if path==root or root in path.parents:
            raise ValueError(f'refusing to write inside protected root: {root}')
    if path==sample.metadata.source_path.resolve() or path.exists():
        raise FileExistsError(path)
    container,expected_suffix,conversion=output_spec(sample,output_format)
    if output_format=='source' and path.suffix.lower()!=sample.metadata.source_path.suffix.lower():
        raise ValueError('Keep the source file extension for numerical detector export')
    if path.suffix.lower()!=expected_suffix:
        raise ValueError(f'The selected numerical container requires {expected_suffix}, not {path.suffix}')
    if container=='tiff' and path.suffix.lower() not in ('.tif','.tiff'):
        raise ValueError('Decoded TIFF requires a TIFF extension; source suffix is inconsistent')
    if container=='dtrek' and path.suffix.lower()!='.img':
        raise ValueError('Decoded d*TREK requires an IMG extension')
    geometry=(embedded_geometry(sample) or geometry) if prefer_embedded_geometry else geometry
    if geometry is not None:
        geometry=replace(geometry,image_width_px=geometry.image_width_px or sample.data.shape[1],
            image_height_px=geometry.image_height_px or sample.data.shape[0])
    if geometry and (geometry.image_height_px,geometry.image_width_px)!=sample.data.shape:
        raise ValueError('Geometry dimensions differ from this sample')
    recipe=background_recipe or BackgroundRecipe(mode=BackgroundMode.NO_BACKGROUND)
    if background is not None and background.metadata.source_path.resolve()==sample.metadata.source_path.resolve():
        raise ValueError('air background cannot also be a batch sample')
    corrected=apply_background(sample,background,recipe)
    masks=build_mask(sample,mask_recipe or MaskRecipe())
    excluded=np.array(masks.excluded,copy=True)
    if background is not None and recipe.mode==BackgroundMode.AUTO_AIR:
        excluded |= build_mask(background).excluded
    excluded |= ~np.isfinite(corrected.data)
    values=np.array(corrected.data,dtype='float32' if container=='dtrek' else 'float64',copy=True)
    if np.any(~np.isfinite(values) & ~excluded):
        raise ValueError('Intensity overflows output pixel type; file not written')
    values[excluded]=np.nan
    upstream=dict(sample.metadata.embedded_processing)
    document={'schema':SCHEMA,'shape':list(values.shape),'dtype':str(values.dtype),
        'source_path':str(sample.metadata.source_path),'source_format':sample.metadata.format_id,
        'native_raster_format':upstream.get('native_raster_format',sample.metadata.format_id),
        'source_sha256':_file_hash(sample.metadata.source_path),
        'geometry':geometry_to_document(geometry) if geometry else None,
        'pixel_size_x_m':sample.metadata.pixel_size_x_m,'pixel_size_y_m':sample.metadata.pixel_size_y_m,
        'sample_exposure_s':corrected.sample_exposure_s,'intensity_unit':corrected.output_unit,
        'background_applied':recipe.mode==BackgroundMode.AUTO_AIR or bool(upstream.get('background_applied')),
        'background':dict(corrected.audit),'mask':dict(masks.audit),'masked_pixel_count':int(excluded.sum()),
        'mask_encoding':'NaN means excluded; finite negative intensity is valid',
        'source_selection':dict(dataset=sample.metadata.dataset_path,frame_index=sample.metadata.frame_index,frame_count=sample.metadata.frame_count),
        'format_conversion':conversion,
        'geometry_preference':'file_header' if prefer_embedded_geometry else 'explicit_override',
        'coordinate_convention':'top_left_edge_xy_right_down_pixel_center_plus_0p5',
        'precision':'float32 (d*TREK float IEEE)' if container=='dtrek' else 'float64',
        'max_storage_absolute_error':float(np.max(np.abs(values[~excluded].astype(float)-corrected.data[~excluded]))) if (~excluded).any() else 0.0}
    if upstream:
        document['upstream_background']=upstream.get('background')
    path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(dir=path.parent,suffix=path.suffix,prefix='.processed_')
    os.close(handle)
    temporary=Path(name)
    try:
        if container=='tiff':
            _write_tiff(temporary,values,_description(document,geometry))
        elif container=='nexus':
            _write_nexus(temporary,values,document,geometry)
        elif container=='edf':
            from fabio.edfimage import EdfImage
            EdfImage(values,header={KEY:_payload(document),'Intensity_unit':document['intensity_unit']}).write(str(temporary))
        else:
            from fabio.dtrekimage import DtrekImage
            # Marker first: bounded sniffing remains independent of vendor header size.
            header={KEY:_payload(document),**{k:v for k,v in sample.metadata.raw_tags.items() if k not in (KEY,'HEADER_BYTES')}}
            if geometry:
                header.update(SWB_CENTER_X_PX=str(geometry.beam_center_x_px),SWB_CENTER_Y_PX=str(geometry.beam_center_y_px),
                    SWB_PIXEL_X_M=str(geometry.pixel_size_x_m),SWB_PIXEL_Y_M=str(geometry.pixel_size_y_m),
                    SWB_SDD_M=str(geometry.detector_distance_m),SWB_WAVELENGTH_M=str(geometry.wavelength_m))
            DtrekImage(values,header=header).write(str(temporary))
        restored=read_processed_frame(temporary)
        if restored is None or not np.array_equal(restored.data,values,equal_nan=True):
            raise ValueError('Numerical detector export failed round-trip validation')
        with temporary.open('r+b') as stream:
            stream.flush()
            os.fsync(stream.fileno())
        # Publish the verified file in one operation, refusing any competing output.
        if os.name=='nt':
            os.rename(temporary,path)
        else:
            os.link(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
    return document


def export_processed_batch(paths,directory,*,background_path=None,background_recipe=None,
        mask_recipe=None,manual_masks=(),external_mask_path=None,geometry=None,
        protected_roots=(),stop_requested=lambda:False,progress=lambda value:None,
        output_format='auto',dataset_selections=None,frame_selections=None,background_selection=None,
        prefer_embedded_geometry=True,frame_choices=None):
    from .readers.tvx_tiff import read_tvx_tiff
    directory=Path(directory).resolve()
    for root in (Path(__file__).resolve().parents[2]/'data',*protected_roots):
        root=Path(root).resolve()
        if directory==root or root in directory.parents:
            raise ValueError(f'refusing to write inside protected root: {root}')
    paths=tuple(Path(p).resolve() for p in paths)
    rows=[]
    air_selection=background_selection or (background_recipe.metadata.get('source_selection',{}) if background_recipe else {})
    background=read_tvx_tiff(background_path,**air_selection) if background_path else None
    directory.mkdir(parents=True,exist_ok=False)
    stream=(directory/'processing_info.txt').open('x',encoding='utf-8')
    try:
        stream.write('Processed numerical detector files / 处理后二维数据\n'
            'Masked pixels: NaN (exclude nonfinite); finite negatives preserved.\n'
            'Original container preferred; explicit safe conversion recorded below.\n'
            'TIFF: float64. d*TREK IMG: float32; storage error reported.\n'
            'Geometry and units embedded. Third-party metadata/MASK support varies.\n')
        from .readers.detector_io import source_selections,inspect_sources,dependency_snapshot,verify_dependencies,filter_frame_selections
        paths,catalogs,source_errors,excluded=inspect_sources(paths)
        for item in excluded:
            stream.write(json.dumps(dict(item,status='excluded_referenced_chunk'),ensure_ascii=False)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
        snapshots={key:dependency_snapshot(value) for key,value in catalogs.items()}
        sources=[]
        for path in paths:
            try:
                dataset=(dataset_selections or {}).get(str(path))
                explicit=(frame_selections or {}).get(str(path))
                selections=[explicit] if explicit is not None else source_selections(path,dataset=dataset)
                if explicit is None:
                    selections=filter_frame_selections(selections,(frame_choices or {}).get(str(path)))
                sources.extend((path,selection,None) for selection in selections)
            except Exception as exc:
                sources.append((path,{},str(exc)))
        for number,(path,selection,error) in enumerate(sources,1):
            row={'source':str(path),'status':'not_processed'}
            row.update(selection)
            if not stop_requested():
                try:
                    if error:raise ValueError(error)
                    verify_dependencies(snapshots.get(str(path),[]))
                    sample=read_tvx_tiff(path,**selection)
                    layers=list((mask_recipe or MaskRecipe()).user_layers)
                    layers.extend(user_mask_layers_from_descriptors(sample.data.shape,manual_masks))
                    if external_mask_path:
                        layers.append(load_external_mask_layer(external_mask_path,sample.data.shape))
                    mask=replace(mask_recipe or MaskRecipe(),user_layers=tuple(layers))
                    container,suffix,conversion=output_spec(sample,output_format)
                    frame_label=f'_frame{sample.metadata.frame_index+1:06d}' if sample.metadata.frame_count>1 else ''
                    destination=directory/f'{number:04d}_{path.stem}{frame_label}_processed{suffix}'
                    document=export_processed_frame(sample,destination,background=background,
                        background_recipe=background_recipe,mask_recipe=mask,geometry=geometry,protected_roots=protected_roots,
                        output_format=output_format,prefer_embedded_geometry=prefer_embedded_geometry)
                    row.update(status='completed',output=destination.name,dtype=document['dtype'],
                        unit=document['intensity_unit'],geometry='embedded' if document['geometry'] else 'unknown',
                        masked=document['masked_pixel_count'],storage_error=document['max_storage_absolute_error'],conversion=conversion)
                except Exception as exc:
                    row.update(status='failed',reason=str(exc))
            rows.append(row)
            # Persist each completed record before reporting progress to the caller.
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
            progress({'completed':number,'total':len(sources),'sample':path.name,'status':row['status']})
    finally:
        stream.close()
    return {'directory':str(directory),'items':rows,'completed':sum(r['status']=='completed' for r in rows),
            'failed':sum(r['status']=='failed' for r in rows)}
