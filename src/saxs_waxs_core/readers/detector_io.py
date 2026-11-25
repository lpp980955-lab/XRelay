"""Content-driven detector adapters with explicit stack and dataset selection.

FabIO supplies native detector codecs; h5py supplies bounded local HDF5 access.
No filename-specific geometry, implicit frame summation, or raster mirroring.
"""
from dataclasses import replace
from pathlib import Path
import math
import re
import posixpath
import struct
import numpy as np
from ..models import ImageFrame,ImageMetadata
from ..geometry import DetectorGeometry,GeometryProvenance,geometry_to_document
from .tvx_tiff import TiffReaderError,UnsupportedTiffError,CorruptImageError

SUPPORTED_SUFFIXES=frozenset({'.tif','.tiff','.img','.gfrm','.sfrm','.cbf','.edf','.h5','.hdf5','.nxs','.nx5'})
FABIO_PROFILES={'BrukerImage':'bruker86','Bruker100Image':'bruker100','CbfImage':'cbf',
    'EdfImage':'edf','TifImage':'scientific_tiff','PilatusImage':'pilatus_tiff',
    'MarccdImage':'marccd_tiff','AdscImage':'adsc','DtrekImage':'dtrek'}


def detector_base_suffix(path):
    path=Path(path)
    return Path(path.stem).suffix.lower() if path.suffix.lower() in ('.gz','.bz2') else path.suffix.lower()


def supported_detector_path(path):
    path=Path(path)
    if path.suffix.lower() in ('.gz','.bz2'):
        return detector_base_suffix(path) in SUPPORTED_SUFFIXES-{'.h5','.hdf5','.nxs','.nx5'}
    if path.suffix.lower() in SUPPORTED_SUFFIXES:return True
    from ..discovery import has_detector_signature
    return has_detector_signature(path)


class SourceSelectionRequired(TiffReaderError):
    def __init__(self,message,choices):
        super().__init__(message)
        self.choices=choices


def _internal_link(file,path,seen=None):
    import h5py
    seen=set() if seen is None else seen
    path=posixpath.normpath(path)
    if path in seen or len(seen)>24:
        raise UnsupportedTiffError('Cyclic HDF5 soft link')
    seen=seen|{path}
    parts=[p for p in path.split('/') if p]
    group=file['/']
    for i,part in enumerate(parts):
        edge=group.get(part,getlink=True)
        if isinstance(edge,h5py.ExternalLink):
            raise UnsupportedTiffError('HDF5 soft-link chains through external files require an explicit adapter')
        if isinstance(edge,h5py.SoftLink):
            target=edge.path if edge.path.startswith('/') else group.name+'/'+edge.path
            target=posixpath.join(target,*parts[i+1:])
            return _internal_link(file,target,seen)
        group=group[part]
    return group


def _validate_virtual(dataset,path):
    if dataset.is_virtual:
        for source in dataset.virtual_sources():
            name=_text(source.file_name)
            if name in ('.',''):continue
            target=(Path(dataset.file.filename).parent/name).resolve()
            if Path(path).resolve()!=target and Path(path).resolve().parent not in target.parents:
                raise UnsupportedTiffError('Virtual dataset reads outside master directory')


def _tiff_orientation(path,frame_index=0):
    opener=open
    with Path(path).open('rb') as source:magic=source.read(3)
    if magic[:2]==b'\x1f\x8b':
        import gzip
        opener=gzip.open
    elif magic==b'BZh':
        import bz2
        opener=bz2.open
    with opener(path,'rb') as stream:
        start=stream.read(16)
        endian='<' if start[:2]==b'II' else '>'
        version=struct.unpack(endian+'H',start[2:4])[0]
        big=version==43
        offset=struct.unpack(endian+('Q' if big else 'I'),start[8:16] if big else start[4:8])[0]
        for index in range(frame_index+1):
            if offset==0:raise UnsupportedTiffError('TIFF frame does not exist')
            stream.seek(offset)
            count=struct.unpack(endian+('Q' if big else 'H'),stream.read(8 if big else 2))[0]
            if count>4096:raise UnsupportedTiffError('TIFF directory exceeds metadata limit')
            orientation=1
            for _ in range(count):
                entry=stream.read(20 if big else 12)
                tag,kind=struct.unpack(endian+'HH',entry[:4])
                if tag==274:
                    amount=struct.unpack(endian+('Q' if big else 'I'),entry[4:12] if big else entry[4:8])[0]
                    if kind!=3 or amount!=1:raise UnsupportedTiffError('Invalid TIFF Orientation tag')
                    orientation=struct.unpack(endian+'H',entry[12:14] if big else entry[8:10])[0]
            if index==frame_index:return orientation
            offset=struct.unpack(endian+('Q' if big else 'I'),stream.read(8 if big else 4))[0]
    return 1


