"""Bounded, offline analysis service shared by stdio clients and tests."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import numpy as np

from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff
from saxs_waxs_core.background import BackgroundRecipe,apply_background
from saxs_waxs_core.masking import MaskRecipe,build_mask,load_external_mask_layer
from saxs_waxs_core.geometry import DetectorGeometry,GeometryProvenance,geometry_to_document
from saxs_waxs_core.integration import RadialIntegrationRecipe,AzimuthalIntegrationRecipe,integrate_radial_1d,integrate_azimuthal_1d
from saxs_waxs_core.workbook import write_workbook
from saxs_waxs_core.readers.detector_io import supported_detector_path,source_catalog,source_selections,SourceSelectionRequired


GUIDE = """XRelay local workflow:
1. list_files and inspect_image. Input metadata may be absent. Ask the user for
wavelength, distance, pixel dimensions and beam center when unknown; do not invent
them or use filename-specific geometry. Geometry uses metres and continuous pixel
coordinates, origin at top-left image edge; pixel (0,0) center is (0.5,0.5).
2. start_integration with radial or azimuthal kind, explicit geometry and optional
air background. Raw/no-background processing is permitted without a calibrant.
If subtracting with missing exposure, equal-exposure fallback is an explicit option,
output counts; known times use rates. Raw files are never overwritten.
3. Poll get_job; start_fit consumes a completed integration job. Automatic azimuth
fits return per-peak candidate Herman using fitted full-domain extrapolation,
which does not fill or alter observed data. Crystallinity requires user-confirmed
WAXS crystalline features and explicit phase assignments/bounds, never an amorphous
halo alone. Review scientific assumptions with the user.
4. start_ruland uses q start/stop/step, separate ring width, one Gaussian or
Lorentzian family for every q, explicit FWHM (default)/HWHM. Report width convention,
failures, slope/intercept, source formula and sensitivity; do not select q points
solely to increase R2 or force an expected length.
5. Every data export is one XLSX with Curves/Parameters/Processing information
(Chinese sheet names: 曲线数据, 参数汇总, 处理说明). Original and fitted curves use
separate XY pairs; failures and stopped items are retained. No per-item JSON.
Jobs run in the background, cancel_job stops between samples. No shell, network,
cloud model, credentials, or arbitrary filesystem writes are provided by these tools.
Technical completion is not scientific validation. Summarize outputs and limitations.
"""


class AgentService:
    def __init__(self,input_root,output_root):
        self.input_root=Path(input_root).expanduser().resolve(strict=True)
        self.output_root=Path(output_root).expanduser().resolve()
        protected=(Path(__file__).resolve().parents[2]/"data").resolve()
        if not self.input_root.is_dir():
            raise ValueError("input_root must be a directory")
        if self.output_root == protected or protected in self.output_root.parents:
            raise ValueError("outputs cannot be written inside project raw data")
        self.jobs={}
        self.run_directories=set()
        self.lock=threading.RLock()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="workbench-agent")

    def input_path(self,value):
        candidate=Path(value).expanduser()
        path=(candidate if candidate.is_absolute() else self.input_root/candidate).resolve(strict=True)
        if path != self.input_root and self.input_root not in path.parents:
            raise ValueError("input path is outside the user-selected directory")
        return path

    def list_files(self,path=".",recursive=True):
        root=self.input_path(path)
        files=root.rglob("*") if recursive else root.glob("*")
        result=[]
        with self.lock:
            run_directories=tuple(self.run_directories)
        for p in files:
            if p.is_file() and (supported_detector_path(p) or p.suffix.lower() in {".csv",".tsv",".txt",".dat"}):
                resolved=p.resolve()
                if self.input_root not in resolved.parents or (
                    self.output_root != self.input_root and self.output_root in resolved.parents
                ) or any(folder in resolved.parents for folder in run_directories):
                    continue
                result.append({"path":str(p.relative_to(self.input_root)),"bytes":p.stat().st_size})
                if len(result)>=10000:
                    break
        return {"files":sorted(result,key=lambda r:r["path"]),"limit":10000,
                "note":"Extensions are discovery hints; inspect content. Do not process duplicate TIFF/IMG captures twice."}

    def inspect_source(self,path):
        return {'path':path,'datasets':source_catalog(self.input_path(path)),
            'note':'Select ambiguous datasets explicitly. Batch jobs expand all frames of the selected detector signal.'}

    def inspect_image(self,path,dataset=None,frame_index=None):
        f=read_tvx_tiff(self.input_path(path),dataset=dataset,frame_index=frame_index)
        m=f.metadata
        return {"path":str(m.source_path),"shape":list(f.data.shape),"dtype":str(f.data.dtype),
            "format":m.format_id,"support":m.support_level,"pixel_size_x_m":m.pixel_size_x_m,
            'dataset':m.dataset_path,'frame_index':m.frame_index,'frame_count':m.frame_count,
            "pixel_size_y_m":m.pixel_size_y_m,"exposure_s":m.exposure_time_s,
            "embedded_geometry":m.embedded_processing.get('geometry') or dict(m.geometry_document),
            "intensity_unit":m.intensity_unit or m.embedded_processing.get('intensity_unit','detector_count'),
            'header_geometry_fields':dict(m.raw_tags.get('geometry_fields',{})),
            "background_applied":m.embedded_processing.get('background_applied',False),
            "minimum":float(np.nanmin(f.data)),"maximum":float(np.nanmax(f.data)),
            "note":"Missing geometry/exposure remains unknown. Native raster orientation is retained."}

    def list_calibrants(self):
        from saxs_waxs_core.calibrants import load_calibrant_library
        return {"calibrants":[{"id":c.id,"name":c.name,"rings":len(c.peaks)} for c in load_calibrant_library().calibrants]}

    def calibrate_three_points(self,wavelength_m,pixel_size_x_m,pixel_size_y_m,calibrant_id,ring_ordinal,points_xy_px):
        from saxs_waxs_core.calibrants import load_calibrant_library
        from saxs_waxs_core.calibration import fit_manual_calibrant_ring
        # The one-metre placeholder is not an observation: the assigned-ring
        # calculation derives distance solely from the physical ring radius.
        seed=DetectorGeometry(wavelength_m=wavelength_m,detector_distance_m=1.,pixel_size_x_m=pixel_size_x_m,
            pixel_size_y_m=pixel_size_y_m,beam_center_x_px=0,beam_center_y_px=0,
            provenance=GeometryProvenance(method="unused_distance_placeholder_for_three_point_solver"))
        estimate=fit_manual_calibrant_ring(seed,load_calibrant_library().resolve(calibrant_id),int(ring_ordinal),points_xy_px)
        geometry=estimate.candidate_geometry
        return {"geometry":{k:getattr(geometry,k) for k in ("wavelength_m","detector_distance_m","pixel_size_x_m",
            "pixel_size_y_m","beam_center_x_px","beam_center_y_px")},"radial_rms_m":estimate.radial_rms_m,
            "note":"Candidate single-ring zero-tilt geometry; verify ring assignment and other rings before scientific use."}

    def _frame_inputs(self,path,args,selection=None):
        from saxs_waxs_core.readers.detector_io import verify_dependencies
        verify_dependencies(args.get('_source_dependencies',{}).get(str(self.input_path(path)),[]))
        sample=read_tvx_tiff(self.input_path(path),**(selection or {}))
        parameters=dict(args.get("geometry",{}))
        parameters.update(image_width_px=sample.metadata.width_px,image_height_px=sample.metadata.height_px,
            provenance=GeometryProvenance(method="agent_user_supplied",source_application="XRelay",
                notes="Parameters explicitly supplied to local Agent tool; scientific validity not certified"))
        from saxs_waxs_core.processed_data import embedded_geometry
        geometry=(embedded_geometry(sample) or DetectorGeometry(**parameters)) if args.get('prefer_embedded_geometry',True) else DetectorGeometry(**parameters)
        air=read_tvx_tiff(self.input_path(args["background_path"]),**args.get('background_selection',{})) if args.get("background_path") else None
        if air is not None and (air.metadata.source_path,air.metadata.dataset_path,air.metadata.frame_index)==(sample.metadata.source_path,sample.metadata.dataset_path,sample.metadata.frame_index):
            raise ValueError("air background cannot be the sample")
        background=BackgroundRecipe(mode="auto_air" if air is not None else "no_background",
            metadata={"assume_equal_exposure_if_missing":args.get("assume_equal_exposure",True)})
        mask=MaskRecipe(user_layers=(load_external_mask_layer(self.input_path(args["external_mask_path"]),sample.data.shape),)) if args.get("external_mask_path") else MaskRecipe()
        return sample,air,geometry,background,mask

    def start(self,kind,arguments):
        if kind not in ("integration","fit","ruland","images","import_curves","saxs_analysis","processed_data"):
            raise ValueError("unsupported analysis")
        arguments=dict(arguments)
        if kind in ("fit","saxs_analysis"):
            source=self.jobs.get(arguments["integration_job"])
            if source is None or source["state"] != "completed" or source["kind"] not in ("integration","import_curves"):
                raise ValueError("a completed integration/import job is required")
            if not source.get("curves"):
                raise ValueError("integration job has no successful curves")
        else:
            paths=arguments.get("paths",[])
            if not paths or len(paths)>10000 or len(paths) != len(set(paths)):
                raise ValueError("choose 1-10000 distinct input paths")
            for p in paths:
                self.input_path(p)
        with self.lock:
            if sum(j["state"] in ("queued","running") for j in self.jobs.values())>=8:
                raise ValueError("job queue is full")
            jobid=uuid.uuid4().hex[:12]
            job={"id":jobid,"kind":kind,"state":"queued","completed":0,"total":0,"errors":[],
                 "stop":threading.Event(),"output":None}
            self.jobs[jobid]=job
            self.executor.submit(self._run,job,arguments)
        return {"job_id":jobid,"state":"queued","next":"Call get_job until completed, cancelled or failed"}

    def get_job(self,job_id):
        with self.lock:
            j=self.jobs[job_id]
            return {k:v for k,v in j.items() if k not in ("stop","curves")}

    def cancel_job(self,job_id):
        self.jobs[job_id]["stop"].set()
        return {"job_id":job_id,"stop_requested":True}

    def _run(self,job,args):
        with self.lock:
            job["state"]="running"
        try:
            label=("radial_integration" if args.get("kind","radial") == "radial" else "azimuthal_integration") if job["kind"] == "integration" else job["kind"]
            folder=self.output_root/(label+"_"+job["id"])
            folder.mkdir(parents=True,exist_ok=False)
            with self.lock:
                self.run_directories.add(folder)
            if job["kind"] == "integration":
                self._integrate(job,args,folder)
            elif job['kind']=='processed_data':
                self._processed_data(job,args,folder)
            elif job["kind"] == "fit":
                self._fit(job,args,folder)
            elif job["kind"] == "images":
                self._images(job,args,folder)
            elif job["kind"] == "import_curves":
                self._import(job,args,folder)
            elif job["kind"] == "saxs_analysis":
                self._saxs(job,args,folder)
            else:
                self._ruland(job,args,folder)
            job["state"]="cancelled" if job["stop"].is_set() else "completed"
        except Exception as exc:
            job["errors"].append(str(exc))
            job["state"]="failed"

    def _processed_data(self,job,args,folder):
        from saxs_waxs_core.processed_data import export_processed_batch
        geometry=DetectorGeometry(**args['geometry']) if args.get('geometry') else None
        paths=[self.input_path(p) for p in args['paths']]
        air=self.input_path(args['background_path']) if args.get('background_path') else None
        mask=self.input_path(args['external_mask_path']) if args.get('external_mask_path') else None
        recipe=BackgroundRecipe(mode='auto_air' if air else 'no_background',
            metadata={'assume_equal_exposure_if_missing':args.get('assume_equal_exposure',True)})
        def progress(value):
            job['completed']=value['completed']
            job['total']=value['total']
        result=export_processed_batch(paths,folder/'frames',background_path=air,
            background_recipe=recipe,external_mask_path=mask,geometry=geometry,
            output_format=args.get('output_format','auto'),background_selection=args.get('background_selection'),
            prefer_embedded_geometry=args.get('prefer_embedded_geometry',True),
            dataset_selections={str(self.input_path(path)):dataset for path,dataset in args.get('datasets',{}).items()},
            stop_requested=job['stop'].is_set,progress=progress)
        job['errors'].extend(row for row in result['items'] if row['status']=='failed')
        job['output']=str(Path(result['directory'])/'processing_info.txt')

    def _sources(self,args):
        from saxs_waxs_core.readers.detector_io import inspect_sources,dependency_snapshot
        result=[]
        names={str(self.input_path(path)):path for path in args['paths']}
        paths,catalogs,errors,excluded=inspect_sources(names)
        args['_source_dependencies']={key:dependency_snapshot(value) for key,value in catalogs.items()}
        args['_excluded_chunks']=excluded
        for absolute in paths:
            path=names[str(absolute)]
            try:
                selections=source_selections(self.input_path(path),dataset=args.get('datasets',{}).get(path))
                result.extend((path,selection,None) for selection in selections)
            except Exception as exc:
                result.append((path,{},str(exc)))
        return result

    def _integrate(self,job,args,folder):
        groups,records,contexts,curves=[],[],[],[]
        paths=args["paths"]
        sources=self._sources(args)
        job["total"]=len(sources)
        contexts.append(('source_container_selection',{'excluded_chunks':args['_excluded_chunks']}))
        for number,(path,selection,error) in enumerate(sources):
            label=path+(f' [source {number+1}]' if selection else '')
            if job["stop"].is_set():
                records.append({"样品":path,"状态":"未处理"})
                continue
            try:
                if error:raise ValueError(error)
                sample,air,geometry,bg,mask=self._frame_inputs(path,args,selection)
                background=apply_background(sample,air,bg)
                excluded=build_mask(sample,mask)
                settings=dict(args.get("settings",{}))
                settings["input_unit"]=background.output_unit
                if args.get("kind","radial") == "radial":
                    if "sectors" in settings:
                        from saxs_waxs_core.integration import AzimuthSector
                        settings["sectors"] = tuple(AzimuthSector(**sector) for sector in settings["sectors"])
                    recipe=RadialIntegrationRecipe(**settings)
                    curve=integrate_radial_1d(background.data,geometry,recipe,mask=excluded.excluded)
                else:
                    recipe=AzimuthalIntegrationRecipe(**settings)
                    curve=integrate_azimuthal_1d(background.data,geometry,recipe,mask=excluded.excluded)
                context={"geometry":geometry_to_document(geometry),"background":dict(background.audit),
                         "mask":dict(excluded.audit),"integration":dict(curve.audit)}
                context['source_selection']=selection
                curves.append((label,curve,context))
                groups.append((label,curve.x,curve.intensity,"横坐标","强度",curve.x_unit,curve.intensity_unit))
                records.append({"样品":label,"状态":"候选结果","点数":len(curve.x),"单位":curve.intensity_unit})
                contexts.append((label,context))
            except Exception as exc:
                records.append({"样品":path,"状态":"失败","说明":str(exc)})
                job["errors"].append({"sample":path,"error":str(exc)})
            job["completed"]+=1
        job["curves"]=curves
        suffix="radial_integration" if args.get("kind","radial") == "radial" else "azimuthal_integration"
        prefix=Path(paths[0]).stem if len(paths)==1 else "batch"
        job["output"]=str(write_workbook(folder/f"{prefix}_{suffix}.xlsx",groups,records,contexts))

    def _import(self,job,args,folder):
        from saxs_waxs_core.curve_import import import_curve
        groups,records,contexts,curves=[],[],[],[]
        sources=self._sources(args)
        job["total"]=len(sources)
        for number,(path,selection,error) in enumerate(sources):
            label=path+(f' [source {number+1}]' if selection else '')
            if job["stop"].is_set():
                records.append({"样品":path,"状态":"未处理"})
                continue
            try:
                curve=import_curve(self.input_path(path),**args["settings"])
                context=dict(curve.audit)
                curves.append((path,curve,context))
                groups.append((path,curve.x,curve.intensity,"原始横坐标","原始强度",curve.x_unit,curve.intensity_unit))
                records.append({"样品":path,"状态":"已导入","点数":len(curve.x)})
                contexts.append((path,context))
            except Exception as exc:
                records.append({"样品":path,"状态":"失败","说明":str(exc)})
                job["errors"].append({"sample":path,"error":str(exc)})
            job["completed"]+=1
        job["curves"]=curves
        job["output"]=str(write_workbook(folder/"imported_curves.xlsx",groups,records,contexts))

    def _saxs(self,job,args,folder):
        from types import SimpleNamespace
        from saxs_waxs_core.curve_coordinates import radial_q_intensity
        from saxs_waxs_core.saxs_power_analysis import fit_saxs_power
        from saxs_waxs_core.debye_bueche import fit_debye_bueche
        from saxs_waxs_core.analysis_export import export_analysis_workbook
        source=self.jobs[args["integration_job"]]
        results={}
        job["total"]=len(source["curves"])
        for i,(name,curve,context) in enumerate(source["curves"]):
            if job["stop"].is_set():
                continue
            try:
                unit=args.get("q_unit","nm^-1")
                q,intensity=radial_q_intensity(curve,unit)
                if args["method"] == "debye_bueche":
                    results[i]=fit_debye_bueche(q,intensity,model=args.get("debye_model","single"),q_range=args["q_range"],q_unit=unit)
                else:
                    results[i]=fit_saxs_power(q,intensity,method=args["method"],q_range=args["q_range"],q_unit=unit)
            except Exception as exc:
                results[i]=str(exc)
                job["errors"].append({"sample":name,"error":str(exc)})
            job["completed"]+=1
        snapshot=SimpleNamespace(curves=[(name,curve) for name,curve,_ in source["curves"]],results=results,
            parameter_contexts=[context for _,_,context in source["curves"]],batch_started=True)
        job["output"]=str(export_analysis_workbook(snapshot,folder/(args["method"]+".xlsx")))

    def _fit(self,job,args,folder):
        from saxs_waxs_core.peak_fitting import fit_peak,fit_multi_peak
        from saxs_waxs_core.auto_azimuth import fit_all_automatic_azimuth
        from saxs_waxs_core.herman import from_single_peak_fit
        from saxs_waxs_core.crystallinity import crystallinity_from_fit
        source=self.jobs[args["integration_job"]]
        groups,records,contexts=[],[],[]
        job["total"]=len(source["curves"])
        for name,curve,context in source["curves"]:
            groups.append((name,curve.x,curve.intensity,"原始横坐标","原始强度",curve.x_unit,curve.intensity_unit))
            contexts.append((name,context))
            if job["stop"].is_set():
                records.append({"样品":name,"状态":"未处理"})
                continue
            try:
                y=np.array(curve.intensity,copy=True)
                y[curve.count<=0]=np.nan
                options=dict(args.get("settings",{}))
                if args.get("automatic_azimuth",False):
                    if curve.kind != "azimuthal_1d":
                        raise ValueError("automatic azimuth requires azimuthal integration")
                    detected,failures=fit_all_automatic_azimuth(curve,shared_model=True,allow_masked=True)
                    fits=[p.result for p in detected]
                    for rank,reason in failures:
                        records.append({"样品":name,"峰号":rank,"状态":"失败","说明":reason})
                        job["errors"].append({"sample":name,"peak":rank,"error":reason})
                else:
                    count=options.pop("peak_count",1)
                    fn=fit_peak if count == 1 else fit_multi_peak
                    extra={} if count == 1 else {"peak_count":count}
                    fits=[fn(curve.x,y,x_unit=curve.x_unit,y_unit=curve.intensity_unit,**extra,**options)]
                for n,fit in enumerate(fits,1):
                    x=fit.used_x%360 if curve.kind == "azimuthal_1d" else fit.used_x
                    order=np.argsort(x)
                    groups.append((f"{name} · 峰{n}",x[order],fit.best_fit_y[order],"拟合横坐标","拟合强度",fit.x_unit,fit.y_unit))
                    groups.append((f"{name} · 峰{n} · 残差",x[order],fit.residual_y[order],
                        "拟合横坐标","残差（原始−拟合）",fit.x_unit,fit.y_unit))
                    row={"样品":name,"峰号":n,"状态":"候选结果","模型":fit.model,**dict(fit.metrics)}
                    for key,p in fit.parameters.items():
                        row[key]=p.value
                        row[key+" stderr"]=p.stderr
                        row[key+" unit"]=p.unit
                    if curve.kind == "azimuthal_1d" and not fit.components:
                        h=from_single_peak_fit(curve,fit,domain="model_full_90")
                        row.update(Herman=h.f_fit,Herman_status=h.status,Herman_note=h.message)
                    assignments=args.get("crystallinity_assignments")
                    if assignments:
                        if not args.get("waxs_confirmed") or curve.kind != "radial_1d":
                            raise ValueError("crystallinity requires user-confirmed WAXS crystalline features")
                        crystal=crystallinity_from_fit(fit,assignments)
                        row.update(crystallinity=crystal.fraction,crystalline_area=crystal.crystalline_area,amorphous_area=crystal.amorphous_area)
                    records.append(row)
            except Exception as exc:
                records.append({"样品":name,"状态":"失败","说明":str(exc)})
                job["errors"].append({"sample":name,"error":str(exc)})
            job["completed"]+=1
        job["output"]=str(write_workbook(folder/"peak_fit.xlsx",groups,records,contexts,
            notes=("Herman使用逐峰拟合模型完整域；外推不修改实测数据。",)))

    def _ruland(self,job,args,folder):
        from saxs_waxs_core.ruland_integration import ruland_q_grid
        from saxs_waxs_core.ruland_workflow import run_ruland_workflow
        from saxs_waxs_ui.ruland_export import export_ruland_workbook
        q=args["q_range"]
        settings={"q_centers":ruland_q_grid(q[0],q[1],args["q_step"]),"q_width":args["q_width"],
            "peak_center_deg":args.get("peak_center_deg"),"half_window_deg":args.get("half_window_deg",25),
            "model":args.get("model","gaussian"),"width_definition":args.get("width_definition","FWHM")}
        entries=[]
        sources=self._sources(args)
        job["total"]=len(sources)
        for number,(path,selection,error) in enumerate(sources):
            label=path+(f' [source {number+1}]' if selection else '')
            if job["stop"].is_set():
                entries.append((path,None,"not_processed: stopped"))
                continue
            try:
                if error:raise ValueError(error)
                sample,air,geometry,bg,mask=self._frame_inputs(path,args,selection)
                recipe=AzimuthalIntegrationRecipe(radial_range=(q[0],q[1]),input_unit=sample.metadata.intensity_unit or ("detector_count/s" if air else "detector_count"))
                result=run_ruland_workflow(sample,air,geometry,bg,mask,recipe,**settings)
                entries.append((label,result,result.error))
                if result.error:
                    job["errors"].append({"sample":path,"error":result.error})
            except Exception as exc:
                entries.append((path,None,str(exc)))
                job["errors"].append({"sample":path,"error":str(exc)})
            job["completed"]+=1
        job["output"]=str(export_ruland_workbook(folder/"ruland.xlsx",entries,settings,
            sampling={"mode":"step","requested_start_nm_inv":q[0],"requested_stop_nm_inv":q[1],"step_nm_inv":args["q_step"],
                'sources':[{'path':path,**selection} for path,selection,error in sources],
                'excluded_chunks':args['_excluded_chunks'],'source_dependencies':args['_source_dependencies']}))

    def _images(self,job,args,folder):
        from dataclasses import replace,asdict
        from saxs_waxs_core.display import DisplayPreset,resolve_display_limits
        from saxs_waxs_core.image_export import export_2d_image
        from saxs_waxs_core.workbook import flatten_context
        display=DisplayPreset(**args.get("display",{}))
        fixed=display if display.limit_mode == "manual" else None
        entries=[]
        sources=self._sources(args)
        job["total"]=len(sources)
        fmt=args.get("format","png")
        for i,(path,selection,error) in enumerate(sources,1):
            if job["stop"].is_set():
                entries.append({"source":path,"status":"not_processed"})
                continue
            try:
                if error:raise ValueError(error)
                from saxs_waxs_core.readers.detector_io import verify_dependencies
                verify_dependencies(args.get('_source_dependencies',{}).get(str(self.input_path(path)),[]))
                sample=read_tvx_tiff(self.input_path(path),**selection)
                air=read_tvx_tiff(self.input_path(args["background_path"]),**args.get('background_selection',{})) if args.get("background_path") else None
                if air is not None and (air.metadata.source_path,air.metadata.dataset_path,air.metadata.frame_index)==(sample.metadata.source_path,sample.metadata.dataset_path,sample.metadata.frame_index):
                    raise ValueError("air background cannot be the sample")
                bg=BackgroundRecipe(mode="auto_air" if air is not None else "no_background",
                    metadata={"assume_equal_exposure_if_missing":args.get("assume_equal_exposure",True)})
                processed=apply_background(sample,air,bg)
                mask_recipe=MaskRecipe(user_layers=(load_external_mask_layer(self.input_path(args["external_mask_path"]),sample.data.shape),)) if args.get("external_mask_path") else MaskRecipe()
                mask=build_mask(sample,mask_recipe).excluded
                if fixed is None:
                    low,high=resolve_display_limits(processed.data,display,mask)
                    fixed=replace(display,limit_mode="manual",manual_vmin=low,manual_vmax=high)
                result=export_2d_image(processed.data,folder/f"{i:04d}_{Path(path).stem}_2d.{fmt}",fixed,
                    mask=mask,dpi=args.get("dpi",300),source_path=sample.metadata.source_path,
                    processing_audit={"background":dict(processed.audit)},write_parameter_file=False)
                entries.append({"source":path,'source_selection':selection,"status":"completed","image":result.image_path.name,
                    "background_formula":processed.audit["formula"],"intensity_unit":processed.output_unit})
            except Exception as exc:
                entries.append({"source":path,"status":"failed","reason":str(exc)})
                job["errors"].append({"sample":path,"error":str(exc)})
            job["completed"]+=1
        note=folder/"processing_info.txt"
        with note.open("x",encoding="utf-8") as stream:
            stream.write("2D image export / 二维图像导出\nShared display limits and colormap / 统一色阶及颜色\n")
            for key,value in flatten_context({"display":asdict(fixed or display),"items":entries,'excluded_chunks':args['_excluded_chunks']}).items():
                stream.write(f"{key}: {value}\n")
        job["output"]=str(note)

    def close(self):
        for job in self.jobs.values():
            job["stop"].set()
        self.executor.shutdown(wait=True,cancel_futures=True)
