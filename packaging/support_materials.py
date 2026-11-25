"""Common offline diagnostics entry shipped with every portable package."""
def write_support_files(release):
    (release/'发布前兼容性自检.cmd').write_text('''@echo off
powershell.exe -NoProfile -File "%~dp0compatibility-check.ps1"
pause
''',encoding='ascii')
    (release/'compatibility-check.ps1').write_text('''$ErrorActionPreference = 'Stop'
$run = Join-Path $env:LOCALAPPDATA ('XRelay\\self-checks\\check-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
if (Test-Path -LiteralPath $run) { throw 'Choose a new result directory.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $run) -Force | Out-Null
$exe = Join-Path $PSScriptRoot 'XRelay.exe'
$oldScale = $env:QT_SCALE_FACTOR
try {
    $env:QT_SCALE_FACTOR = '1'
    $process = Start-Process -FilePath $exe -ArgumentList @('--self-check', ('"' + $run + '"')) -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(240000)) { throw 'Self-check exceeded four minutes; retain logs and report the issue.' }
} finally { $env:QT_SCALE_FACTOR = $oldScale }
$result = Join-Path $run 'result.json'
if (-not (Test-Path -LiteralPath $result)) { throw ('No result report. Check logs at: ' + $run) }
Get-Content -LiteralPath $result -Encoding UTF8
Compress-Archive -LiteralPath $run -DestinationPath ($run + '.zip')
Write-Output ('Review the report before sharing: ' + $run + '.zip')
''',encoding='utf-8-sig')
    (release/'导出诊断报告.cmd').write_text('''@echo off
"%~dp0XRelay-Agent.exe" --export-diagnostics
pause
''',encoding='ascii')
    (release/'诊断与监控说明.txt').write_text('''XRelay 自动监控与诊断

正常启动 XRelay.exe 即自动启用，不用额外开启监控程序。
日志记录版本、Windows/CPU/内存、后台预览任务耗时、积分阶段、界面持续无响应和异常代码位置。
显示诊断记录屏幕逻辑尺寸、可用区域、DPI、缩放比例、窗口与分栏尺寸及滚动区域状态。
不记录键入内容、鼠标坐标、实验图像、原始数据或截图，不自动上传。
日志位于 %LOCALAPPDATA%\\XRelay\\logs，使用分卷轮换并清理旧会话，避免长期无限增长。

出现问题后：
1. 在软件中选择“帮助 → 导出诊断报告…”，自行命名保存 ZIP。
2. 如果软件已经卡住或无法启动，双击本文件夹的“导出诊断报告.cmd”。
   终端会显示报告 ZIP 的完整路径，默认在 %LOCALAPPDATA%\\XRelay\\diagnostic-reports。
3. 把 ZIP 与操作步骤发回维护者；请说明点击了哪个功能、等了多久。
   软件崩溃后仍可运行导出命令。没有先前日志时只能记录当前环境。

长时间休眠、系统繁忙也可能产生无响应记录；日志用于定位问题，不能把每条记录都当成软件故障。
不要求管理员权限或安装 Python。监控不改变科学参数或计算结果。
''',encoding='utf-8-sig')
    (release/'显示适配说明.txt').write_text('''XRelay 显示适配

启动时根据当前屏幕的可用区域、Qt逻辑尺寸与字体测量分配窗口空间，不复制开发电脑的窗口宽度。
换显示器、改变缩放或窗口大小时重新适配；参数区较窄时标签与输入框自动分行、按钮组自动堆叠，参数多时上下滚动。
手动拖动主分栏或积分区上下分隔线后，自动分栏暂停，保留手动选择；参数换行和滚动继续有效。
需要恢复时选择“窗口 → 重新适配当前屏幕”；“自动适配窗口布局”可以开关自动分栏。
不同显示配置下的显示适配不改变原始图像、几何、积分或拟合参数，也不降低导出分辨率。

如有内容看不全或界面异常，使用“帮助 → 导出诊断报告…”。报告包含display-layout.json，记录显示环境和布局状态；不自动截图或上传。
无需另开监控程序或修改系统缩放设置。不额外安装显示插件。
''',encoding='utf-8-sig')