def _text(value):
    if isinstance(value,bytes):
        return value.decode('utf-8',errors='strict')
    return str(value)


def is_hdf5(path):
    # HDF5 signatures may follow a power-of-two user block. Keep ordinary TIFF
    # preview independent of h5py, and never scan detector pixel data for magic.
    with Path(path).open('rb') as stream:
        start=stream.read(16)
        if start[:8]==b'\x89HDF\r\n\x1a\n':return True
        if start.startswith((b'II',b'MM',b'FORMAT',b'###CBF',b'{',b'\r\n{')):return False
        size=stream.seek(0,2);offset=512
        while offset+8<=size:
            stream.seek(offset)
            if stream.read(8)==b'\x89HDF\r\n\x1a\n':return True
            offset*=2
    return False


def _hdf_catalog(path):
    """Traverse hard/soft links safely; local Eiger external links stay in root.

    Do not use visititems (it omits external links) or follow arbitrary links to
    files outside the chosen master file's directory. Cyclic groups are bounded.
    """
    import h5py
    path=Path(path).resolve()
    result=[]
    with h5py.File(path,'r') as file:
        def walk(group,prefix,ancestors,depth=0):
            if depth>24 or len(result)>10000:
                raise UnsupportedTiffError('HDF5 hierarchy exceeds safe discovery limits')
            token=(group.file.filename,h5py.h5o.get_info(group.id).addr)
            if token in ancestors:
                return
            ancestors=ancestors|{token}
            nx=_text(group.attrs.get('NX_class',''))
            signal=_text(group.attrs.get('signal',''))
            for name in group.keys():
                link=group.get(name,getlink=True)
                if isinstance(link,h5py.ExternalLink):
                    target=(Path(group.file.filename).parent/link.filename).resolve()
                    if path.parent!=target.parent and path.parent not in target.parents:
                        raise UnsupportedTiffError('HDF5 external link leaves the master data directory')
                if isinstance(link,h5py.SoftLink):
                    target=link.path if link.path.startswith('/') else group.name+'/'+link.path
                    obj=_internal_link(group.file,target)
                else:
                    try:
                        obj=group[name]
                    except (OSError,KeyError) as exc:
                        raise CorruptImageError(f'HDF5 link cannot be resolved: {prefix}/{name}: {exc}') from exc
                key=prefix+'/'+name
                if isinstance(obj,h5py.Group):
                    walk(obj,key,ancestors,depth+1)
                elif isinstance(obj,h5py.Dataset):
                    _validate_virtual(obj,path)
                    if obj.ndim not in (2,3) or obj.dtype.kind not in 'iuf':continue
                    if name in {'pixel_mask','mask','flatfield','flat_field','dark','darkfield','errors','error'}:
                        continue
                    axes=group.attrs.get('axes',[])
                    if isinstance(axes,(str,bytes)):
                        axes=_text(axes).split(':')
                    leading=_text(axes[0]).lower() if len(axes) else ''
                    if obj.ndim==3 and leading in {'module','modules','channel','energy'}:
                        continue
                    if np.prod(obj.shape[-2:],dtype=object)*obj.dtype.itemsize>512*1024**2:
                        raise UnsupportedTiffError('Detector frame exceeds the current 512 MiB raster limit')
                    result.append(dict(dataset=key,shape=list(obj.shape),dtype=str(obj.dtype),
                        frames=int(obj.shape[0]) if obj.ndim==3 else 1,
                        signal=(name==signal or str(obj.attrs.get('signal',''))=='1'),
                        nxdata=nx=='NXdata',axes=[_text(a) for a in axes],
                        storage_file=str(Path(obj.file.filename).resolve()),storage_object=int(h5py.h5o.get_info(obj.id).addr)))
        walk(file,'',set())
    if not result:
        raise UnsupportedTiffError('No numeric 2D detector image or frame/y/x stack found in HDF5')
    return result


