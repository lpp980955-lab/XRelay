"""Inventory the local Windows build environment without installing packages."""
import argparse
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def inventory():
    locked = {}
    for lock in sorted((ROOT / "packaging").glob("*-lock.txt")):
        for name, version in re.findall(r"(?m)^([\w.-]+)==([^\s]+)", lock.read_text(encoding="utf-8")):
            key = canonical(name)
            if key in locked and locked[key] != version:
                raise ValueError(f"Conflicting pins for {name}")
            locked[key] = version
    distributions = []
    for dist in sorted(metadata.distributions(), key=lambda item: canonical(item.metadata["Name"])):
        name = dist.metadata["Name"]
        key = canonical(name)
        notices = []
        for file in dist.files or ():
            if any(part.lower() in {"license", "licenses", "license.txt", "license.md",
                                    "notice", "notice.txt", "copying"} for part in file.parts):
                path = dist.locate_file(file)
                if path.is_file():
                    notices.append({"path": str(file), "sha256": sha256(path.read_bytes()).hexdigest()})
        distributions.append({
            "name": name, "version": dist.version, "locked_version": locked.get(key),
            "license_expression": dist.metadata.get("License-Expression"),
            "license_classifiers": [value for value in dist.metadata.get_all("Classifier", [])
                                    if value.startswith("License ::")],
            "notices": notices,
        })
    installed = {canonical(item["name"]): item["version"] for item in distributions}
    resources = [
        {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in sorted((ROOT / "resources").rglob("*")) if path.is_file()
    ]
    return {
        "scope": "local environment inventory, not frozen bundle SBOM",
        "python": platform.python_version(), "platform": platform.platform(),
        "machine": platform.machine(), "pyinstaller": installed.get("pyinstaller"),
        "missing_locked": sorted(set(locked) - installed.keys()),
        "version_mismatches": {name: {"locked": version, "installed": installed[name]}
                               for name, version in locked.items()
                               if name in installed and installed[name] != version},
        "distributions": distributions, "resources": resources,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    artifacts = (ROOT / "artifacts").resolve()
    if artifacts not in output.parents:
        parser.error("inventory output must be inside workspace artifacts")
    result = inventory()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"{len(result['distributions'])} distributions, {len(result['resources'])} resources")
    print(f"Missing locked: {result['missing_locked']}; mismatches: {result['version_mismatches']}")
    print(f"PyInstaller: {result['pyinstaller']}")
    return int(bool(result["missing_locked"] or result["version_mismatches"]))


if __name__ == "__main__":
    sys.exit(main())
