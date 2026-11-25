"""Runtime language presentation for existing Qt forms, preserving their data."""
import re
import weakref
from PySide6 import QtCore,QtGui,QtWidgets

# Business values/currentData are deliberately never translated.
EN = {
"对比预览所选帧":"Compare selected frames","显示更多帧…":"Show more frames…",
"正在后台积分…":"Integrating in the background…","正在后台预览背景…":"Preparing background preview…",
"正在后台生成MASK…":"Preparing mask preview…","正在后台拟合…":"Fitting in the background…",
"正在后台拟合选中峰…":"Fitting selected peaks…","正在结束当前拟合，请稍后关闭。":"Finishing the current fit; please close again shortly.",
"无标样":"No calibrant","自动识别中心…":"Find center…","正在识别中心…":"Finding center…",
"无标样：只确定中心。SDD、波长及像素尺寸使用输入值。":"No calibrant: determine center only. Use the supplied distance, wavelength and pixel sizes.",
"无标样：输入已知参数，只确定中心后应用即可积分。":"Enter known geometry, determine the center and apply to integrate.",
"正在后台识别中心，保持输入的SDD、波长和像素尺寸。":"Finding center in the background, keeping the supplied distance, wavelength and pixel sizes.",
"文件":"File","窗口":"Window","语言":"Language","中文":"Chinese","英文":"English",
"帮助":"Help","重启并加载更新":"Restart and load updates","全部预览曲线":"All preview curves","当前曲线":"Current curve","全部曲线":"All curves",
"默认一个工作簿，三张表：曲线数据、参数汇总、处理说明。保留完整原始曲线，导出不重新计算。":"One workbook, three sheets: curves, parameters and processing information. Full raw curves are retained; export does not recalculate.",
"导出当前预览集合；原始曲线完整保留。数据默认一个工作簿：曲线数据、参数汇总、处理说明。":"Export the preview collection with complete original curves. One workbook: curves, parameters and processing information.",
"几何与处理预览":"Processing","几何/标定":"Geometry","背景":"Background","积分":"Integration",
"拟合":"Fitting","配方":"Batch","浏览…":"Browse…","浏览...":"Browse…","包含子文件夹":"Include subfolders",
"全部勾选":"All","勾选所选":"Selected","清空勾选":"Clear","上一张":"Previous","下一张":"Next",
"多图查看":"Image workspace","显示设置":"Display","自动坐标":"Auto coordinates","适应":"Fit view","同步显示":"Link display",
"添加图像窗格":"Add image pane","＋ 添加图像窗格":"+ Add image pane","收起预览 ▸":"Hide controls ▸",
"◂ 文件栏":"◂ Files","文件栏":"Files","收起预览":"Hide controls","自动":"Auto","像素坐标":"Pixels",
"显示理论环":"Show reference rings","应用几何参数":"Apply geometry","全图自动建议中心 / SDD…":"Suggest center / SDD…",
"三点定环":"Three-point ring","三点定中心 / SDD":"Three-point center / SDD","清除预览":"Clear preview",
"自定义标样…":"Custom calibrant…","导入自定义 d-spacing…":"Import d-spacing…","标样":"Calibrant","理论环":"Reference ring",
"波长":"Wavelength","中心 X":"Center X","中心 Y":"Center Y","像素 X":"Pixel X","像素 Y":"Pixel Y",
"图像宽度":"Image width","图像高度":"Image height","最多显示环":"Maximum rings","探测器尺寸与配置":"Detector settings",
"导入配置…":"Import settings…","导出配置…":"Export settings…","处理方式":"Mode","背景文件":"Background file",
"选择…":"Choose…","预览当前图":"Preview image","恢复原图":"Restore raw image","自动扣背景":"Subtract background",
"不扣背景":"No subtraction","数据已扣背景":"Already subtracted","当前样品与计算说明":"Sample and calculation details",
"曝光未知时按同批等曝光处理":"Assume equal batch exposure when unknown","未执行背景预览":"No background preview",
"图像":"Image","范围":"Scope","当前文件夹":"Current folder","左侧选中项":"Checked files","应用范围":"Apply scope",
"执行所选样品批处理…":"Process checked samples…","执行所选样品批处理...":"Process checked samples…",
"恢复已有批处理…":"Resume batch…","完成当前文件后暂停":"Pause after current file","批处理尚未启动。":"Batch has not started.",
"批处理策略":"Batch options","配方版本":"Recipe versions","配方文件与批次方案":"Recipe files","配方与批次说明":"Batch help",
"保存当前处理配方…":"Save recipe…","载入配方/批次方案…":"Load recipe / batch…","保存显式批次方案…":"Save batch plan…",
"预览类型":"Integration type","曲线范围":"Curve scope","图上区域":"Image region","显示积分区域":"Show integration region",
"积分区域":"Integration region","扇区中心":"Sector center","扇区总开角":"Sector width","更多积分与坐标设置":"More integration options",
"径向单位":"Radial unit","像素分割":"Pixel splitting","修正":"Corrections","立体角校正":"Solid-angle correction",
"径向点数":"Radial points","方位点数":"Azimuth points","方位内部bin":"Radial bins per azimuth","横坐标":"X axis","纵坐标":"Y axis",
"坐标范围":"Axis range","设置范围…":"Set range…","预览积分曲线":"Preview curves","曲线显示与导出":"Curve display and export",
"四边框":"Box frame","网格":"Grid","自动范围":"Auto range","宽表 XY（Origin）":"Paired XY columns (Origin)",
"导出数据…":"Export data…","导出数据...":"Export data…","导出图片…":"Export image…","导出图片...":"Export image…",
"＋辅助线":"+ Guide","+辅助线":"+ Guide","删除辅助线":"Delete guide","清空辅助线":"Clear guides",
"单侧扇区":"Single sector","双侧扇区":"Opposite sectors","全环":"Full ring","全部已打开窗格（各自区域）":"All open panes (own regions)",
"一维径向 I–q / I–2θ":"Radial I–q / I–2θ","方位角 I–χ":"Azimuth I–χ","原始坐标":"Original coordinates",
"曲线":"Curve","峰模型":"Peak model","峰数":"Peaks","常数":"Constant","线性":"Linear","无":"None",
"拟合范围":"Fit range","使用全部有限点":"Use all finite points","在图中选择拟合范围":"Select range on plot",
"拟合当前模型":"Fit current model","比较常用模型":"Compare models","按共享设置拟合整批":"Fit batch with shared settings",
"导出拟合结果":"Export results…","导出结果…":"Export results…","参数、误差与技术统计":"Parameters and statistics",
"拟合设置与其他分析":"Fit settings and analysis","自动识峰、选模型并计算 Herman":"Detect peaks, select model and calculate Herman",
"识峰阈值":"Peak threshold","自动峰":"Detected peak","逐峰线型…":"Per-peak settings…","查看峰参数":"Peak details",
"选择峰":"Select peaks","全选峰":"Select all peaks","拟合选中峰":"Fit selected peaks","曲线视图":"Curve view",
"完整方位角（全部峰）":"Full azimuth (all peaks)","当前峰局部":"Current peak","查看折叠峰":"View folded peak",
"计算当前峰 Herman":"Calculate peak Herman","比较第二个峰…":"Compare opposite peak…","导出时汇总 Herman":"Include Herman results",
"结晶度（候选）":"Crystallinity (candidate)","计算结晶度候选":"Calculate crystallinity","导出时汇总结晶度":"Include crystallinity",
"请确认曲线适用性":"Confirm WAXS applicability","WAXS径向曲线 · 有结晶特征峰":"WAXS radial curve with crystalline peaks",
"只有非晶包峰 · 不做结晶度拟合":"Amorphous halo only; skip crystallinity","定义晶峰 / 非晶峰与约束…":"Define crystalline / amorphous peaks…",
"WAXS晶峰 / 非晶峰定义":"WAXS peak definitions","归属":"Assignment","峰型":"Line shape","峰位下限":"Center lower bound",
"峰位上限":"Center upper bound","FWHM下限":"FWHM lower bound","FWHM上限":"FWHM upper bound","请选择":"Choose",
"结晶峰":"Crystalline peak","非晶峰":"Amorphous peak","添加峰":"Add peak","删除所选峰":"Remove selected peak","应用并试拟合":"Apply and fit",
"Ruland 多 q 分析…":"Ruland multi-q analysis…","Ruland 多 q 分析（候选）":"Ruland multi-q analysis (candidate)",
"q 起点 (nm⁻¹)":"q start (nm⁻¹)","q 终点 (nm⁻¹)":"q end (nm⁻¹)","q 步长 (nm⁻¹)":"q step (nm⁻¹)",
"q 采样方式":"q sampling","按步长":"By step","按点数":"By count","环带总宽 (nm⁻¹)":"Ring width (nm⁻¹)","q 点数":"q points",
"峰中心 (°)":"Peak center (°)","峰半范围 (°)":"Peak half-window (°)","候选公式":"Model / formula","B 的宽度定义":"Width definition B",
"FWHM（半高全宽，默认）":"FWHM (default)","HWHM（半高半宽）":"HWHM","请选择峰宽定义":"Choose width definition",
"积分宽度（Pauw 2010）":"Integral breadth (Pauw 2010)","生成环带并拟合当前样品":"Integrate rings and fit sample",
"更新峰中心建议":"Update peak suggestion","多 q 方位角与拟合":"Multi-q curves and fits","Ruland 散点与线性拟合":"Ruland scatter and regression",
"可见峰候选":"Visible peak candidate","批量逐样品建议峰中心":"Suggest peak center per sample","检查 q 范围敏感性…":"Check q-range sensitivity…",
"参与":"Use","q (nm⁻¹)":"q (nm⁻¹)","范围 (nm⁻¹)":"Range (nm⁻¹)","峰宽 (°)":"Width (°)","状态":"Status","峰拟合 R²":"Peak-fit R²",
"导出 Ruland 结果":"Export Ruland results","当前样品 · 全部q":"Current sample · all q","本批全部样品 · 全部q":"Batch · all q",
"当前样品 · 勾选峰":"Current sample · checked peaks","当前样品 · 全部峰":"Current sample · all peaks",
"本窗口全部样品 · 全部峰":"All samples · all peaks","内容":"Content","数据工作簿（.xlsx）":"Data workbook (.xlsx)",
"拟合图片":"Fit images","图片格式":"Image format","选择保存位置…":"Choose destination…","取消":"Cancel","确定":"OK",
"参数":"Parameter","值":"Value","标准误":"Std. error","单位":"Unit","参数/结果":"Parameter / result",
"峰位 (°)":"Center (°)","峰位（°）":"Center (°)","样品":"Sample","峰号":"Peak","峰":"Peak",
"导入一维曲线并拟合…":"Import 1D curves…","打开项目…":"Open project…","保存项目…":"Save project…","导出便携项目包…":"Export portable project…",
"导出2D图…":"Export 2D images…","恢复2D批量导出…":"Resume 2D export…","当前文件后暂停2D批量导出":"Pause 2D export after current file",
"AI Agent 接口…":"AI Agent interface…","操作指南…":"Workflow guide…","处理":"Processing","预览":"Preview","批量":"Batch",
"几何/预览 ▶":"Geometry / preview ▶","尚未载入图像":"No image loaded","未载入":"Not loaded",
"读取：未选择 | 分析：未就绪":"Reader: no file | Analysis: not ready",
"请选择数据文件夹，或导入一维曲线开始处理。":"Choose a data folder or import 1D curves to begin.",
}