def source_catalog(path):
    path=Path(path).expanduser().resolve()
    if is_hdf5(path):
        return _hdf_catalog(path)
    import fabio
    image=fabio.open(str(path))
    try:
        name=type(image).__name__
        if name not in FABIO_PROFILES:
            raise UnsupportedTiffError(f'No reviewed detector adapter for FabIO codec {name}')
        data=image.data
        if data is None or data.ndim!=2 or data.dtype.kind not in 'iuf':
            raise UnsupportedTiffError('Only scalar numeric detector rasters are supported; RGB/complex data are not intensity images')
        return [dict(dataset=None,shape=list(data.shape),dtype=str(data.dtype),frames=int(image.nframes),
                     signal=True,nxdata=False,codec=name)]
    finally:
        image.close()


def default_datasets(catalog):
    unique={}
    for item in catalog:
        key=(item.get('storage_file'),item.get('storage_object',item['dataset']))
        old=unique.get(key)
        if old is None or (item.get('signal',False),item.get('nxdata',False))>(old.get('signal',False),old.get('nxdata',False)):
            unique[key]=item
    catalog=list(unique.values())
    signals=[item for item in catalog if item.get('signal')]
    if len(signals)==1:
        return signals
    if len(catalog)==1:
        return catalog
    # Eiger/LIMA chunk sequence: the same NXdata parent and raster shape.
    if all(item.get('nxdata') and len(item['shape'])==3 for item in catalog):
        parents={item['dataset'].rsplit('/',1)[0] for item in catalog}
        shapes={tuple(item['shape'][-2:]) for item in catalog}
        if len(parents)==len(shapes)==1:
            return sorted(catalog,key=lambda item:item['dataset'])
    raise SourceSelectionRequired('Multiple HDF5 detector datasets: select one explicitly',catalog)


def source_selections(path,*,dataset=None,all_frames=True):
    catalog=source_catalog(path)
    return _selections(catalog,dataset,all_frames)


def _selections(catalog,dataset=None,all_frames=True):
    chosen=[item for item in catalog if item['dataset']==dataset] if dataset is not None else default_datasets(catalog)
    if not chosen:
        raise UnsupportedTiffError(f'Detector dataset not found: {dataset}')
    if sum(item['frames'] for item in chosen)>100000:
        raise UnsupportedTiffError('More than 100000 frames; select a smaller dataset before processing')
    result=[]
    for item in chosen:
        for number in range(item['frames'] if all_frames else 1):
            selection={}
            if item['dataset'] is not None:
                selection['dataset']=item['dataset']
            if item['frames']>1:
                selection['frame_index']=number
            result.append(selection)
    return result


def dependency_snapshot(catalog):
    result=[]
    for path in sorted({item['storage_file'] for item in catalog if item.get('storage_file')}):
        stat=Path(path).stat()
        result.append(dict(path=path,size_bytes=stat.st_size,mtime_ns=stat.st_mtime_ns))
    return result


def verify_dependencies(snapshot):
    for item in snapshot:
        current=Path(item['path']).stat()
        if current.st_size!=item['size_bytes'] or current.st_mtime_ns!=item['mtime_ns']:
            raise CorruptImageError(f"Detector dataset dependency changed after selection: {item['path']}")


def inspect_sources(paths):
    """Detect explicit master/chunk relationships, not equal-valued exposures."""
    paths=[Path(path).resolve() for path in paths]
    catalogs={};errors={}
    for path in paths:
        try:catalogs[str(path)]=source_catalog(path)
        except Exception as exc:errors[str(path)]=exc
    referenced={}
    for master,items in catalogs.items():
        for item in items:
            storage=item.get('storage_file')
            if storage and storage!=master:referenced[storage]=master
    excluded=[dict(path=str(path),reason='Referenced chunk; processed through selected master',master=referenced[str(path)])
        for path in paths if str(path) in referenced and referenced[str(path)] in catalogs]
    skip={item['path'] for item in excluded}
    return [path for path in paths if str(path) not in skip],catalogs,errors,excluded


