from pathlib import Path

from PyInstaller.utils.hooks import collect_all

packaging_dir = Path(SPEC).resolve().parent
root = packaging_dir.parent
datas = [(str(root / "resources"), "resources")]
binaries = []
hiddenimports = []
for module in ("fabio", "pyFAI", "hdf5plugin"):
    module_data, module_binaries, module_imports = collect_all(module, include_py_files=False,
        exclude_datas=['**/test/**','**/tests/**','**/testimages/**','**/__pycache__/**'],
        filter_submodules=lambda name: not any(part in {'test','tests','testimages','benchmarks'} for part in name.split('.')))
    datas.extend(module_data)
    binaries.extend(module_binaries)
    hiddenimports.extend(module_imports)

analysis = Analysis(
    [str(packaging_dir / "workbench_entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
# Locked Qt uses Windows ICU. Avoid unrelated ICU DLLs found on the build PATH.
analysis.binaries = [
    entry for entry in analysis.binaries
    if not (Path(entry[0]).parent == Path(".")
            and Path(entry[0]).name.lower().startswith("icu")
            and Path(entry[0]).suffix.lower() == ".dll")
]
archive = PYZ(analysis.pure)
gui = EXE(archive, analysis.scripts, exclude_binaries=True,
          name="XRelay", console=False, upx=False, icon=str(root / "resources/branding/xrelay.ico"), version=str(packaging_dir / "version_info.txt"))
agent = EXE(archive, analysis.scripts, exclude_binaries=True,
            name="XRelay-Agent", console=True, upx=False, icon=str(root / "resources/branding/xrelay.ico"))
COLLECT(gui, agent, analysis.binaries, analysis.datas, upx=False,
        name="XRelay")
