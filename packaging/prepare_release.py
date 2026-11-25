"""Prepare a private release from a new frozen build and verified vendor materials.

No upload, installation, user-setting writes, downloads or recursive deletion.
"""
import argparse,ast,hashlib,json,re,runpy,shutil,zipfile
from pathlib import Path
from build_manual_patch import read_release,same_zip_members
from support_materials import write_support_files
ROOT=Path(__file__).resolve().parents[1]
VERSION=runpy.run_path(str(ROOT/'src/saxs_waxs_ui/branding.py'))['VERSION']
PRIVATE=re.compile(r'[A-Za-z]:[\\/](?:Users|Desktop\ssoft)|\b\d{2}[A-Z]{3}-[A-Z]{2}\b|\bwtj\d{7}|\b(?:10|5)PBOA(?=[_\-])|LPP-F\d|My\sData|wxid_|xwechat_files',re.I)
BUILD_FILES=('prepare_release.py','build_windows_candidate.ps1','workbench.spec','workbench_entry.py',
    'version_info.txt','windows_preflight.py','build_manual_patch.py','support_materials.py','test_public_source.py',
    'check_windows_offline.ps1','audit_windows_candidate.py','requirements-ui-prototype-lock.txt',
    'requirements-pyfai-prototype-lock.txt','requirements-windows-build-lock.txt')

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def safe_source_files():
    selected=[];excluded=[]
    for folder in ('src','resources'):
        for path in (ROOT/folder).rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'):continue
            if path.suffix.lower() in ('.py','.html','.json','.md','.txt','.ps1','.cmd'):
                if PRIVATE.search(path.read_text(encoding='utf-8-sig')):raise ValueError('Private content in required source: '+str(path.relative_to(ROOT)))
            selected.append(path)
    for name in BUILD_FILES:selected.append(ROOT/'packaging'/name)
    selected.extend((ROOT/'packaging/fixtures').glob('*.tif'))
    helper=ROOT/'validation/generate_truth_sets.py'
    assert not PRIVATE.search(helper.read_text(encoding='utf-8'))
    selected.append(helper)
    # Preserve shareable synthetic/unit tests; do not publish local-data tests.
    for folder in ('tests','tests_ui'):
        for path in (ROOT/folder).glob('*.py'):
            text=path.read_text(encoding='utf-8-sig')
            if PRIVATE.search(text) or re.search(r'[\'\"]data[/\\]|[\'\"]1003saxs|data[\'\"]\s*/',text):
                excluded.append(path.relative_to(ROOT).as_posix());continue
            selected.append(path)
    # Do not ship tests whose local fixture/helper imports were excluded.
    changed=True
    while changed:
        changed=False;modules={'.'.join(p.relative_to(ROOT).with_suffix('').parts) for p in selected if p.suffix=='.py'}
        for path in tuple(selected):
            if path.parent.name not in ('tests','tests_ui'):continue
            tree=ast.parse(path.read_text(encoding='utf-8-sig'))
            deps=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module and n.module.startswith(('tests.','tests_ui.','validation.'))]
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):
                    deps.extend('tests.'+a.name for a in node.names if a.name.startswith('test_'))
                elif isinstance(node,ast.ImportFrom) and node.module and node.module.startswith('test_'):deps.append('tests.'+node.module)
            if any(dep not in modules for dep in deps):
                selected.remove(path);excluded.append(path.relative_to(ROOT).as_posix());changed=True
    return sorted(set(selected)),excluded

def create_source_archive(destination):
    files,excluded=safe_source_files()
    build_readme=f'''# XRelay {VERSION} source

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
'''
    inventory=[]
    with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path in files:
            relative=path.relative_to(ROOT).as_posix();archive.write(path,f'XRelay-{VERSION}/'+relative)
            inventory.append(dict(path=relative,sha256=sha(path),bytes=path.stat().st_size))
        archive.write(ROOT/'LICENSE',f'XRelay-{VERSION}/LICENSE')
        archive.write(ROOT/'LICENSE_METADATA.json',f'XRelay-{VERSION}/LICENSE_METADATA.json')
        archive.write(ROOT/'LIBRARY_REPLACEMENT.md',f'XRelay-{VERSION}/LIBRARY_REPLACEMENT.md')
        archive.writestr(f'XRelay-{VERSION}/tests/__init__.py','')
        archive.writestr(f'XRelay-{VERSION}/README.md',build_readme)
        archive.writestr(f'XRelay-{VERSION}/SOURCE_MANIFEST.json',json.dumps(inventory,indent=2))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            assert '/data/' not in name and '/artifacts/' not in name and '/docs/' not in name
            assert '/validation/' not in name or name.endswith('/validation/generate_truth_sets.py')
    return dict(files=len(files),bytes=destination.stat().st_size,sha256=sha(destination),excluded_tests=excluded,manifest=inventory)