def _index(index,count):
    if index is None:
        if count>1:
            raise SourceSelectionRequired(f'This container has {count} frames; select a frame explicitly',
                [dict(dataset=None,frames=count)])
        return 0
    if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<count:
        raise UnsupportedTiffError(f'Frame index must be an integer from 0 to {count-1}')
    return index


def _geometry(fields,shape,source,convention=None):
    required=('wavelength_m','detector_distance_m','pixel_size_x_m','pixel_size_y_m','beam_center_x_px','beam_center_y_px')
    if not all(k in fields and fields[k] is not None for k in required) or convention is None:
        return {}
    values={key:float(fields[key]) for key in required}
    if convention=='zero_based_pixel_centers':
        values['beam_center_x_px']+=.5;values['beam_center_y_px']+=.5
    elif convention!='canonical_top_left_edge':
        return {}
    from ..geometry import GeometryError
    try:
        return geometry_to_document(DetectorGeometry(**values,image_height_px=shape[0],image_width_px=shape[1],
            provenance=GeometryProvenance(method='instrument_header',source_path=str(source),
                source_convention=convention,notes='Explicit units and center convention; native raster retained')))
    except GeometryError:
        return {}  # Preserve pixel access and raw fields; do not apply an unsupported geometry.


def _header_fields(header,profile):
    """Conservative metadata: explicit units or a reviewed format-specific field.

    Ambiguous beam centers remain visible in raw_tags, never auto-calibrated.
    """
    fields={}
    if profile in ('bruker86','bruker100'):
        for key,target,scale in [('CUMULAT','exposure_time_s',1.),('DISTANC','detector_distance_m',.01),
                                  ('WAVELEN','wavelength_m',1e-10)]:
            if key in header:
                value=float(str(header[key]).split()[0])*scale
                if math.isfinite(value) and value>0:fields[target]=value
    text='\n'.join(f'{k} {v}' for k,v in header.items())
    quantities={'pixel_size_x_m':r'Pixel_size\s+([\d.eE+-]+)\s*m\s*x',
                'pixel_size_y_m':r'Pixel_size\s+[\d.eE+-]+\s*m\s*x\s*([\d.eE+-]+)\s*m',
                'exposure_time_s':r'Exposure_time\s+([\d.eE+-]+)\s*s',
                'wavelength_m':r'Wavelength\s+([\d.eE+-]+)\s*(A|angstrom|nm|m)\b',
                'detector_distance_m':r'Detector_distance\s+([\d.eE+-]+)\s*(mm|m)\b'}
    for key,pattern in quantities.items():
        found=re.search(pattern,text,re.I)
        if found:
            value=float(found[1])
            unit=found[2].lower() if len(found.groups())>1 else ''
            value*=dict(a=1e-10,angstrom=1e-10,nm=1e-9,mm=1e-3).get(unit,1)
            if math.isfinite(value) and value>0:
                fields[key]=value
    # Explicit portable scalar header keys, useful in EDF and site-generated IMG.
    for key in ('pixel_size_x_m','pixel_size_y_m','wavelength_m','detector_distance_m',
                'beam_center_x_px','beam_center_y_px','exposure_time_s'):
        if key in header:
            value=float(header[key])
            if not math.isfinite(value):
                raise CorruptImageError(f'Nonfinite detector metadata: {key}')
            fields[key]=value
    return fields


