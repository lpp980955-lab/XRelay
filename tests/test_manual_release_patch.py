import hashlib,json,runpy,shutil,tempfile,unittest,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULE=runpy.run_path(str(ROOT/'packaging/build_manual_patch.py'))
plan_patch=MODULE['plan_patch'];build_patch=MODULE['build_patch'];PAYLOAD=MODULE['PAYLOAD']


class ManualReleasePatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'artifacts');self.root=Path(self.temp.name)
        self.base=self.make_release('1.0.2',b'old gui')
        self.target=self.make_release('1.0.3',b'new gui')

    def tearDown(self):self.temp.cleanup()

    def manifest(self,root):
        files=sorted(p for p in root.rglob('*') if p.is_file() and p.name!='SHA256SUMS.txt')
        (root/'SHA256SUMS.txt').write_text('\n'.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}' for p in files),encoding='utf-8')

    def make_release(self,version,gui):
        root=self.root/version;root.mkdir()
        for relative,data in {'XRelay.exe':gui,'XRelay-Agent.exe':b'agent','LICENSE':b'license fixture',
            '_internal/runtime.dll':b'unchanged dependency',
            '_internal/resources/manual/index.html':b'old manual',
            '_sources/third-party-source.tar.gz':b'unchanged source',
            f'_sources/XRelay-{version}-source.zip':b'matching original source '+gui,
            'licenses/release-status.json':json.dumps({'version':version}).encode()}.items():
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self.manifest(root);return root

    def test_overlay_reconstructs_all_target_files_without_repeated_runtime(self):
        updated=self.target/'_internal/resources/manual/index.html';updated.write_bytes(b'new manual');self.manifest(self.target)
        output=self.root/'small.zip';result=build_patch(self.base,self.target,output)
        self.assertTrue(result['eligible']);self.assertGreater(result['reused_bytes'],0)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn(PAYLOAD+'/_internal/runtime.dll',archive.namelist())
            self.assertNotIn(PAYLOAD+'/_sources/third-party-source.tar.gz',archive.namelist())
            self.assertIn(PAYLOAD+'/_sources/XRelay-1.0.3-source.zip',archive.namelist())
            self.assertIn('1.0.2 → 1.0.3',archive.read('更新说明.txt').decode('utf-8-sig'))
            self.assertFalse(any(n.endswith(('.ps1','.cmd','.bat')) for n in archive.namelist()))
            applied=self.root/'applied';shutil.copytree(self.base,applied)
            for name in archive.namelist():
                if not name.startswith(PAYLOAD+'/'):continue
                path=applied/name.removeprefix(PAYLOAD+'/');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.read(name))
        for path in self.target.rglob('*'):
            if path.is_file():self.assertEqual((applied/path.relative_to(self.target)).read_bytes(),path.read_bytes())
        self.assertTrue((applied/'_sources/XRelay-1.0.2-source.zip').exists())
        self.assertEqual(output.with_name(output.name+'.sha256').read_text().split()[0],hashlib.sha256(output.read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):build_patch(self.base,self.target,output)

    def test_dependency_change_or_addition_demands_full_package(self):
        for relative in ('_internal/runtime.dll','_internal/new.dll'):
            with self.subTest(relative=relative):
                path=self.target/relative;old=path.read_bytes() if path.exists() else None
                path.write_bytes(b'new dependency');self.manifest(self.target)
                self.assertFalse(plan_patch(self.base,self.target)['eligible'])
                with self.assertRaisesRegex(ValueError,'Full package required'):build_patch(self.base,self.target,self.root/'no.zip')
                self.assertFalse((self.root/'no.zip').exists())
                path.write_bytes(old) if old is not None else path.unlink()
        self.manifest(self.target)

    def test_removal_is_rejected_but_retired_original_source_can_remain(self):
        self.assertTrue(plan_patch(self.base,self.target)['eligible'])
        (self.target/'_internal/resources/manual/index.html').unlink();self.manifest(self.target)
        result=plan_patch(self.base,self.target)
        self.assertFalse(result['eligible']);self.assertTrue(any('Removal' in why for why in result['full_package_reasons']))

    def test_corrupt_or_unlisted_files_are_rejected(self):
        (self.target/'XRelay.exe').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):plan_patch(self.base,self.target)
        self.manifest(self.target);(self.target/'private-data.h5').write_bytes(b'private')
        with self.assertRaisesRegex(ValueError,'unlisted'):plan_patch(self.base,self.target)

    def test_paths_case_collisions_and_output_inside_release_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'outside'):build_patch(self.base,self.target,self.target/'patch.zip')
        manifest=self.target/'SHA256SUMS.txt';text=manifest.read_text()
        for name in ('../outside','C:/outside','_internal\\outside.dll','XRelay.exe'):
            manifest.write_text(text+'\n'+'0'*64+'  '+name)
            with self.assertRaisesRegex(ValueError,'Unsafe or duplicate'):plan_patch(self.base,self.target)

    def test_cumulative_patch_and_new_minor_baseline(self):
        newest=self.make_release('1.0.6',b'newest gui')
        self.assertTrue(plan_patch(self.base,newest)['eligible'])
        minor=self.make_release('1.1.0',b'new minor')
        self.assertFalse(plan_patch(self.base,minor)['eligible'])
        with self.assertRaisesRegex(ValueError,'newer'):plan_patch(self.target,self.base)

    def test_standard_library_container_only_change_is_allowed(self):
        for root,year in ((self.base,2020),(self.target,2021)):
            with zipfile.ZipFile(root/'_internal/base_library.zip','w') as archive:
                archive.writestr(zipfile.ZipInfo('encodings.pyc',date_time=(year,1,1,0,0,0)),b'same stdlib')
            self.manifest(root)
        self.assertTrue(plan_patch(self.base,self.target)['eligible'])
        with zipfile.ZipFile(self.target/'_internal/base_library.zip','w') as archive:archive.writestr('encodings.pyc',b'new stdlib')
        self.manifest(self.target);self.assertFalse(plan_patch(self.base,self.target)['eligible'])
