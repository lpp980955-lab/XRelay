# XRelay 1.0.2

Visual SAXS/WAXS processing: compare detector images, integration regions and multiple curves, then reuse one recipe across a batch.

## Download and start

- Windows x64: download **XRelay-1.0.2-Windows-x64.zip**, extract the entire folder and run **XRelay.exe**. No separate Python or AI account is required for routine desktop use.
- **XRelay-1.0.2-corresponding-source.zip** contains editable application/build materials and accompanying upstream sources/notices; it is not the runnable app. GitHub's automatic snapshots do not replace this complete source-material attachment.
- Check **SHA256SUMS.txt**. This release offers no older binary or patch; future updates are manual.

## Highlights

- Named geometry templates, reference-ring checks, background and MASK workflows.
- Multi-sample/frame previews, radial and azimuthal integration, linked draggable regions.
- Editable curve styles and legends; independent 2D/curve export previews and custom output names.
- Save/reload recipes, batch selected files/frames and inspect progress/failures.
- Optional external AI clients through local MCP stdio, sharing the desktop processing core.

## Verification and licence

Windows11 x64, same-host isolated directories: 204 core tests,38 UI tests,24 frozen GUI checks at each of100/125/150% scaling, plus six lossless NeXus codecs,12 pixel-exact exports, radial/azimuthal integration, fitting and XLSX checks. This is not a second-machine or clean-VM validation. The app is unsigned. See VERIFICATION.md.

Original XRelay material: **LicenseRef-XRelay-Free-Use-1.0**. Free academic/enterprise use and commercial data processing; selling renamed/repackaged software versions requires written permission. Third-party licences, LGPL library replacement/debugging rights and prior lawful grants remain unchanged.

Developer: **Jiutian Zhang**. [English feature gallery](https://lpp980955-lab.github.io/XRelay/index.en.html) · [中文发布页](https://lpp980955-lab.github.io/XRelay/) · [Issues](https://github.com/lpp980955-lab/XRelay/issues)

---

## 中文说明

本次仅发布最新版Windows x64完整包、对应源码及校验文件，不发布旧版或补丁。完整解压后运行 **XRelay.exe**；普通桌面处理无需另装Python或配置AI账号，请保留同目录依赖、许可、源码和手册。

支持几何模板、背景与MASK、径向/方位角积分、多样品多帧曲线对比、拖动选区、曲线样式与独立导出预览、配方批处理及可选MCP接口。先检查代表样品，再处理整批数据；实验几何和模型需结合实际确认。

本机隔离与缩放、源码和Agent检查通过；未完成第二台电脑或干净虚拟机验证，未代码签名。诊断仅在本地记录，用户主动导出，分享前检查敏感信息。

原创部分采用 **XRelay免费使用与再分发许可1.0**，允许企业与商业数据处理，改名包装软件销售须书面授权；第三方原许可及既有合法授权不受追溯影响。开发者：**张久天**。