def _read_fabio(path,frame_index=None):
    import fabio
    image=fabio.open(str(path))
    try:
        codec=type(image).__name__
        profile=FABIO_PROFILES.get(codec)
        if profile is None:
            raise UnsupportedTiffError(f'Unreviewed detector codec: {codec}')
        count=int(image.nframes)
        index=_index(frame_index,count)
        if profile.endswith('tiff') and _tiff_orientation(path,index)!=1:
            raise UnsupportedTiffError('Non-top-left TIFF orientation requires an explicit orientation adapter')
        current=image.getframe(index) if index else image
        try:
            data=np.array(current.data,copy=True)
            header=dict(current.header)
        finally:
            if current is not image and hasattr(current,'close'):
                current.close()
        if data.ndim!=2 or data.dtype.kind not in 'iuf':
            raise UnsupportedTiffError('Expected numeric scalar 2D detector data, not RGB/complex data')
        if profile=='scientific_tiff' and str(header.get('imageDescription','')).strip('\x00 ')=='Rigaku 2D image':
            profile='rigaku_tiff'
        if profile=='scientific_tiff' and int(header.get('orientation',1))!=1:
            raise UnsupportedTiffError('Non-top-left TIFF orientation requires an explicit orientation adapter')
        if profile=='bruker100' and 'LINEAR' in header:
            slope,offset=map(float,header['LINEAR'].split()[:2])
            if not (math.isfinite(slope) and math.isfinite(offset)):
                raise CorruptImageError('Invalid Bruker LINEAR calibration')
            # Installed Bruker100 codec returns integer codes even with LINEAR.
            data=data.astype(float)*slope+offset
        fields=_header_fields(header,profile)
        found=re.search(r'Count_cutoff\s+(\d+)\s+counts','\n'.join(str(v) for v in header.values()),re.I)
        cutoff=int(found[1]) if found else None
        geometry=_geometry(fields,data.shape,path,header.get('center_convention'))
        mask=~np.isfinite(data)
        if ((profile in ('pilatus_tiff','cbf') and 'PILATUS' in str(header).upper())
                or 'TVX TIFF' in str(header.get('software',''))) and data.dtype.kind=='i':
            mask |= data<0
        if cutoff is not None:mask |= data>=cutoff
        return ImageFrame(data,ImageMetadata(source_path=Path(path),format_id=f'{profile}_v1',
            support_level='quantitative_candidate',width_px=data.shape[1],height_px=data.shape[0],dtype=str(data.dtype),
            pixel_size_x_m=fields.get('pixel_size_x_m'),pixel_size_y_m=fields.get('pixel_size_y_m'),
            exposure_time_s=fields.get('exposure_time_s'),raw_tags={**header,'geometry_fields':fields},geometry_document=geometry,
            detector_mask=mask,dataset_path=None,frame_index=index,frame_count=count,
            count_cutoff=cutoff,intensity_unit=header.get('intensity_unit',header.get('Intensity_unit'))))
    finally:
        image.close()


_LENGTH={'m':1.,'mm':1e-3,'um':1e-6,'µm':1e-6,'nm':1e-9,'angstrom':1e-10,'a':1e-10,'å':1e-10}


