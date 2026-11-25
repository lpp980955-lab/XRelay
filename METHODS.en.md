# Methods and references

This describes version1.0.2 implementation, not validation for every experiment. [Detailed implementation notes, Chinese](METHODS.md).

- Geometry: check wavelength, distance, centre, pixel size and rotation convention. Some preview/automatic candidates use a zero-tilt approximation and need manual confirmation.
- Radial integration: pyFAI integrate1d_ng with actual bins, range, mask, splitting, normalization and selected corrections.
- Azimuthal integration: an explicit annulus and angular bins; no invented signal in gaps/occlusions. 0° and360° are the same direction.
- Background: selected frames and exposure/scaling strategy; unknown exposure is not a measured equality.
- Reading: FabIO/adapters and h5py for supported structures; same suffix does not prove universal vendor support.
- Peak fitting: SciPy least_squares with chosen model, bounds, background and domain; convergence is not proof of a unique physical interpretation.
- Guinier: ln(I) versus q² over an explicit interval; Rg²=-3×slope for a negative slope. Check background, positive intensity and applicable q range.
- Porod: ln(I) versus ln(q) over an explicit positive domain; report the negative slope exponent without claiming every fitted range is an ideal asymptote.
- Orientation: define reference direction and observed angular support; selected/truncated domains are not complete orientation distributions.
- Demonstrations: real Qt states/tool responses with edited timing. Independent colour limits are display settings, not intensity normalization or performance evidence.

References:

1. Ashiotis, G. et al. The fast azimuthal integration Python library: pyFAI. Journal of Applied Crystallography48,510–519(2015). https://doi.org/10.1107/S1600576715004306
2. Knudsen, E. B. et al. FabIO: easy access to two-dimensional X-ray detector images in Python. Journal of Applied Crystallography46,537–539(2013). https://doi.org/10.1107/S0021889813000150
3. [pyFAI geometry](https://pyfai.readthedocs.io/en/stable/geometry.html).
4. [SciPy least_squares](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html).

Cite the models/methods and data actually used. Dependency papers do not certify the entire workflow. Gallery visualizations are retained under the user’s instruction, with origins recorded. No independent third-party written permission is claimed; raw research arrays and identity maps are not included.