EN.update({
    '播放':'Play','暂停':'Pause','跳转':'Go',
    '目标播放速度；实际速度取决于读取和显示耗时':'Target playback rate; actual rate depends on reading and rendering',
    '正在完成帧读取，请稍后关闭窗格。':'Finishing a frame read; close the pane shortly.',
    '正在完成帧读取，请稍后关闭程序。':'Finishing a frame read; close the application shortly.',
    '批次帧…':'Batch frames…','批次帧':'Batch frames',
    '选择当前数据集进入批处理的帧':'Select frames of this dataset for batch processing',
    '预览帧；批次帧可单独选择':'Preview frame; choose batch frames separately',
    '全部帧':'All frames','区间与步长':'Range and step','勾选帧':'Checked frames',
    '选择方式':'Selection mode','起始帧':'First frame','结束帧':'Last frame','步长':'Step',
    '请至少选择一帧，并检查区间顺序。':'Select at least one frame and check the range order.',
"Language / 语言":"Language","◂ 文件栏":"< Files","▸ 文件栏":"> Files","几何/预览 ▶":"Geometry / preview >",
"联用遮挡（仅排除，不补全）":"Combined mask (exclude only, no filling)",
"Debye–Bueche 候选分析":"Debye–Bueche analysis (candidate)","Guinier / Porod 分析":"Guinier / Porod analysis",
"Porod 幂律":"Porod power law","单组分":"Single component","双组分":"Two components","原始 I–q":"Original I–q",
"导出工作簿…":"Export workbook…","导出拟合图…":"Export fit image…","方法":"Method","显示":"Display","线性化":"Linearized",
"计算全部":"Calculate all","计算当前":"Calculate current","计算整批":"Calculate batch",
"明确选择模型和 q 范围后计算；相关长度不直接等于孔径。":"Choose a model and q range explicitly. Correlation length is not directly a pore diameter.",
"范围使用所选 q 单位；使用已处理的强度，不自动扣除背景。":"Range uses the selected q unit and processed intensities; background is not subtracted automatically.",
"每个样品保留完整原始曲线，附上已拟合峰的曲线。\n工作簿只含曲线数据、参数汇总、处理说明。":"Keep each full original curve and its fitted peaks.\nThree sheets: curves, parameters and processing information.",
"工作簿只含曲线数据、参数汇总、处理说明。\n保留每个q的完整原始及拟合曲线、Ruland散点和回归结果。":"Three sheets: curves, parameters and processing information.\nIncludes full raw/fitted curves per q and Ruland scatter/regression results.",
"先人工确定各峰位置和归属。峰位范围必填，宽度范围可留空；单位与当前曲线横坐标相同。提交后代表曲线试拟合，整批沿用相同约束。":"Define each peak position and assignment first. Center bounds are required; width bounds are optional, in the curve x unit. Test on a representative curve, then reuse constraints for the batch.",
"人工处理可以完全离线。需要AI时，将以下MCP配置加入支持MCP的客户端；AI只能通过本接口读取所选目录、在指定目录输出结果。客户端中的模型及联网行为由客户端管理。未配置时不会启动Agent服务。":"Manual processing works offline. Add the generated MCP configuration to a compatible AI client. These tools read only the selected directory and write only to the selected output directory. The client controls its own model and network access. No Agent service starts until configured.",
"上下限":"Bounds","上限":"Upper bound","下限":"Lower bound","最小":"Minimum","最大":"Maximum","模型":"Model",
"色阶":"Levels","配色":"Colormap","反转配色":"Reverse colormap","自定义…":"Custom…","自动百分位":"Auto percentiles",
"原始单位":"Original units","像素 x/y":"Pixel x/y","手动强度":"Manual intensity","上半区被挡":"Upper half blocked",
"下半区被挡":"Lower half blocked","左侧被挡":"Left side blocked","右侧被挡":"Right side blocked","两侧对称扇区":"Opposite symmetric sectors",
"no（像素中心）":"no (pixel center)","bbox（候选默认）":"bbox (default)","full（完整分割）":"full (pixel splitting)",
"自动：拟合模型完整域":"Auto: full fitted-model domain","完整0–90°域":"Full 0–90° domain","选定峰域":"Selected peak domain",
"完整360°方位角 I–φ":"Full 360° azimuth I–φ","GaussianAmp（峰高参数）":"GaussianAmp (height)","Gaussian（面积参数）":"Gaussian (area)",
"＋圆形":"+ Circle","＋矩形":"+ Rectangle","＋联用遮挡":"+ Combined mask","＋逐点多边形":"+ Polygon",
"MASK区":"MASK region","积分区":"Integration region","径向上限":"Radial upper bound","径向下限":"Radial lower bound",
"手动排除区域":"Manual exclusions","显示MASK编辑轮廓":"Show mask outlines","隐藏MASK":"Hide masks","未执行MASK预览":"No mask preview",
"导入外部MASK…":"Import external mask…","移除外部MASK":"Remove external mask","外部MASK：未选择":"External mask: none",
"外部坏点MASK（非零=排除）":"External mask (nonzero excludes)","原始TVX负哨兵像素":"Raw TVX negative sentinel pixels",
"NaN / Inf像素":"NaN / Inf pixels","达到探测器计数上限的像素":"Pixels at detector count limit","更多MASK来源与操作":"More mask options",
"探测器MASK元数据：未知":"Detector mask metadata: unknown","自定义区域：0":"Custom regions: 0","撤销区域":"Undo region","清空区域":"Clear regions",
"自动建议beamstop/支撑杆":"Suggest beamstop / support mask","自定义矩形":"Custom rectangle","自定义多边形":"Custom polygon",
"候选几何（未标定）":"Candidate geometry (not calibrated)","当前仅预览零倾角理论环；不会修改原始图像。":"Untilited reference-ring preview; raw image is unchanged.",
"理论环预览已关闭":"Reference-ring preview off","多环残差（建议值）":"Multi-ring residuals (suggestion)","环":"Ring",
"尚未扫描背景候选":"Background candidates not scanned","当前样品：未载入":"Current sample: not loaded",
"当前图像：未载入":"Current image: not loaded","当前图像":"Current image","当前窗格":"Current pane",
"尚未保存或载入配方。":"No recipe saved or loaded.","创建新版本…":"Create revision…","结构QC":"Structural QC","科学QC":"Scientific QC",
"结构安全检查始终开启":"Structural checks always enabled","科学QC（尚未冻结规则）":"Scientific QC (criteria pending)",
"锁定（逐帧完全重放）":"Locked (replay each frame)","自适应建议（只记录，不改参数）":"Adaptive suggestions (record only)",
"尚未建立基础批次方案；请先保存或载入批次方案。":"Save or load a base batch plan first.","当前图所在序列":"Current image sequence",
"当前文件夹及子文件夹":"Current folder and subfolders","从当前图起":"From current image","取消取点":"Cancel point picking",
"加载左侧勾选样品的积分曲线":"Load integration curves for checked samples","停止加载":"Stop loading",
"打开当前已生成曲线":"Open current available curve","选择已生成曲线到同一窗口…":"Select curves for one workspace…",
"全部已生成曲线到同一窗口":"All curves in one workspace","拟合窗口不会占用二维查看区域；可以同时打开多个窗口。":"Fit workspaces are separate from the image viewer; multiple workspaces may remain open.",
"请先在左侧勾选样品，或在“积分”页生成当前窗格曲线。":"Check samples on the left or generate a curve in Integration first.",
"请先在“积分”页生成曲线；本卡片不平滑、不删异常点。":"Generate an integration curve first. Fitting does not smooth or delete outliers.",
"请先确认是有结晶特征峰的WAXS径向曲线。":"Confirm a WAXS radial curve with crystalline features first.",
"选择方位角单峰并拟合后计算。":"Fit an azimuthal peak to calculate Herman.","模型比较（可选）":"Model comparison (optional)",
"q 为默认候选值":"q values are initial suggestions","先确认 q 范围、峰中心、模型及 B 的定义；不同文献的峰宽与公式系数可能不同。":"Check q range, peak center, model and width B. Width conventions and coefficients differ between sources.",
"导出 Ruland 图…":"Export Ruland image…","批量导出 Ruland 图…":"Export batch Ruland images…",
"FWHM/HWHM候选使用 Liu 2025 SI，原文对应关系待核实；积分宽度使用 Pauw 2010，需完整180°及非负扣底强度。":"FWHM/HWHM use Liu 2025 SI; the source width terminology needs verification. Pauw integral breadth requires complete 180° coverage and nonnegative background-subtracted intensity.",
"FWHM/HWHM使用Liu候选公式；积分宽度使用Pauw公式与相应系数。请核查扣底、覆盖范围和文献定义。":"FWHM/HWHM use Liu formulas; integral breadth uses Pauw coefficients. Check baseline, angular coverage and source definitions.",
"Gaussian：B² 对 1/q²；Bψ=√截距；Lf=1/√斜率（Liu 2025 SI式6） B以弧度计算；同一q序列统一峰型和峰宽定义。":"Gaussian: B² vs 1/q²; Bψ=√intercept; Lf=1/√slope (Liu 2025 SI Eq.6). B is in radians. Use one line shape and width convention across q.",
"Lorentzian：B 对 1/q；Bψ=截距；Lf=2π/斜率。 B以弧度计算；同一q序列统一峰型和峰宽定义。":"Lorentzian: B vs 1/q; Bψ=intercept; Lf=2π/slope. B is in radians. Use one line shape and width convention across q.",
"未执行积分；预览与后续批处理共用同一核心。":"No integration yet. Preview and batch processing share the same core.",
"点击图例可突出曲线；再次点击可恢复。":"Click a legend item to highlight its curve; click again to restore.",
"物理绝对：q / s / 2θ / φ":"Physical coordinates: q / s / 2θ / φ","像素；相对区域仅用于预览":"Pixels; relative regions are for preview only",
"排序只提供统计证据，不会自动替换您选择的模型，也不证明物理模型正确。":"Ranking provides statistical evidence only; it does not replace your selected model or prove physical validity.",
"技术拟合 ≠ 科学有效。模型、背景和范围必须随结果保存；Herman、Ruland与结晶度在后续独立卡片中处理。":"A converged fit does not prove scientific validity. Save the model, background and range with results; assess Herman, Ruland and crystallinity separately.",
"MASK只标记积分时应排除的像素，不改强度，也不填补遮挡区域。":"MASK marks excluded pixels; it does not change intensities or fill blocked regions.",
"曝光已知：sample/t_sample − air/t_air（计数率）\n曝光未知且按等曝光处理：sample − air（计数）":"Known exposures: sample/t_sample − air/t_air (count rate)\nUnknown, assumed equal: sample − air (counts)",
"曝光时间未知时，默认按本批样品与背景曝光相同直接相减，输出计数；双方曝光时间已知时按时间归一化，输出计数率。预览不会修改原始文件。":"If exposure is unknown, assume equal batch exposure and subtract in counts. Known exposures are normalized to count rate. Raw files remain unchanged.",
"处理配方保存几何、背景、MASK区域和积分参数。选择范围只会先生成左侧可检查的候选；文件夹、递归和序列范围会排除已配置输入及明显的背景/标样候选，执行前仍可人工增删。":"Recipes store geometry, background, mask regions and integration settings. Applying scope updates the checked-file candidates for review; configured inputs and obvious air/calibrant candidates are excluded before processing.",
"2–8峰使用同一线型自动给出初值；每个峰的物理归属仍需人工确认":"2–8 peaks use consistent line shapes with automatic seeds. Physical assignments still require review.",
"已知中心、SDD、波长和像素尺寸时可直接应用，无需提供标样。":"Known center, SDD, wavelength and pixel dimensions may be applied directly; no calibrant is required.",
"选择样品/峰范围，导出精简工作簿或拟合图片。":"Choose samples/peaks and export a compact workbook or fit images.",
"先把范围转换为左侧可见选择；检查无误后再执行，不会立即启动批处理":"Apply scope to the visible file selection, then review and execute; this does not start processing.",
"添加内嵌图像窗格":"Add image pane","关闭这个内嵌图像窗格":"Close image pane","收起右侧预览栏":"Hide processing panel",
"显示/收起右侧几何与处理预览栏":"Show / hide processing panel","收起/显示左侧文件浏览栏":"Show / hide file browser",
"鼠标悬停读数；自动模式在已应用几何时显示q，否则显示像素坐标":"Hover for coordinates; auto uses q with applied geometry, otherwise pixels.",
"数据文件夹":"Data folder","输出文件夹":"Output folder","生成连接配置":"Generate configuration","复制配置":"Copy configuration",
"保存配置…":"Save configuration…","AI Agent 接口":"AI Agent interface","选择文件夹":"Choose folder",
"客户端":"Client","本地测试超时":"Local test timeout","合并预览…":"Preview merge…",
"配置：尚未生成":"Configuration: not generated","本地服务：尚未测试":"Local service: not tested",
"客户端连接：待在客户端会话中验证":"Client connection: verify in the client session",
"测试本地服务":"Test local service","取消测试":"Cancel test","连接配置":"Configuration",
"接入与验证步骤":"Connection and verification","配置合并预览":"Configuration merge preview",
"预览合并":"Preview merge","复制合并结果":"Copy merged result","粘贴已有配置":"Paste existing configuration",
"通用 MCP JSON":"Generic MCP JSON","Grok Build（官方 CLI）":"Grok Build (official CLI)",
"DeepSeek / Kimi / 豆包等（需明确客户端）":"DeepSeek / Kimi / Doubao (client unspecified)",
"人工处理完全离线；此处生成本地 MCP 连接配置。模型、账号及联网行为由所用客户端管理。":"Manual processing works offline. Generate local MCP configuration here. The client controls models, accounts and network use.",
"配置：已生成；未改写任何客户端设置":"Configuration generated; no client settings changed",
"配置：已保存；请手动合并到客户端，保留已有设置":"Configuration saved; merge it manually and preserve existing settings",
"正在启动本地 Agent…":"Starting local Agent…","正在协商 MCP 协议…":"Negotiating MCP protocol…",
"正在发现工具…":"Discovering tools…","正在结束只读测试进程…":"Finishing read-only test process…",
"本地服务测试通过；客户端实际连接仍待验证。":"Local service test passed; client connection remains unverified.",
"本地服务测试已取消":"Local service test cancelled",
"本地服务测试超时；请检查程序路径、目录权限或增加超时。":"Local service test timed out; check executable and folder permissions or increase the timeout.",
"粘贴已有配置（可先去除密钥）；只在内存预览，不读取或改写客户端文件。修改前请自行备份原配置。":"Paste existing settings (remove keys first). Merge preview stays in memory; client files are not read or changed. Back up your settings before editing.",
"合并预览已生成；同名冲突会阻止覆盖。":"Merge preview generated; conflicting service entries are not overwritten.",
"操作指南":"Workflow guide","简体中文":"简体中文","长表":"Long table",
})