def _read_hdf(path,dataset=None,frame_index=None):
    import h5py
    try:
        import hdf5plugin  # Already locked/audited private-prototype dependency.
    except ModuleNotFoundError:
        pass  # Missing custom filters fail explicitly when that dataset is read.
    catalog=_hdf_catalog(path)
    if dataset is None:
        choices=default_datasets(catalog)
        if len(choices)>1:
            raise SourceSelectionRequired('Select the HDF5 dataset chunk to preview; batch expands all chunks',catalog)
        selected=choices[0]
    else:
        selected=next((item for item in catalog if item['dataset']==dataset),None)
        if selected is None:
            raise UnsupportedTiffError(f'Dataset is not a reviewed detector raster: {dataset}')
    index=_index(frame_index,selected['frames'])
    with h5py.File(path,'r') as file:
        ds=file[selected['dataset']]
        _validate_virtual(ds,path)
        data=np.array(ds[index] if ds.ndim==3 else ds[()],copy=True)
        unit=_text(ds.attrs['units']).strip() if 'units' in ds.attrs else None
        unit={'counts':'detector_count','count':'detector_count','counts/s':'detector_count/s',
              'count/s':'detector_count/s','cps':'detector_count/s','counts s-1':'detector_count/s'}.get(unit,unit)
        # Find the NXdetector owning this signal, or one unique detector group.
        detectors=[]
        def find(name,obj):
            if isinstance(obj,h5py.Group) and _text(obj.attrs.get('NX_class',''))=='NXdetector':
                detectors.append(obj.name)
        file.visititems(find)
        owners=[name for name in detectors if selected['dataset'].startswith(name+'/')]
        detector=file[owners[0] if owners else detectors[0]] if owners or len(detectors)==1 else None
        fields={};notes=[]
        def scalar(group,key,kind='length'):
            if group is None or key not in group:
                return None
            obj=group[key]
            if obj.size!=1:
                if kind=='time' and obj.shape==(selected['frames'],):
                    raw=float(obj[index])
                else:
                    notes.append(f'{obj.name}: non-scalar metadata not assumed constant');return None
            else:
                raw=float(np.asarray(obj[()]).reshape(-1)[0])
            units=_text(obj.attrs.get('units','')).lower()
            scale=({'s':1,'ms':.001,'us':1e-6} if kind=='time' else _LENGTH).get(units)
            if scale is None:
                notes.append(f'{obj.name}: missing or unsupported units {units!r}');return None
            if not math.isfinite(raw):
                raise CorruptImageError(f'Nonfinite HDF metadata: {obj.name}')
            return raw*scale
        for key,target in [('x_pixel_size','pixel_size_x_m'),('y_pixel_size','pixel_size_y_m'),('distance','detector_distance_m')]:
            value=scalar(detector,key)
            if value is not None:fields[target]=value
        exposure=scalar(detector,'count_time','time')
        for candidate in ('/entry/instrument/beam','/entry/beam','/entry/sample/beam'):
            if candidate in file:
                wavelength=scalar(file[candidate],'incident_wavelength')
                if wavelength is not None:fields['wavelength_m']=wavelength
        convention=None
        if detector is not None:
            convention=_text(detector.attrs.get('center_convention','')) or None
            for axis in ('x','y'):
                key='beam_center_'+axis
                if key in detector:
                    obj=detector[key]
                    if obj.size!=1:continue
                    units=_text(obj.attrs.get('units','')).lower()
                    pixel=fields.get('pixel_size_'+axis+'_m')
                    if units in ('pixel','pixels','px'):
                        fields['beam_center_'+axis+'_px']=float(obj[()])
                    elif units in _LENGTH and pixel:
                        fields['beam_center_'+axis+'_px']=float(obj[()])*_LENGTH[units]/pixel
                        # Length measured from detector edge is canonical.
                        convention=convention or 'canonical_top_left_edge'
            if 'depends_on' in detector and _text(detector['depends_on'][()])!='.':
                notes.append('NXtransformations chain requires explicit geometry review');convention=None
        mask=~np.isfinite(data)
        mask_sources=[]
        cutoff=None
        if detector is not None:
            for key in ('pixel_mask','detectorSpecific/pixel_mask'):
                if key not in detector:
                    continue
                obj=detector[key]
                if obj.shape!=data.shape:
                    raise UnsupportedTiffError('NXdetector pixel_mask shape differs from raster')
                mask |= np.array(obj[()]!=0)
                mask_sources.append(obj.name)
            if 'saturation_value' in detector:
                obj=detector['saturation_value']
                if obj.size!=1:
                    raise CorruptImageError('NXdetector saturation_value must be scalar')
                value=float(np.asarray(obj[()]).reshape(-1)[0])
                if not math.isfinite(value) or value<=0 or not value.is_integer():
                    raise CorruptImageError('NXdetector saturation_value must be a positive integer')
                cutoff=int(value)
        header={'dataset':selected['dataset'],'metadata_notes':notes,'geometry_fields':fields,
                'axes':selected['axes'],'center_convention':convention,
                'detector_mask_sources':mask_sources,'saturation_value':cutoff}
        geometry=_geometry(fields,data.shape,path,convention)
        return ImageFrame(data,ImageMetadata(source_path=Path(path),format_id='hdf5_detector_v1',
            support_level='quantitative_candidate',width_px=data.shape[1],height_px=data.shape[0],dtype=str(data.dtype),
            pixel_size_x_m=fields.get('pixel_size_x_m'),pixel_size_y_m=fields.get('pixel_size_y_m'),
            count_cutoff=cutoff,
            exposure_time_s=exposure,raw_tags=header,geometry_document=geometry,detector_mask=mask,
            dataset_path=selected['dataset'],frame_index=index,frame_count=selected['frames'],intensity_unit=unit))


