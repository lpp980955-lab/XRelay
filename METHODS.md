# XRelay 处理方法与参考来源

版本1.0.2；说明依据当前实现，不构成对所有实验数据的科学验收。

| 环节 | 实际实现与条件 |
|---|---|
| 几何 | 核对波长、SDD、束心、像素尺寸及旋转约定；自动/理论环预览候选需要人工确认，部分候选基于零倾角近似 |
| 径向积分 | 调用 pyFAI `integrate1d_ng`，传入实际MASK、区间、点数、像素分割、归一化与所选修正；记录实际参数 |
| 方位角积分 | 使用明确的径向环带与角度分箱；0°与360°为同一方向；探测器缺失/遮挡区域不补造测量信号 |
| 背景 | 按所选帧、曝光/缩放策略处理；曝光未知时不得将等曝光假设当成实测事实 |
| 图像读取 | FabIO及专用适配器读取适配探测器格式，h5py读取HDF5/NeXus；同后缀不保证所有厂商结构均兼容 |
| 峰拟合 | SciPy `least_squares`求解所选模型；核对拟合域、峰数、约束、背景和残差，求解收敛不证明物理模型唯一 |
| Guinier | 在所选区间以ln(I)对q²回归，负斜率按Rg²=-3×斜率转换；正强度、背景及适用q范围需确认，误差来自变换坐标中的回归假设 |
| Porod | 在所选正q、正强度区间拟合ln(I)对ln(q)，报告负斜率对应的幂指数；不将任意区间斜率直接称为理想Porod极限 |
| 取向 | Herman相关计算明确参考方向、观测/拟合背景及角度覆盖；所选截断域不能冒充完整取向分布，缺失角度不自动补全 |
| 演示 | Qt原生界面状态捕获；部分动图为参数更新/工具请求回放，部分为离屏拖动事件。停留时间经过编辑，不作为性能基准 |

## 参考文献与官方说明

1. Ashiotis, G. et al. The fast azimuthal integration Python library: pyFAI. *Journal of Applied Crystallography* **48**, 510–519 (2015). https://doi.org/10.1107/S1600576715004306
2. Knudsen, E. B. et al. FabIO: easy access to two-dimensional X-ray detector images in Python. *Journal of Applied Crystallography* **46**, 537–539 (2013). https://doi.org/10.1107/S0021889813000150
3. [pyFAI几何约定](https://pyfai.readthedocs.io/en/stable/geometry.html)。
4. [SciPy最小二乘求解器](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html)。

这些引用分别用于积分/读取方法及实现说明，不是对XRelay整套程序科学有效性的背书。具体模型研究需另引实际采用的原始模型文献；不以依赖清单替代方法引用。

## 数据与图像说明

AgBh、氧化铝及其他参考图案的来源入口保留在展示页页脚。各图的采集几何、遮挡和显示尺度不同，不跨图比较颜色对应的绝对强度。背景操作示例含明确命名的合成背景；匿名SAXS/WAXS演示使用用户授权的可视化，路径和名称脱敏。

用户已确认保留并使用选定处理图；来源区分其提供的标样库及按其要求下载的官方参考数据。不宣称已独立取得第三方书面授权，不将软件MIT许可自动延伸到站外数据。原始数组、身份映射、研究数值结果及内部记录不进入拟公开仓库。
