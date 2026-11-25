"""MCP stdio baseline: initialization, tools and asynchronous job polling.

No dependency on a particular model provider or desktop client. Protocol output
is newline-delimited JSON-RPC only; numerical-library logging goes to stderr.
"""
import argparse
import contextlib
import json
import sys
from .service import AgentService,GUIDE
from saxs_waxs_ui.branding import VERSION


def tool(name,description,properties=None,required=(),read_only=True):
    return {"name":name,"description":description,"inputSchema":{"type":"object",
        "properties":properties or {},"required":list(required),"additionalProperties":False},
        "annotations":{"readOnlyHint":read_only,"destructiveHint":False,"openWorldHint":False}}


S={"type":"string"}
N={"type":"number"}
I={"type":"integer"}
PATHS={"type":"array","items":S,"minItems":1,"maxItems":10000}
GEO={"type":"object","properties":{k:N for k in ("wavelength_m","detector_distance_m","pixel_size_x_m",
     "pixel_size_y_m","beam_center_x_px","beam_center_y_px")},
     "required":["wavelength_m","detector_distance_m","pixel_size_x_m","pixel_size_y_m","beam_center_x_px","beam_center_y_px"],
     "additionalProperties":False}
INPUT={"paths":PATHS,"geometry":GEO,"background_path":S,"external_mask_path":S,"assume_equal_exposure":{"type":"boolean"},
       'datasets':{'type':'object','additionalProperties':S},
       'prefer_embedded_geometry':{'type':'boolean'},
       'background_selection':{'type':'object','properties':{'dataset':S,'frame_index':I},'additionalProperties':False}}
TOOLS=[
    tool("get_workflow_guide","Read this first: shared workflow, units, scientific limits, output contract."),
    tool('start_processed_data','Export quantitative background-corrected and/or masked 2D files in the original supported container (TIFF float64 or d*TREK IMG float32), with embedded geometry, units and NaN MASK. No recipe save or calibration required. Optional geometry must be explicitly known; existing file geometry takes precedence. One shared processing note.',
         {**INPUT,'output_format':{'type':'string','enum':['auto','source','tiff','nexus']}},['paths'],False),
    tool("list_calibrants","List the built-in reference calibrants; never infer the standard from a sample filename."),
    tool("calibrate_three_points","Derive candidate zero-tilt center and SDD from user-picked points on one assigned calibrant ring; wavelength and pixels required, no known SDD needed. Verify other rings before use.",
         {"wavelength_m":N,"pixel_size_x_m":N,"pixel_size_y_m":N,"calibrant_id":S,"ring_ordinal":I,
          "points_xy_px":{"type":"array","items":{"type":"array","items":N,"minItems":2,"maxItems":2},"minItems":3}},
         ["wavelength_m","pixel_size_x_m","pixel_size_y_m","calibrant_id","ring_ordinal","points_xy_px"]),
    tool("list_files","Discover detector files inside the user-selected input directory.",{"path":S,"recursive":{"type":"boolean"}}),
    tool('inspect_source','List detector datasets and frame counts before selecting a frame. Ambiguous HDF5 datasets require an explicit datasets map.',{'path':S},['path']),
    tool("inspect_image","Read one detector frame and its metadata without changing pixels. Unknown geometry remains unknown.",{"path":S,'dataset':S,'frame_index':I},["path"]),
    tool("start_import_curves","Import raw TXT/CSV/TSV/DAT curves. settings require zero-based x_column/y_column, kind(radial_1d/azimuthal_1d), x_unit(q_nm^-1,q_A^-1,s_nm^-1,2th_deg or deg), intensity_unit; optional encoding/header_rows. Then use the resulting job id with start_fit/start_saxs_analysis.",
         {"paths":PATHS,"settings":{"type":"object"}},["paths","settings"],False),
    tool("start_saxs_analysis","Analyze completed integration/import curves with explicit Guinier, Porod or Debye-Bueche model and q range. Same core and export contract as the GUI; never infer validity from R2.",
         {"integration_job":S,"method":{"type":"string","enum":["guinier","porod","debye_bueche"]},
          "q_range":{"type":"array","items":N,"minItems":2,"maxItems":2},
          "q_unit":{"type":"string","enum":["nm^-1","A^-1"]},"debye_model":{"type":"string","enum":["single","double"]}},
         ["integration_job","method","q_range"],False),
    tool("start_integration","Start a background batch. Geometry is read from processed files; otherwise provide explicit SI geometry. No calibrant required. settings use core recipe fields; azimuthal needs radial_range. Defaults preserve raw intensity without air subtraction.",
         {**INPUT,"kind":{"type":"string","enum":["radial","azimuthal"]},"settings":{"type":"object"}},["paths"],False),
    tool("start_fit","Fit an integration job's full raw curves. settings: model, background, x_range, peak_count and optional center_bounds/fwhm_bounds. Automatic azimuth fits all peaks consistently and reports model-based Herman. Crystallinity needs explicit user phase assignments and WAXS confirmation.",
         {"integration_job":S,"settings":{"type":"object"},"automatic_azimuth":{"type":"boolean"},
          "waxs_confirmed":{"type":"boolean"},"crystallinity_assignments":{"type":"object"}},["integration_job"],False),
    tool("start_images","Export raw or optionally air-subtracted and masked 2D images, without requiring calibration. One shared colormap and limits for the whole batch, chosen from display settings or the first readable sample; one common parameter note. display uses DisplayPreset fields; manual limits are supported.",
         {k:v for k,v in {**INPUT,"display":{"type":"object"},"format":{"type":"string","enum":["png","tiff","pdf","svg"]},"dpi":I}.items() if k != "geometry"},["paths"],False),
    tool("start_ruland","Start multi-q integration, consistent Gaussian/Lorentzian peak fits and Ruland regression. q in nm^-1; FWHM default. Ring width differs from q step. Poll get_job; failures stay in the same workbook.",
         {**INPUT,"q_range":{"type":"array","items":N,"minItems":2,"maxItems":2},"q_step":N,"q_width":N,
          "peak_center_deg":N,"half_window_deg":N,"model":{"type":"string","enum":["gaussian","cauchy"]},
          "width_definition":{"type":"string","enum":["FWHM","HWHM","integral_breadth"]}},
         ["paths","geometry","q_range","q_step","q_width"],False),
    tool("get_job","Poll progress, final workbook and failures. completed is technical execution, not scientific validation.",{"job_id":S},["job_id"]),
    tool("cancel_job","Request stopping between samples; keep not-processed statuses.",{"job_id":S},["job_id"],False),
]