EN.update({"方位角":"Azimuth angle","强度":"Intensity","导出结果":"Export results","图片":"Images",
           "< 文件栏":"< Files","文件栏 >":"Files >","显示范围":"Display range",
           "原数组":"Original array","未修改":"unchanged",
           "选择包含二维散射图的文件夹":"Choose a folder containing 2D scattering images"})


EN.update({
    '导出处理后二维数据':'Export processed 2D data',
    '导出处理后二维数据…':'Export processed 2D data…',
    '当前样品':'Current sample','左侧勾选样品':'Checked samples',
    '扣背景（使用背景页设置）':'Subtract background (Background settings)',
    '应用当前MASK及手动区域':'Apply current MASK and manual regions',
    '附带当前已应用几何（文件内几何优先）':'Include applied geometry (embedded geometry takes precedence)',
    '完成当前文件后停止':'Stop after current file',
    '按原文件容器导出数值数据，不是预览图片。\n'
    'TIFF保留64位浮点；d*TREK IMG为32位浮点，并记录存储误差。\n'
    'MASK像素写为NaN，有限负值保留。几何未知时明确记录未知。\n'
    '本软件重开自动恢复；其他软件需支持浮点、NaN及对应几何头。\n'
    '整批仅附一份处理说明，无逐文件JSON。':
    'Export numerical data in the source container.\n'
    'TIFF: float64. d*TREK IMG: float32 with recorded storage error.\n'
    'MASK pixels become NaN; finite negative values are preserved. Unknown geometry stays unknown.\n'
    'This workbench restores metadata. Other software must support floating pixels, NaN and geometry headers.\n'
    'One shared processing note; no per-file JSON.'})


