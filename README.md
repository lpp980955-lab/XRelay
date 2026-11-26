English | [简体中文](README.zh-CN.md)

[Full designed showcase](https://lpp980955-lab.github.io/XRelay/index.en.html) · [Download XRelay 1.0.2](https://github.com/lpp980955-lab/XRelay/releases/tag/v1.0.2)

# XRelay

Compare samples live. Reuse one recipe for a batch.

Visual data processing for beginners and experienced researchers.
Compare detector patterns, integration regions and curves in one workspace.

- [**Click and drag; no code required** Connect import, frame selection, integration and export with visual controls.](https://lpp980955-lab.github.io/XRelay/index.en.html#workflow)

- [**Learn through the data** Compare patterns, regions and curves to understand structure–signal relationships.](https://lpp980955-lab.github.io/XRelay/index.en.html#workflow)

- [**Compare multiple samples** Synchronize region angles while retaining each detector geometry.](https://lpp980955-lab.github.io/XRelay/index.en.html#workflow)

- [**Configure once; reuse a recipe** Apply checked settings across files and frames.](https://lpp980955-lab.github.io/XRelay/index.en.html#workflow)

- [**From integration to finished figures** Preview and style images, then export data and graphics separately.](https://lpp980955-lab.github.io/XRelay/index.en.html#workflow)

- [**Optional AI workflow** An external MCP client can reuse the processing core.](https://lpp980955-lab.github.io/XRelay/index.en.html#workflow)

Windows x64 portable · Local computation · Free use · repackaged sales require permission

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/multi-image-workbench.png" alt="Complete three-column workspace: files, two linked images, processing controls and curves" width="1100">

The selected screenshots and GIFs retain the Chinese UI; captions and instructions are in English.

## Geometry and processing preview

Use the right-hand panel for geometry, calibration, background, MASK, integration, fitting and recipes. Check wavelength, distance, beam centre and pixel size against reference rings. Three-point tools and automatic centre/distance suggestions produce candidates for manual review; some previews assume zero tilt. Named geometry templates can be saved and reapplied.

Geometry and processing preview — geometry preview demo

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/geometry-preview-demo.gif" alt="Geometry and processing preview — geometry preview demo" width="1100">

Geometry and processing preview — integration single sync

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/integration-single-sync.gif" alt="Geometry and processing preview — integration single sync" width="1100">

Geometry and processing preview — integration opposite sync

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/integration-opposite-sync.gif" alt="Geometry and processing preview — integration opposite sync" width="1100">

Geometry and processing preview — integration full sync

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/integration-full-sync.gif" alt="Geometry and processing preview — integration full sync" width="1100">

Geometry and processing preview — processing panel tour

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/processing-panel-tour.gif" alt="Geometry and processing preview — processing panel tour" width="1100">

[Geometry and processing notes](https://github.com/lpp980955-lab/XRelay/blob/main/METHODS.en.md)

## Reference patterns: SAXS and WAXS

Built-in reference spacings and custom d-spacing imports help compare measured and theoretical rings. The gallery below keeps the selected reference patterns. File labels are not certificates of sample identity. Viridis/logarithmic display uses independent valid-pixel percentile limits; colours do not establish equal absolute intensity across samples. The user has confirmed retaining the selected visualizations; origins distinguish the supplied reference library from official public downloads. Raw datasets are not redistributed.

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/agb.png" alt="Reference patterns: SAXS and WAXS — agb" width="300">

agb

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/silver.png" alt="Reference patterns: SAXS and WAXS — silver" width="300">

silver

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/agbh-public.png" alt="Reference patterns: SAXS and WAXS — agbh public" width="300">

agbh public

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/ceo2-18.png" alt="Reference patterns: SAXS and WAXS — ceo2 18" width="300">

ceo2 18

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/ceo2-20.png" alt="Reference patterns: SAXS and WAXS — ceo2 20" width="300">

ceo2 20

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/ceo2-60.png" alt="Reference patterns: SAXS and WAXS — ceo2 60" width="300">

ceo2 60

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/lab6.png" alt="Reference patterns: SAXS and WAXS — lab6" width="300">

lab6

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/si.png" alt="Reference patterns: SAXS and WAXS — si" width="300">

si

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/y2o3.png" alt="Reference patterns: SAXS and WAXS — y2o3" width="300">

y2o3

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/al2o3-public.png" alt="Reference patterns: SAXS and WAXS — al2o3 public" width="300">

al2o3 public

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/bull-center.png" alt="Reference patterns: SAXS and WAXS — bull center" width="300">

bull center

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/gallery/bull-offset.png" alt="Reference patterns: SAXS and WAXS — bull offset" width="300">

bull offset

## Background and MASK in the same workflow

Choose background files and frames, verify exposure and scaling, and inspect automatic and manual exclusion regions. The background-operation screenshot uses an explicitly named synthetic constant background, not a measured matched blank. Missing or masked signal is excluded, not reconstructed.

Background and MASK in the same workflow — standard background

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-background.png" alt="Background and MASK in the same workflow — standard background" width="1100">

Background and MASK in the same workflow — standard mask

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-mask.png" alt="Background and MASK in the same workflow — standard mask" width="1100">

## SAXS: compare regions and azimuthal curves

The AgBh radial example uses accompanying geometry and log₁₀(I) display. The anonymized azimuthal demo selects three different samples: drag the inner/outer ring boundaries and compare three curves. The region follows during a drag; full-precision curves update on release. Candidate demonstration geometry is not an experimental calibration result.

SAXS: compare regions and azimuthal curves — standard saxs regions

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-saxs-regions.gif" alt="SAXS: compare regions and azimuthal curves — standard saxs regions" width="1100">

SAXS: compare regions and azimuthal curves — saxs azimuth drag

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/saxs-azimuth-drag.gif" alt="SAXS: compare regions and azimuthal curves — saxs azimuth drag" width="1100">

## WAXS: diffraction rings and multi-sample azimuthal profiles

The public alumina example uses accompanying geometry and excludes values above 65000 following its tutorial. The anonymized azimuthal demo selects three different samples and compares their curves after changing the shared annulus. 0° and 360° are the same direction; a peak crossing the boundary appears at both ends. Independent display limits are used.

WAXS: diffraction rings and multi-sample azimuthal profiles — standard waxs

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-waxs.png" alt="WAXS: diffraction rings and multi-sample azimuthal profiles — standard waxs" width="1100">

WAXS: diffraction rings and multi-sample azimuthal profiles — waxs azimuth drag

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/waxs-azimuth-drag.gif" alt="WAXS: diffraction rings and multi-sample azimuthal profiles — waxs azimuth drag" width="1100">

## Curve styling and image export

Select curves, edit line styles, fonts, legends and individual axis ticks, then export data and images with chosen names. The selected demos show four AgBh region curves, stable-size 2D export previews, Viridis/Magma/Cividis colour changes, and three actual reference-image exports. The current 2D dialog does not provide every bold-font and four-axis option available for curves.

Curve styling and image export — standard curve style

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-curve-style.gif" alt="Curve styling and image export — standard curve style" width="1100">

Curve styling and image export — standard format edit

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-format-edit.gif" alt="Curve styling and image export — standard format edit" width="1100">

Curve styling and image export — standard color preview

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/standard-color-preview.gif" alt="Curve styling and image export — standard color preview" width="1100">

Curve styling and image export — batch 2d export

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/batch-2d-export.gif" alt="Curve styling and image export — batch 2d export" width="1100">

## SAXS batch processing with one saved recipe

Save and reload geometry, background, MASK and integration settings, then process the checked files. This demonstration processed 45 different anonymized SAXS files and shows three representative curves after completion. Names and paths are redacted. Edited GIF dwell times are not a processing-speed benchmark.

SAXS batch processing with one saved recipe — saxs batch

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/saxs-batch.gif" alt="SAXS batch processing with one saved recipe — saxs batch" width="1100">

## A processing workflow in three steps

1. Import files and select datasets/frames. Keep HDF5 master and linked data files together. 2. Confirm geometry, background, MASK and region on representative inputs. 3. Compare curves, reuse a recipe and export results with sample, frame and unit information.

## Which data can you process?

Scientific TIFF and supported HDF5/NeXus arrays, selected IMG/GFRM/SFRM/CBF/EDF variants, and existing TXT/CSV/TSV/DAT curves can enter the applicable workflows. XLSX is an output format, not a promised external curve importer. Some variants have synthetic tests only. Proprietary XRD scan parsing, Rietveld refinement and dedicated grazing-incidence analysis are outside the current advertised scope.

[Data and analysis scope](https://github.com/lpp980955-lab/XRelay/blob/main/DATA_COVERAGE.en.md)

## Let your AI assistant run the workflow

Optional local MCP stdio lets an external assistant inspect datasets and geometry, submit supported integration/fitting/export tasks, and retrieve progress and workbooks. It uses the desktop processing core. Templates include WorkBuddy, Codex, Claude Code, Gemini CLI and Grok Build CLI; DeepSeek Harness uses its native MCP bridge. Template availability is not a claim that every client/version passed end-to-end testing. The workflow animation replays actual MCP calls, not a recorded model conversation. Ordinary desktop use needs no AI account; model access and data transmission depend on the client.

Let your AI assistant run the workflow — agent workflow

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/agent-workflow.gif" alt="Let your AI assistant run the workflow — agent workflow" width="1100">

Let your AI assistant run the workflow — agent connect

<img src="https://raw.githubusercontent.com/lpp980955-lab/XRelay/main/assets/agent-connect.gif" alt="Let your AI assistant run the workflow — agent connect" width="1100">

[Connect a client and try a task](https://github.com/lpp980955-lab/XRelay/blob/main/AGENT_QUICKSTART.en.md)

## Download the latest release

This first release offers only the latest Windows x64 complete package and corresponding source materials. No older binary or patch is being offered. Only the latest complete app and corresponding source archives are offered. Consult the release assets for sizes and SHA-256; previous binaries and patches are not offered.

[Download Windows x64](https://github.com/lpp980955-lab/XRelay/releases/download/v1.0.2/XRelay-1.0.2-Windows-x64.zip)  · [Corresponding source](https://github.com/lpp980955-lab/XRelay/releases/download/v1.0.2/XRelay-1.0.2-corresponding-source.zip)  · [SHA-256](https://github.com/lpp980955-lab/XRelay/releases/download/v1.0.2/SHA256SUMS.txt)  · [Asset registry](https://github.com/lpp980955-lab/XRelay/blob/main/release.json)  · [Release information](https://github.com/lpp980955-lab/XRelay/blob/main/RELEASE-v1.0.2.en.md)

Updates remain manual. Future full releases or version-specific patches will be offered only when appropriate. Diagnostics are recorded locally and never uploaded automatically. The selected media are retained under the user’s instruction with source attribution. Updates are downloaded manually.

Updates remain manual. Future full releases or version-specific patches will be offered only when appropriate. Diagnostics are recorded locally and never uploaded automatically. The selected media are retained under the user’s instruction with source attribution. Updates are downloaded manually.

## Download and start

Extract the complete portable folder and run XRelay.exe; no separate Python installation is required. Keep the dependencies, offline manual, notices and accompanying sources. The current engineering checks used Windows 11 x64; other systems and machines need validation. The app is unsigned: verify release checksums and do not disable security protection for files from unknown sources. External AI clients are optional. Corresponding-source.zip contains build/source materials, not the runnable app; GitHub snapshots do not replace all accompanying source materials.

[Actual verification scope (Chinese)](https://github.com/lpp980955-lab/XRelay/blob/main/VERIFICATION.md)  · [AI connection guide](https://github.com/lpp980955-lab/XRelay/blob/main/AGENT_QUICKSTART.en.md)

## Before processing your data

Check wavelength, distance, beam centre and pixel size; verify background/exposure and MASK; keep processing bins, corrections and normalization consistent for comparisons. Display axes and colour limits are distinct from integration settings. Test representative files before batch processing; preserve raw data and parameter records. Review fitting ranges, residuals and physical assumptions. Diagnostics stay local unless you export/share them. Check paths, sample names and keys before posting feedback.

## Methods and references

Radial integration calls pyFAI with the selected bins, region, pixel splitting and corrections. FabIO participates in detector-image reading; h5py reads HDF5. Peak fitting uses SciPy least_squares. Guinier and Porod candidate fits use explicit transformed-coordinate ranges; orientation analysis needs a defined reference and angular coverage. Cite the methods actually used, not just a dependency list. Read the linked implementation notes and original sources.

[Methods and references](https://github.com/lpp980955-lab/XRelay/blob/main/METHODS.en.md)  · [pyFAI paper](https://doi.org/10.1107/S1600576715004306)  · [FabIO paper](https://doi.org/10.1107/S0021889813000150)

## Questions before downloading

Start with one representative sample and the bundled offline manual. Routine computation is local; external AI transmission follows the client configuration. For issues, report the version, display scaling and reproduction steps, and review an exported diagnostic ZIP before sharing. Updates are downloaded manually; input paths and geometry need checking on another computer.

[Report an issue (template in Chinese)](https://github.com/lpp980955-lab/XRelay/blob/main/.github/ISSUE_TEMPLATE/bug_report.md)

## Citation and licence

Developer signature: Jiutian Zhang, the developer’s established pen name. Cite XRelay, the actual version and the real release URL, together with your geometry, background, MASK, integration and model choices. No software DOI or software paper is currently assigned. The XRelay licence permits academic/enterprise use and commercial data processing, but requires permission to sell renamed or repackaged software versions. It covers eligible original material only. Third-party rights and earlier lawful grants remain unchanged.

[Use and redistribution licence](https://github.com/lpp980955-lab/XRelay/blob/main/LICENSE)  · [Methods to cite](https://github.com/lpp980955-lab/XRelay/blob/main/METHODS.en.md)

Developer: Jiutian Zhang (pen name). Feedback and licensing enquiries: project GitHub Issues.

External reference data: [AgBh tutorial](https://www.silx.org/doc/pyFAI/latest/usage/cookbook/calibration_with_jupyter.html)  · [alumina tutorial](https://www.silx.org/doc/pyFAI/latest/usage/cookbook/integration_with_python.html)  · [pyFAI reference datasets](https://www.silx.org/pub/pyFAI/testimages/) . Origins and the user’s display instruction are recorded; no independent third-party written permission is claimed. Raw inputs and identity maps are not included.

[Repository overview](https://github.com/lpp980955-lab/XRelay/blob/main/README.md)  · [中文版](https://lpp980955-lab.github.io/XRelay/index.html)

## Source and release materials

[Build instructions](BUILD.md) · [Corresponding sources and checksums](https://github.com/lpp980955-lab/XRelay/releases/tag/v1.0.2) · [Verification scope](VERIFICATION.md) · [Citation metadata](CITATION.cff)