def validate(value,schema):
    kind=schema.get("type")
    if kind == "object":
        if not isinstance(value,dict):
            raise ValueError("expected object")
        if any(k not in value for k in schema.get("required",())):
            raise ValueError("missing required arguments: "+", ".join(k for k in schema.get("required",()) if k not in value))
        properties=schema.get("properties",{})
        if schema.get("additionalProperties") is False and set(value)-set(properties):
            raise ValueError("unknown arguments: "+", ".join(set(value)-set(properties)))
        for k,v in value.items():
            if k in properties:
                validate(v,properties[k])
    elif kind == "array":
        if not isinstance(value,list) or len(value)<schema.get("minItems",0) or len(value)>schema.get("maxItems",1000000):
            raise ValueError("invalid array length")
        for item in value:
            validate(item,schema["items"])
    elif kind == "string" and not isinstance(value,str):
        raise ValueError("expected string")
    elif kind == "boolean" and not isinstance(value,bool):
        raise ValueError("expected boolean")
    elif kind == "number" and (isinstance(value,bool) or not isinstance(value,(int,float))):
        raise ValueError("expected number")
    elif kind == "integer" and (isinstance(value,bool) or not isinstance(value,int)):
        raise ValueError("expected integer")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("unsupported option")


class Protocol:
    def __init__(self,service):
        self.service=service
        self.initialized=False

    def handle(self,message):
        request_id=message.get("id")
        if request_id is None:
            return None
        method=message.get("method")
        result=None
        if method == "initialize":
            self.initialized=True
            supported=("2024-11-05","2025-03-26","2025-06-18")
            requested=message.get("params",{}).get("protocolVersion")
            result={"protocolVersion":requested if requested in supported else supported[-1],
                    "capabilities":{"tools":{}},"serverInfo":{"name":"saxs-waxs-workbench","version":VERSION},
                    "instructions":GUIDE}
        elif not self.initialized:
            return {"jsonrpc":"2.0","id":request_id,"error":{"code":-32000,"message":"initialize first"}}
        elif method == "ping":
            result={}
        elif method == "tools/list":
            result={"tools":TOOLS}
        elif method == "tools/call":
            params=message.get("params",{})
            name=params.get("name")
            try:
                definition=next(t for t in TOOLS if t["name"] == name)
                args=params.get("arguments",{})
                validate(args,definition["inputSchema"])
                if name == "get_workflow_guide":
                    value={"guide":GUIDE,"input_root":str(self.service.input_root),"output_root":str(self.service.output_root)}
                elif name.startswith("start_"):
                    value=self.service.start(name.removeprefix("start_"),args)
                else:
                    value=getattr(self.service,name)(**args)
                result={"content":[{"type":"text","text":json.dumps(value,ensure_ascii=False,default=str)}],"isError":False}
            except Exception as exc:
                result={"content":[{"type":"text","text":str(exc)}],"isError":True}
        else:
            return {"jsonrpc":"2.0","id":request_id,"error":{"code":-32601,"message":"method not supported"}}
        return {"jsonrpc":"2.0","id":request_id,"result":result}


def main():
    parser=argparse.ArgumentParser(description="Local SAXS/WAXS MCP stdio tools")
    parser.add_argument("--input-root",required=True)
    parser.add_argument("--output-root",required=True)
    args=parser.parse_args()
    if hasattr(sys.stdin,"reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    output=sys.stdout
    service=AgentService(args.input_root,args.output_root)
    protocol=Protocol(service)
    try:
        # Stdout is reserved for protocol responses, including during core jobs.
        with contextlib.redirect_stdout(sys.stderr):
            for line in sys.stdin:
                try:
                    if len(line)>1024*1024:
                        raise ValueError("message too large")
                    message=json.loads(line)
                    if not isinstance(message,dict):
                        raise ValueError("expected JSON-RPC object")
                    response=protocol.handle(message)
                except Exception as exc:
                    response={"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":str(exc)}}
                if response is not None:
                    output.write(json.dumps(response,ensure_ascii=False,default=str,allow_nan=False)+"\n")
                    output.flush()
    finally:
        service.close()


if __name__ == "__main__":
    main()
