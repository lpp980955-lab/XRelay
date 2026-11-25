"""Build a version-specific file overlay; never download, install or update an app.

Compare two complete, hash-verified portable releases. Unchanged runtime libraries
and accompanying source archives are reused from the explicitly named base.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path,PurePosixPath
import zipfile

PAYLOAD='覆盖到软件目录'


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def read_release(directory):
    root=Path(directory).resolve(strict=True)
    manifest=root/'SHA256SUMS.txt'
    files={};seen=set()
    for line in manifest.read_text(encoding='utf-8-sig').splitlines():
        if not line:continue
        digest,relative=line.split('  ',1)
        path=PurePosixPath(relative)
        if (not re.fullmatch('[0-9a-f]{64}',digest) or path.is_absolute() or
                '..' in path.parts or '\\' in relative or ':' in relative or
                str(path)!=relative or relative.casefold() in seen):
            raise ValueError('Unsafe or duplicate release manifest path')
        seen.add(relative.casefold())
        actual=root.joinpath(*path.parts)
        if any(part.is_symlink() for part in (actual,*actual.parents)):
            raise ValueError('Release symlinks are not supported')
        actual.resolve(strict=True).relative_to(root)
        if not actual.is_file() or sha(actual)!=digest:
            raise ValueError(f'Release checksum mismatch: {relative}')
        files[relative]=dict(sha256=digest,bytes=actual.stat().st_size)
    inventory={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if inventory!=set(files)|{'SHA256SUMS.txt'}:
        raise ValueError('Release contains unlisted files or missing manifest entries')
    for name in ('XRelay.exe','XRelay-Agent.exe','LICENSE','licenses/release-status.json'):
        if name not in files:raise ValueError(f'Incomplete portable release: {name}')
    status=json.loads((root/'licenses/release-status.json').read_text(encoding='utf-8'))
    version=status['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Expected a three-part release version')
    return dict(root=root,version=version,files=files,manifest_sha256=sha(manifest))


def same_zip_members(a,b):
    try:
        with zipfile.ZipFile(a) as left,zipfile.ZipFile(b) as right:
            names=left.namelist()
            return len(names)==len(set(names)) and set(names)==set(right.namelist()) and len(names)==len(right.namelist()) and all(left.read(n)==right.read(n) for n in names)
    except (OSError,zipfile.BadZipFile):return False


def plan_patch(base,target):
    old=read_release(base);new=read_release(target)
    version=lambda text:tuple(map(int,text.split('.')))
    if version(new['version'])<=version(old['version']):raise ValueError('Target version must be newer than base')
    old_files,new_files=old['files'],new['files']
    changed=sorted(name for name,entry in new_files.items() if entry!=old_files.get(name))
    removed=sorted(set(old_files)-set(new_files))
    retained=[name for name in removed if re.fullmatch(r'_sources/XRelay-\d+\.\d+\.\d+-source\.zip',name)]
    reasons=[]
    if version(old['version'])[:2]!=version(new['version'])[:2]:
        reasons.append('Major/minor version changed: use a new complete runtime baseline.')
    for name in removed:
        if name not in retained:reasons.append(f'Removal requires a clean full package: {name}')
    for name in changed:
        if not name.startswith('_internal/') or name.startswith('_internal/resources/'):continue
        if name=='_internal/base_library.zip' and name in old_files and same_zip_members(old['root']/name,new['root']/name):continue
        reasons.append(f'Runtime dependency changed: {name}')
    unchanged=sorted(name for name in new_files if name not in changed)
    return dict(schema='xrelay.manual_file_overlay/1',from_version=old['version'],to_version=new['version'],
        platform='Windows-x64',eligible=not reasons,full_package_reasons=reasons,
        base_manifest_sha256=old['manifest_sha256'],target_manifest_sha256=new['manifest_sha256'],
        changed_files={name:new_files[name] for name in changed},
        retained_old_source_archives=retained,
        reused_files=len(unchanged),reused_bytes=sum(new_files[name]['bytes'] for name in unchanged),
        payload_bytes=sum(new_files[name]['bytes'] for name in changed),
        total_target_bytes=sum(item['bytes'] for item in new_files.values()),
        application='manual copy only; no updater, deletion, network or automatic version checking')


def instructions(plan):
    return f'''XRelay 手动更新补丁：{plan['from_version']} → {plan['to_version']}

仅用于 Windows x64 官方完整包 {plan['from_version']}，不能独立运行。
如果当前版本不符、文件曾被手动删改，或不确定旧目录是否完整，请下载最新版完整包。

1. 在旧软件“帮助 → 关于”确认版本为 {plan['from_version']}。
2. 关闭 XRelay、XRelay-Agent 和相关后台任务。
3. 将整个旧软件文件夹复制一份作为本地备份。
4. 解压本补丁，将“{PAYLOAD}”文件夹内的全部内容复制到旧软件目录
   （原 XRelay.exe 所在目录），选择替换同名文件，保留原来的 _internal 等目录。
   请复制文件夹内的内容，不要把“{PAYLOAD}”文件夹本身放入软件目录。
5. 启动软件，确认版本为 {plan['to_version']}，再检查常用功能。
6. 如果失败，退出软件后使用第3步的完整备份恢复；也可以另解压最新版完整包。

本补丁不会自动执行、联网、检查版本或替你更新文件。当前版本核对与文件复制由使用者完成。
补丁不包含个人数据、样式或几何参数库；不要删除 LocalAppData/XRelay 或自行清空注册表设置。
未变化的运行依赖、许可证和第三方对应源码沿用旧完整包；新版原创源码在 _sources 内。
旧版的原创源码ZIP可以保留，不参与软件运行；新清单只列出目标版本文件。
patch-manifest.json 记录适用版本、变化文件和哈希。SHA-256用于检查文件一致性，不是代码签名。
'''


def build_patch(base,target,output):
    plan=plan_patch(base,target)
    if not plan['eligible']:raise ValueError('Full package required:\n'+'\n'.join(plan['full_package_reasons']))
    root=Path(target).resolve(strict=True);output=Path(output).resolve()
    for release in (Path(base).resolve(),root):
        if output.is_relative_to(release):raise ValueError('Patch output must be outside both releases')
    checksum=output.with_name(output.name+'.sha256')
    if output.exists() or checksum.exists():raise FileExistsError('Patch output already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation keeps a previous reviewed patch immutable by default.
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        archive.writestr('更新说明.txt',instructions(plan).encode('utf-8-sig'))
        archive.writestr('patch-manifest.json',json.dumps(plan,ensure_ascii=False,indent=2))
        for name,entry in plan['changed_files'].items():
            if sha(root/name)!=entry['sha256']:raise ValueError(f'Target changed during patch creation: {name}')
            archive.write(root/name,PAYLOAD+'/'+name)
        if sha(root/'SHA256SUMS.txt')!=plan['target_manifest_sha256']:raise ValueError('Target manifest changed')
        archive.write(root/'SHA256SUMS.txt',PAYLOAD+'/SHA256SUMS.txt')
    # Verify every archived byte against the complete target before handoff.
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:raise ValueError('Patch ZIP integrity failure')
        for name,entry in plan['changed_files'].items():
            if hashlib.sha256(archive.read(PAYLOAD+'/'+name)).hexdigest()!=entry['sha256']:
                raise ValueError(f'Archived checksum mismatch: {name}')
    checksum.write_text(f'{sha(output)}  {output.name}\n',encoding='ascii')
    return dict(plan,zip_bytes=output.stat().st_size,zip_sha256=sha(output))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=build_patch(args.base,args.target,args.output) if args.output else plan_patch(args.base,args.target)
    print(json.dumps(result,ensure_ascii=False,indent=2))