EN.update({
    '原格式优先，必要时无损TIFF（记录转换）':'Source preferred; lossless TIFF fallback',
    '严格原格式，不能可靠保存时报错':'Strict source format; report unsupported writing',
    '统一无损TIFF':'Lossless TIFF for all files','统一NeXus（含参数与MASK）':'NeXus for all files (parameters and MASK)',
    '文件格式':'File format','附带当前几何（保留人工修正）':'Include applied geometry (keep manual corrections)',
    '背景数据集':'Background dataset','背景帧':'Background frame','单一数据集':'Single dataset',
    '预览帧；勾选批处理默认遍历全部帧':'Preview frame; checked-file batches process all frames',
    '预览数据集；批量使用已确认的主信号范围':'Preview dataset; batches use the confirmed detector signal',
    '导出可继续计算的二维数值数据。\n'
    'TIFF保留64位浮点；d*TREK IMG为32位浮点，并记录存储误差。\n'
    'GFRM/压缩CBF等不能可靠保存浮点MASK时，明确转换为TIFF。\n'
    '当前导出预览帧；勾选批量时使用批次帧选择（默认全部帧）。\n'
    'MASK像素写为NaN，有限负值保留。几何未知时明确记录未知。\n'
    '本软件重开自动恢复；其他软件需支持浮点、NaN及对应几何头。\n'
    '整批仅附一份处理说明，无逐文件JSON。':
    'Export numerical 2D data for further analysis.\n'
    'TIFF: float64. d*TREK IMG: float32; storage error is recorded.\n'
    'GFRM / compressed CBF convert explicitly to lossless TIFF when floating MASK data cannot be preserved.\n'
    'Current exports the preview frame; batches process all frames of confirmed datasets.\n'
    'MASK pixels are NaN; finite negatives are retained. Unknown geometry stays unknown.\n'
    'This workbench restores metadata. Other applications need floating-point, NaN and header support.\n'
    'One shared processing note; no per-file JSON.'})


