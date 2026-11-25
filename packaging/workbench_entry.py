"""Dispatch the two frozen launchers sharing the same runtime."""
from pathlib import Path
import sys

if __name__ == "__main__":
    if len(sys.argv)>1 and sys.argv[1]=='--library-report':
        from saxs_waxs_ui.library_report import write_report
        write_report(sys.argv[2])
        raise SystemExit(0)
    if len(sys.argv)>1 and sys.argv[1]=='--export-diagnostics':
        from saxs_waxs_ui.runtime_diagnostics import export_report
        report=export_report(sys.argv[2] if len(sys.argv)>2 else None)
        if sys.stdout is not None:print(report)
        raise SystemExit(0)
    from saxs_waxs_ui.runtime_diagnostics import install_runtime_diagnostics
    install_runtime_diagnostics()
    if len(sys.argv)>1 and sys.argv[1]=="--self-check":
        from saxs_waxs_ui.release_check import main
        raise SystemExit(main(sys.argv[2]))
    if Path(sys.executable).stem.casefold() in {"saxs-waxs-agent", "xrelay-agent"}:
        from saxs_waxs_agent.__main__ import main
    else:
        from saxs_waxs_ui.app import main
    raise SystemExit(main())
