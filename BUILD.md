# XRelay 1.0.2 source

This archive contains the preferred editable application source, resources,
version-matched build scripts, dependency locks and shareable unit tests.
Experimental inputs, private research documents and demonstration recorders
are intentionally excluded. No raw research data are required to build.

On Windows x64 / Python 3.13 create an isolated environment and install the
three packaging/*-lock.txt files with pip --require-hashes. Then run:

    powershell -File packaging/build_windows_candidate.ps1 -BuildPython <venv-python.exe>

The output contains XRelay.exe and XRelay-Agent.exe sharing _internal.
For an offline functional check:

    XRelay.exe --self-check <new-output-directory>

Source mode: set PYTHONPATH=src, then python -m saxs_waxs_ui.app.
Run the supplied independent tests with python packaging/test_public_source.py;
add --ui for the public UI test subset. The runner creates temporary output
directories and configures src plus the offscreen Qt backend. Tests requiring
private inputs and their dependent test modules are intentionally not included.

LicenseRef-XRelay-Free-Use-1.0 applies to eligible original XRelay material. Third-party
components retain their own licenses. Matching vendor source archives and
notices accompany the binary release in _sources/ and licenses/. No author
email or source-request offer is required to access these accompanying files.