# Qt/pyqtgraph creates these labels in English before the app language pass.
# Canonical Chinese text lets a later language switch restore either language.
NATIVE_ZH={
    'View All':'恢复完整视图','X axis':'横坐标轴','Y axis':'纵坐标轴',
    'Mouse Mode':'鼠标模式','3 button':'三键平移模式','1 button':'单键框选缩放',
    'Plot Options':'绘图选项','Transforms':'坐标与数据变换','Downsample':'显示降采样',
    'Average':'曲线平均','Alpha':'透明度','Grid':'网格','Points':'数据点',
    'Log X':'横轴对数显示','Log Y':'纵轴对数显示','Power Spectrum (FFT)':'功率谱（FFT）',
    'Subtract Mean':'减去均值','dy/dx':'一阶导数 dY/dX',"Y vs. Y'":'相图：Y与导数',
    'Clip to View':'仅绘制可见范围','Max Traces:':'最多显示曲线数：',
    'Forget hidden traces':'释放已隐藏曲线','Peak':'峰值保留','Mean':'区间均值','Subsample':'间隔抽点',
    'Auto':'自动','Show X Grid':'显示横向网格','Show Y Grid':'显示纵向网格','Opacity':'不透明度',
    'Export...':'导出…','Close':'关闭','Cancel':'取消','Apply':'应用','OK':'确定',
    'Mouse Enabled':'启用鼠标操作','Manual':'手动','Auto pan only':'仅自动平移',
    'Visible Data Only':'仅可见数据','Invert Axis':'反向坐标轴','Link Axis':'关联坐标轴',
    'Display averages of the curves displayed in this plot. The parameter list allows you to choose parameters to average over (if any are available).':'额外显示当前曲线的平均曲线；参数列表用于选择分组依据。',
    'Plot only the portion of each curve that is visible. This assumes X values are uniformly spaced.':'仅绘制可见曲线部分，假定横坐标等间距。',
    'Downsample by drawing a saw wave that follows the min and max of the original data. This method produces the best visual representation of the data but is slower.':'按区间最小值和最大值绘制，保留峰形轮廓；比间隔抽点更耗时。',
    'Downsample by taking the mean of N samples.':'每N点取均值用于显示。',
    'Downsample by taking the first of N samples. This method is fastest and least accurate.':'每N点取首点用于显示，速度快但可能漏掉窄峰。',
    'Automatically downsample data based on the visible range. This assumes X values are uniformly spaced.':'根据可见范围自动减少屏幕绘点，假定横坐标等间距。',
    'Downsample data before plotting. (plot every Nth sample)':'绘图前按指定倍数减少显示点数，不改变积分分箱数。',
    'If multiple curves are displayed in this plot, check this box to limit the number of traces that are displayed.':'限制当前图中同时显示的曲线数量。',
    'If MaxTraces is checked, remove curves from memory after they are hidden (saves memory, but traces can not be un-hidden).':'限制曲线数量时释放被隐藏曲线；节约内存，但不能直接恢复显示。',
    'If multiple curves are displayed in this plot, check "Max Traces" and set this value to limit the number of traces that are displayed.':'勾选最多显示曲线数后，通过此值限制显示数量。',
}
EN.update({zh:en for en,zh in NATIVE_ZH.items()})
EN.update({
    '绘图选项…':'Plot Options','绘图选项':'Plot Options','选择显示曲线…':'Select curves…',
    '左侧勾选样品／帧':'Checked samples / frames','选择预览曲线':'Select preview curves',
    '搜索样品名、帧号或数据集':'Search sample, frame or dataset','全选':'Select all','清空':'Clear all',
    '只控制右侧曲线显示与当前曲线导出，不改变左侧批处理勾选。':'Controls curve visibility and current export only; left batch selection is unchanged.',
    '曲线与文字':'Curves and text','坐标轴与刻度':'Axes and ticks','图例':'Legend','各条曲线':'Individual curves',
    '字体':'Font','默认曲线线宽':'Default line width','默认线型':'Default line style',
    '实线':'Solid','虚线':'Dashed','点线':'Dotted','点划线':'Dash-dot',
    '图标题':'Plot title','横轴标题':'X axis title','纵轴标题':'Y axis title',
    '标题字号 / pt':'Title size / pt','标题加粗':'Bold titles','刻度字号 / pt':'Tick size / pt','刻度文字加粗':'Bold tick labels',
    '坐标轴线宽':'Axis width','刻度线宽':'Tick width','主刻度长度':'Major tick length','次刻度长度':'Minor tick length',
    '刻度向内':'Inward ticks','显示次刻度':'Minor ticks','每个主刻度间的次刻度数':'Minor ticks per interval',
    '横轴主刻度间隔（0为自动）':'X major spacing (0: auto)','纵轴主刻度间隔（0为自动）':'Y major spacing (0: auto)',
    '四边框时显示上／右刻度':'Top / right ticks in box frame','轴线与文字颜色':'Axes and text color',
    '图例显示':'Legend visibility','自动（超过8条收起）':'Auto (hide above 8 curves)','显示':'Show','隐藏':'Hide',
    '图例位置':'Legend position','图外下方（避免遮挡）':'Outside below (no overlap)',
    '右上':'Upper right','左上':'Upper left','右下':'Lower right','左下':'Lower left',
    '图例字号 / pt':'Legend size / pt','图例加粗':'Bold legend','图例列数':'Legend columns','图例边框':'Legend frame',
    '默认缩短文件名，省略数据集路径':'Short filenames without dataset paths',
    '图内图例可拖动；逐条图例文字、颜色和线型在“各条曲线”中修改。':'Drag an inside legend; edit labels, colors and lines in Individual curves.',
    '完整曲线身份':'Full curve identity','图例文字':'Legend label','颜色（双击选择）':'Color (double-click)','线宽':'Line width','线型':'Line style',
    '保存为默认样式':'Save as default','恢复初始样式':'Reset style',
    '仅修改图形外观；积分参数、原始数据及数据表的来源名称保持不变。':'Appearance only; integration, raw data and source names are unchanged.',
    '留空使用当前坐标名称':'Leave blank for current axis label',
    '设置坐标范围…':'Set axis ranges…','辅助线':'Guides',
    '曲线菜单（也可在曲线区右键打开）':'Curve menu (also available by right-click)',
    '右键打开曲线菜单：选择曲线、绘图选项、坐标范围、导出与辅助线。':'Right-click for curve selection, Plot Options, axis ranges, export and guides.',
})


