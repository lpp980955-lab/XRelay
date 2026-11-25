# 第三方库修改、替换与重建 / Library modification and replacement

XRelay 原创部分的许可不限制第三方组件各自许可授予的权利，包括适用的 LGPL 库修改、替换、重新链接及调试修改所需逆向工程。完整条款以随包第三方许可原文为准。

## 随包材料

- `licenses/THIRD_PARTY_NOTICES.txt`、`SOURCE_AND_NATIVE_NOTICES.txt`：组件及原生子组件声明；保留原文。
- `_sources/SOURCE_INDEX.json`：未修改上游源码的版本、来源与SHA-256。
- `_sources/pyside-setup-*.tar.xz`：PySide/shiboken；`qtbase`、`qtsvg`、`qttools`：实际所用Qt模块源码；`fabio`：包含LGPL的CCP4组件源码。
- `_sources/XRelay-1.0.2-source.zip`：可编辑项目源码、资源、构建文件及哈希依赖锁。

## 安装与替换

1. 退出软件及Agent，复制整个解压目录用于实验，保留原件。不在原始数据目录操作。
2. 此包采用PyInstaller onedir，动态库位于 `_internal`。Qt库通常位于 `_internal/PySide6`，FabIO压缩扩展通常位于 `_internal/fabio/ext/mar345_IO.pyd`；以实际文件和报告为准。
3. 使用匹配Windows x64、CPython ABI、Qt/PySide版本及编译依赖的库替换文件。在DLL互相依赖时，按上游构建说明提供完整匹配集合，不能随意混搭版本。更换绑定层时可能还需替换对应Python模块并从源码重建。
4. 库可修改/重建；软件不通过内容哈希或原创许可条款锁死第三方库。原始附件的SHA-256用于验证官方分发文件，修改后的目录自然不再匹配原始清单，不表示许可禁止修改。
5. 检查加载位置：`XRelay-Agent.exe --library-report 新的报告路径.json`。该命令只检查加载库路径、版本与许可资源，不处理实验数据。已有报告不会覆盖。
6. 再运行 `XRelay.exe --self-check 新的输出目录`。回归失败说明该组合需排查；它不取消库许可赋予的权利。

完整应用从源码重建：在Windows x64/Python3.13隔离环境安装 `packaging/*-lock.txt` 的哈希锁依赖，执行 `packaging/build_windows_candidate.ps1 -BuildPython <python.exe>`。若修改库，先按其归档内README/CMake/Meson说明构建，替换构建环境中的相应组件，再按实际修改更新锁和构建预检；不能通过删除验证假称其仍是未修改的官方环境。

## English

The original XRelay licence does not limit third-party rights, including applicable LGPL rights to modify/replace/relink libraries and reverse engineer as necessary to debug those modifications. Preserve the authoritative licence texts and notices.

Matching unmodified library sources are in `_sources`, indexed by version, origin and hash. Editable application source, resources, build scripts and dependency locks are in the XRelay source archive. Follow upstream build instructions in each library archive.

Work on a copy of the full portable folder with all processes stopped. This is a dynamically loaded onedir bundle. Qt DLLs are normally under `_internal/PySide6`; the FabIO CCP4 extension is normally `_internal/fabio/ext/mar345_IO.pyd`. Use compatible Windows x64/CPython ABI/Qt binding and DLL sets. Binding-layer changes may require corresponding Python modules or a complete application rebuild. Original archive checksums are provenance checks, not an integrity lock on modified libraries.

Use `XRelay-Agent.exe --library-report new-report.json` to inspect actual loaded locations and versions. Then run `XRelay.exe --self-check new-output-folder`. Neither command certifies legal compliance; a failed regression does not remove rights granted by the library licence. Rebuilding modified dependencies requires updating the actual build records/locks, not deleting validation checks.