def main(stage,base):
    stage=stage.resolve(strict=True);base=base.resolve(strict=True)
    if stage.parent!=ROOT/'artifacts' or not stage.name.startswith('windows_candidate_'):raise ValueError('Unexpected build stage')
    old=read_release(base)  # Check every byte before reusing vendor materials.
    bundle=stage/'dist/XRelay';new_internal=bundle/'_internal';old_internal=base/'_internal'
    def vendor_files(root):return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file() and 'resources' not in p.relative_to(root).parts}
    previous,current=vendor_files(old_internal),vendor_files(new_internal)
    if previous.get('base_library.zip')!=current.get('base_library.zip') and same_zip_members(old_internal/'base_library.zip',new_internal/'base_library.zip'):
        previous['base_library.zip']=current['base_library.zip']
    if previous!=current:
        changed=[n for n in set(previous)|set(current) if previous.get(n)!=current.get(n)]
        raise ValueError('Vendor payload changed; review before reusing materials: '+json.dumps(changed[:30]))
    release=ROOT/f'artifacts/releases/XRelay-{VERSION}-Windows-x64'
    if release.exists():raise FileExistsError(release)
    shutil.copytree(bundle,release)
    shutil.copytree(base/'licenses',release/'licenses')
    sources=release/'_sources';sources.mkdir()
    records=json.loads((base/'_sources/SOURCE_INDEX.json').read_text(encoding='utf-8-sig'))
    for record in records:
        if not record.get('included_in_source_folder'):continue
        source=base/'_sources'/record['file'];assert sha(source)==record['sha256']
        shutil.copy2(source,sources/source.name)
    shutil.copy2(base/'_sources/SOURCE_INDEX.json',sources/'SOURCE_INDEX.json')
    source_report=create_source_archive(sources/f'XRelay-{VERSION}-source.zip')
    shutil.copy2(ROOT/'LICENSE',release/'LICENSE')
    shutil.copy2(ROOT/'LICENSE_METADATA.json',release/'LICENSE_METADATA.json')
    shutil.copy2(ROOT/'LIBRARY_REPLACEMENT.md',release/'LIBRARY_REPLACEMENT.md')
    (sources/'README.txt').write_text((base/'_sources/README.txt').read_text(encoding='utf-8-sig').replace(old['version'],VERSION).replace('资源、测试、构建脚本','资源、经筛选测试、构建脚本'),encoding='utf-8-sig')
    (release/'README.md').write_text(f'# XRelay {VERSION}\n\n完整解压后运行 XRelay.exe。操作手册.html 为离线图文手册。\nXRelay-Agent.exe 为可选本地 MCP 入口，普通桌面处理无需 AI 账号。\n本包没有自动检查或安装更新。源码及第三方声明位于 _sources 与 licenses。\n',encoding='utf-8')
    (release/'BRANDING.md').write_text((ROOT/'BRANDING.md').read_text(encoding='utf-8').replace('版本：1.0.0','版本：'+VERSION),encoding='utf-8')
    (release/'先读我.txt').write_text(f'''XRelay {VERSION} · Windows x64
完整解压整个目录后运行 XRelay.exe，无需另装 Python。
不要只复制 EXE，不要删除 _internal、licenses 或 _sources。
操作手册.html 提供离线教程。XRelay-Agent.exe 为可选 MCP 入口。
程序不自动更新、不自动上传诊断；用户设置与实验数据不作为补丁负载。
出现问题可在“帮助”导出诊断，或运行“导出诊断报告.cmd”。
未进行代码签名。仅从维护者实际发布的附件下载并核对 SHA-256。
本次仅在本机隔离环境验收；第二台电脑及其他系统仍需单独确认。
''',encoding='utf-8-sig')
    for name in ('创建桌面快捷方式.cmd','create-shortcut.ps1','操作手册.html'):
        content=(base/name).read_text(encoding='utf-8-sig').replace(old['version'],VERSION)
        (release/name).write_text(content,encoding='utf-8-sig')
    write_support_files(release)
    shutil.copy2(ROOT/'docs/public-release/RELEASE-v1.0.2.md',release/'更新说明.md')
    status=dict(version=VERSION,engineering='pending_current_frozen_checks',license='LicenseRef-XRelay-Free-Use-1.0',
        license_materials='matching_vendor_payload_hashes_and_accompanying_sources',
        scientific_acceptance=False,clean_machine_verified=False,second_machine_verified=False,
        code_signed=False,personal_contact_published=False,source_manifest_sha256=source_report['sha256'])
    (release/'licenses/release-status.json').write_text(json.dumps(status,indent=2),encoding='utf-8')
    # License evidence is retained, but no private build-path inventories ship.
    for path in (release/'licenses').rglob('*.json'):
        text=path.read_text(encoding='utf-8-sig')
        if str(ROOT) in text or str(ROOT).replace('\\','\\\\') in text:
            text=text.replace(str(ROOT).replace('\\','\\\\'),'[build-root]').replace(str(ROOT),'[build-root]')
            path.write_text(text,encoding='utf-8')
    checksums=[f'{sha(p)}  {p.relative_to(release).as_posix()}' for p in sorted(release.rglob('*')) if p.is_file()]
    (release/'SHA256SUMS.txt').write_text('\n'.join(checksums)+'\n',encoding='utf-8')
    read_release(release)
    report=dict(version=VERSION,stage=stage.name,vendor_base=old['version'],vendor_payload_equal=True,source=source_report,release=str(release))
    (stage/'release-assembly.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(release,flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--stage',type=Path,required=True);parser.add_argument('--base',type=Path,required=True)
    args=parser.parse_args();main(args.stage,args.base)