EN.update({
    '导出积分曲线':'Export integration curves','曲线数据':'Curve data','曲线图片':'Curve image',
    '自动适配窗口布局':'Adapt window layout automatically','重新适配当前屏幕':'Fit layout to current screen',
    '添加内嵌图像窗格':'Add embedded image pane',
    '手动拖动分隔线后暂停自动分栏；重新勾选可恢复。':'Dragging a divider pauses automatic sizing; check again to resume.',
    '下轴刻度线':'Bottom axis ticks','左轴刻度线':'Left axis ticks','上轴刻度线':'Top axis ticks','右轴刻度线':'Right axis ticks',
    '四个方向分别控制刻度线；不隐藏坐标文字。开启上／右刻度会显示对应轴线。':'Control ticks on each side; axis labels stay visible. Top / right ticks also show their axis lines.',
    '导出内容':'Export content','文件格式':'File format','宽度':'Width','高度':'Height',
    '选择保存位置并导出…':'Choose location and export…','适合窗口':'Fit to window','1:1 像素':'1:1 pixels',
    '正在更新高清预览…':'Updating high-resolution preview…',
    '仅预览前4条曲线的前15行；导出包含全部当前显示曲线。':'Preview of the first 15 rows of 4 curves; export includes all visible curves.',
    '正在后台导出…':'Exporting in the background…','导出已取消':'Export cancelled',
    '正在导出，请完成后再关闭。':'Export in progress; close after completion.',
    '当前没有可导出的预览曲线':'No preview curves available to export',
    '正在结束曲线导出，请稍后关闭。':'Finishing curve export; please close again shortly.',
    '不支持的曲线导出格式':'Unsupported curve export format',
})

