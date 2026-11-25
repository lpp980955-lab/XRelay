# Data and analysis scope

[English overview](README.md) · [Detailed Chinese scope](DATA_COVERAGE.md)

| Input | Scope and conditions |
|---|---|
| Scientific TIFF | Scalar detector images; selected TVX/PILATUS/Rigaku paths. A colour photograph is not detector counts |
| HDF5 / NeXus | Supported 2D or frame/y/x numerical datasets; choose ambiguous datasets/frames explicitly. Keep linked master/data files intact |
| IMG / GFRM / SFRM / CBF / EDF | Existing adapters; some variants have constructed tests only. An extension is not a compatibility guarantee |
| TXT / CSV / TSV / DAT curves | Explicit columns, encoding, units and radial/azimuthal type; q/s/2θ conversions need the required geometry/wavelength |
| Background / MASK | Compatible shape, frame, units and exposure/scaling. Not every external MASK format is validated |
| XLSX | Result export; external XLSX curve import is not in the advertised input scope |

Workflows include radial and azimuthal integration, multi-sample/frame comparison, recipe batches, applicable peak fitting, candidate SAXS model analysis and data/image export. Orientation calculations require defined directions and angular support. Crystallinity decomposition and Ruland analysis retain scientific acceptance conditions and are not unconditional quantitative promises.

Not advertised: proprietary XRD raw-scan parsing, complete Rietveld refinement, dedicated GISAXS/GIWAXS models, 3D reciprocal reconstruction, arbitrary multi-module/energy axes or live acquisition append-reading. Exported 2θ–I text may be imported as a generic curve without supporting its proprietary source format.

Real examples, synthetic tests, source features and frozen-app checks are different evidence levels. [Actual release verification, Chinese](VERIFICATION.md). Missing geometry/units must be supplied, not guessed from names. Converting processed arrays does not promise preservation of a vendor's proprietary schema.
