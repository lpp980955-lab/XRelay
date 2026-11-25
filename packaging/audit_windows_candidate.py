"""Generate an evidence inventory and license notices for a private frozen bundle."""
import argparse
import ast
from collections import Counter
from hashlib import file_digest
from importlib import metadata
import json
import sys
from pathlib import Path

from windows_preflight import canonical


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def audit(stage):
    bundle = stage / "dist" / "XRelay"
    build = stage / "build" / "workbench"
    collected, = ast.literal_eval((build / "COLLECT-00.toc").read_text(encoding="utf-8"))
    _, modules = ast.literal_eval((build / "PYZ-00.toc").read_text(encoding="utf-8"))
    distribution_files = {}
    distributions = {}
    for distribution in metadata.distributions():
        name = canonical(distribution.metadata["Name"])
        distributions[name] = distribution
        for relative in distribution.files or ():
            distribution_files[distribution.locate_file(relative).resolve()] = name

    represented = Counter()
    entries = []
    seen = set()
    for dest, source, kind in collected:
        source = Path(source).resolve()
        relative = Path(dest) if kind == "EXECUTABLE" else Path("_internal") / dest
        deployed = bundle / relative
        if not deployed.is_file():
            raise ValueError(f"Missing collected file: {relative}")
        seen.add(deployed)
        owner = distribution_files.get(source)
        if owner:
            represented[owner] += 1
        entries.append({
            "path": relative.as_posix(), "kind": kind, "bytes": deployed.stat().st_size,
            "sha256": digest(deployed), "distribution": owner, "build_source": str(source),
        })
    resources = json.loads((stage / "environment.json").read_text(encoding="utf-8"))["resources"]
    packaged = {entry["path"]: entry["sha256"] for entry in entries}
    for resource in resources:
        if packaged.get("_internal/" + resource["path"]) != resource["sha256"]:
            raise ValueError(f"Packaged resource differs from build input: {resource['path']}")
    extras = set(p for p in bundle.rglob("*") if p.is_file()) - seen
    if extras:
        raise ValueError(f"Bundle files not represented in COLLECT: {sorted(extras)}")
    archived = Counter(
        owner for _, source, _ in modules
        if (owner := distribution_files.get(Path(source).resolve()))
    )

    notices = {}
    components = []
    for name in sorted(represented.keys() | archived.keys()):
        distribution = distributions[name]
        texts = []
        for relative in distribution.files or ():
            if not any(part.lower().startswith(("license", "licence", "notice", "copying", "copyright"))
                       for part in relative.parts):
                continue
            path = distribution.locate_file(relative)
            if not path.is_file() or path.suffix.lower() not in ("", ".txt", ".md", ".rst", ".python"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            texts.append((relative.as_posix(), digest(path), text))
        notices[name] = texts
        components.append({
            "name": distribution.metadata["Name"], "version": distribution.version,
            "collected_files": represented[name], "archived_modules": archived[name],
            "declared_license_expression": distribution.metadata.get("License-Expression"),
            "notice_files": [{"path": relative, "sha256": checksum} for relative, checksum, _ in texts],
        })
    python_notice = Path(sys.base_prefix) / "LICENSE.txt"
    output = stage / "distribution-audit"
    output.mkdir(exist_ok=False)
    record = {
        "scope": "private candidate evidence; not an approved release SBOM",
        "files": entries, "components": components,
        "python_runtime_notice": ({"name": python_notice.name, "sha256": digest(python_notice)}
                                  if python_notice.is_file() else None),
        "unattributed_native_files": [item["path"] for item in entries
            if not item["distribution"] and item["kind"] in ("BINARY", "EXTENSION")],
        "open_review": ["Qt distribution license and corresponding obligations",
                        "HDF5 codec DLL component notices", "unattributed native files",
                        "Python runtime and other vendored native components"],
    }
    (output / "bundle-inventory.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "NOTICE-candidate.txt").open("x", encoding="utf-8") as stream:
        stream.write("Private candidate notice evidence. Distribution review pending.\n")
        if python_notice.is_file():
            stream.write("\n===== Python runtime: bundled interpreter license =====\n")
            stream.write(python_notice.read_text(encoding="utf-8", errors="replace") + "\n")
        for component in components:
            name = component["name"]
            stream.write(f"\n===== {name} {component['version']} =====\n")
            for relative, checksum, text in notices[canonical(name)]:
                stream.write(f"\n--- {relative} | SHA-256 {checksum} ---\n{text}\n")
    print(f"{len(entries)} files; {len(components)} represented distributions; "
          f"{len(record['unattributed_native_files'])} native files without wheel attribution")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    stage = args.candidate.resolve(strict=True)
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    if stage.parent != artifacts or not stage.name.startswith("windows_candidate_"):
        parser.error("choose a Windows candidate directory directly under artifacts")
    audit(stage)