def english(text):
    if text in EN:
        return EN[text]
    match=re.fullmatch(r'导出当前显示的 (\d+) 条曲线。图片沿用绘图选项。XLSX沿用原积分／显示坐标规则；CSV/TSV仅导出显示坐标。',text)
    if match:return f'Export {match[1]} visible curves. Images use Plot Options. XLSX retains native / displayed coordinates; CSV/TSV use displayed coordinates.'
    match=re.fullmatch(r'高清排版预览 · (.+) mm · 输出 (.+) px（(.+) DPI）',text)
    if match:return f'High-resolution layout preview · {match[1]} mm · Output {match[2]} px ({match[3]} DPI)'
    if text.startswith('已导出：'):return 'Exported: '+text[len('已导出：'):]
    match=re.fullmatch(r'显示曲线（(\d+)/(\d+)）…',text)
    if match:return f'Select curves ({match[1]}/{match[2]})…'
    match=re.fullmatch(r'(\d+) / (\d+) 条',text)
    if match:return f'{match[1]} / {match[2]} curves'
    match=re.fullmatch(r"共 (\d+) 帧。编号从 1 开始，按数据集及帧索引排序。",text)
    if match:
        return f"{match[1]} frames. One-based numbering in dataset and frame order."
    match=re.fullmatch(r"(\d+) 个候选文件",text)
    if match:
        return f"{match[1]} candidate files"
    match=re.fullmatch(r"批量处理左侧勾选样品（(\d+)）",text)
    if match:
        return f"Process checked samples ({match[1]})"
    match=re.fullmatch(r"左侧已选择 (\d+) 个批次样品",text)
    if match:
        return f"{match[1]} batch samples checked"
    if not re.search(r"[\u4e00-\u9fff]",text):
        return text
    # Never translate a filename or filesystem path, including sample identifiers.
    if re.search(r"(?:[A-Za-z]:[\\/]|\.(?:tiff?|img|csv|xlsx|json|h5)\b)",text,re.I) and not text.startswith(("图像 ","当前样品：")):
        return text
    value=text
    for source in sorted(EN,key=len,reverse=True):
        if len(source)>1:
            value=value.replace(source,EN[source])
    return value