def read_frame(path,*,dataset=None,frame_index=None):
    path=Path(path).expanduser().resolve()
    try:
        from ..processed_data import read_processed_frame
        processed=read_processed_frame(path)
        if processed is not None:
            _index(frame_index,1)
            if dataset is not None and dataset not in (None,'/entry/data/data'):
                raise UnsupportedTiffError('Processed export contains one canonical dataset')
            return processed
        if is_hdf5(path):
            return _read_hdf(path,dataset,frame_index)
        with path.open('rb') as stream:
            signature=stream.read(128)
        if signature[:4] in (b'II*\0',b'MM\0*',b'II+\0',b'MM\0+'):
            if _tiff_orientation(path,frame_index or 0)!=1:
                raise UnsupportedTiffError('TIFF Orientation 2-8 needs an explicit orientation/geometry adapter; pixels are not silently mirrored')
            from .tvx_tiff import _read_current_tvx_tiff
            try:
                result=_read_current_tvx_tiff(path)
                _index(frame_index,1)
                return result
            except TiffReaderError:
                pass
            from .rigaku_tiff import read_rigaku_tiff
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-65536));tail=stream.read(65536)
            if b'Rigaku 2D image' in signature or b'Rigaku 2D image' in tail:
                try:
                    result=read_rigaku_tiff(path)
                    _index(frame_index,1)
                    return result
                except UnsupportedTiffError:pass
        if signature.startswith(b'{') and b'HEADER_BYTES=' in signature:
            from .dtrek import read_dtrek
            try:
                result=read_dtrek(path)
                _index(frame_index,1)
                return result
            except UnsupportedTiffError:pass
        return _read_fabio(path,frame_index)
    except TiffReaderError:
        raise
    except Exception as exc:
        raise CorruptImageError(f'Cannot decode detector data {path.name}: {exc}') from exc


def load_plan_sample(plan,index,loader):
    selection=plan.metadata.get('source_selections',[{}]*len(plan.sample_sources))[index]
    path=Path(plan.sample_sources[index].path)
    verify_dependencies(plan.metadata.get('source_dependencies',{}).get(str(path.resolve()),[]))
    return read_frame(path,**selection) if selection else loader(path)


def load_background_source(recipe,loader):
    path=Path(recipe.background_source.path)
    selection=recipe.background.metadata.get('source_selection',{})
    return read_frame(path,**selection) if selection else loader(path)


def selection_suffix(plan,index):
    selection=plan.metadata.get('source_selections',[{}]*len(plan.sample_sources))[index]
    return f"_frame{int(selection.get('frame_index',0))+1:04d}_source{index+1:06d}" if selection else ''


def source_label(path, selection=None):
    """Human-readable acquisition identity; frame numbers are always one-based."""
    selection = selection or {}
    label = Path(path).name
    if selection:
        label += f" · 帧{int(selection.get('frame_index', 0))+1:04d}"
        if selection.get('dataset'):
            label += f" [{selection['dataset']}]"
    return label


def selection_label(plan, index):
    selections = plan.metadata.get('source_selections', [{}]*len(plan.sample_sources))
    return source_label(plan.sample_sources[index].path, selections[index])


def frame_label(frame):
    metadata = frame.metadata
    selection = {}
    if metadata.frame_count > 1 or metadata.dataset_path:
        selection = {'frame_index': metadata.frame_index, 'dataset': metadata.dataset_path}
    return source_label(metadata.source_path, selection)


def filter_frame_selections(choices,indices=None):
    """Select zero-based source ordinals, retaining dataset/frame acquisition order."""
    if indices is None:
        return choices
    requested=list(indices)
    if not requested or any(isinstance(i,bool) or not isinstance(i,int) or
                            i<0 or i>=len(choices) for i in requested):
        raise UnsupportedTiffError('Choose valid frame numbers from this source')
    selected=set(requested)
    return [choice for i,choice in enumerate(choices) if i in selected]


def expand_identities(paths,dataset_choices=None,details=None,frame_choices=None):
    from ..processing import FileIdentity
    identities=[];selections=[]
    paths,catalogs,errors,excluded=inspect_sources(paths)
    snapshots={key:dependency_snapshot(catalog) for key,catalog in catalogs.items()}
    if details is not None:details.update(excluded=excluded,dependencies=snapshots)
    for path in paths:
        identity=FileIdentity.from_path(path)
        dataset=(dataset_choices or {}).get(str(Path(path).resolve()))
        try:
            if str(path) in errors:raise errors[str(path)]
            choices=_selections(catalogs[str(path)],dataset)
        except (UnsupportedTiffError,CorruptImageError,OSError):
            choices=[{}]  # Retain bad files as batch failures, do not drop them.
        choices=filter_frame_selections(choices,(frame_choices or {}).get(str(path)))
        for selection in choices:
            identities.append(identity);selections.append(selection)
    return tuple(identities),selections