class LanguageManager(QtCore.QObject):
    def __init__(self,app):
        super().__init__(app)
        self.app=weakref.proxy(app)
        self.language="zh"
        self._texts=weakref.WeakKeyDictionary()
        self._pending=False
        self._applying=False
        app.installEventFilter(self)

    def set_language(self,language):
        if language not in ("zh","en"):
            raise ValueError("language must be zh or en")
        self.language=language
        self.apply_all()

    def _text(self,obj,key,value,setter):
        state=self._texts.setdefault(obj,{})
        old=state.get(key)
        source=old[0] if old and value == old[1] else value
        source=NATIVE_ZH.get(source,source)
        shown=english(source) if self.language == "en" else source
        state[key]=(source,shown)
        if shown != value:
            setter(shown)

    def apply_all(self):
        self._pending=False
        self._applying=True
        try:
            for window in self.app.topLevelWidgets():
                for w in [window,*window.findChildren(QtWidgets.QWidget),*window.findChildren(QtGui.QAction)]:
                    with QtCore.QSignalBlocker(w):
                        if isinstance(w,(QtWidgets.QLabel,QtWidgets.QAbstractButton,QtGui.QAction)):
                            self._text(w,"text",w.text(),w.setText)
                        self._text(w,"tooltip",w.toolTip(),w.setToolTip)
                        if isinstance(w,QtWidgets.QLineEdit):
                            self._text(w,"placeholder",w.placeholderText(),w.setPlaceholderText)
                        if isinstance(w,QtWidgets.QComboBox):
                            for i in range(w.count()):
                                self._text(w,('item',i),w.itemText(i),lambda t,i=i:w.setItemText(i,t))
                        if isinstance(w,QtWidgets.QTabWidget):
                            for i in range(w.count()):
                                self._text(w,('tab',i),w.tabText(i),lambda t,i=i:w.setTabText(i,t))
                        if isinstance(w,QtWidgets.QTableWidget):
                            for i in range(w.columnCount()):
                                item=w.horizontalHeaderItem(i)
                                if item:
                                    self._text(w,('header',i),item.text(),item.setText)
                        if hasattr(w,'retranslate_curve_ui'):
                            w.retranslate_curve_ui(self.language)
                        if hasattr(w,'retranslate_agent_ui'):
                            w.retranslate_agent_ui(self.language)
                        if isinstance(w,QtWidgets.QGroupBox):
                            self._text(w,"title",w.title(),w.setTitle)
                        if isinstance(w,QtWidgets.QMenu):
                            self._text(w,'menuTitle',w.title(),w.setTitle)
                        if isinstance(w,QtWidgets.QStatusBar):
                            self._text(w,"message",w.currentMessage(),w.showMessage)
                        if isinstance(w,QtWidgets.QWidget) and w.isWindow():
                            self._text(w,"windowTitle",w.windowTitle(),w.setWindowTitle)
                        # Plot axes are graphics items, outside QWidget traversal.
                        if hasattr(w,"getPlotItem"):
                            plot=w.getPlotItem()
                            for spec in plot.axes.values():
                                axis=spec['item']
                                self._text(axis,'axisLabel',axis.labelText,
                                    lambda t,a=axis:a.setLabel(text=t,units=a.labelUnits,unitPrefix=a.labelUnitPrefix,**a.labelStyle))
        finally:
            self._applying=False

    def eventFilter(self,obj,event):
        events=(QtCore.QEvent.Type.Show,QtCore.QEvent.Type.ActionAdded)
        if self.language=='en':events=(*events,QtCore.QEvent.Type.LayoutRequest)
        if not self._applying and not self._pending and event.type() in events:
            self._pending=True
            QtCore.QTimer.singleShot(0,self.apply_all)
        return False


def language_manager():
    app=QtWidgets.QApplication.instance()
    if not hasattr(app,"_workbench_language"):
        app._workbench_language=LanguageManager(app)
    return app._workbench_language
