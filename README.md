# PulseAtlas

**桌面上的硬件运行状态，一眼可见。**

PulseAtlas 是面向 Windows 的轻量只读监控悬浮窗。查看 CPU、物理内存，以及当前选中显卡的占用、温度、显存和功耗；支持置顶、透明度、折叠和设备选择。

从个人使用的监控工具整理而来，公开版本不包含固定用户名、安装路径、本机硬件型号或第三方工具包。Python 运行时不需要 pip 包；真实传感器数据仍依赖硬件、驱动和外部采集工具，缺失值显示 `—`，不是 0。

## 项目目标

我希望把日常查看电脑状态这件事做得更简单：不必一直打开完整的硬件管理面板，就能在工作、创作或游戏时看到关键指标，并知道这些数据来自哪里、是否仍然有效。

- **一眼看懂当前状态**：用紧凑的桌面窗口呈现 CPU、内存与所选显卡的信息，一次聚焦一张显卡，避免把不同设备的数据混在一起。
- **真实数据优先**：从系统接口和本机数据源读取，不用演示值填补缺失；无法取得或已经过期的读数明确显示不可用。
- **换台电脑也能用**：自动识别设备，使用通用入口与可选端口，不把开发者的用户名、安装目录或硬件型号写进运行逻辑。
- **保持本地、只读、可控**：不上传遥测，不调节风扇、电压或频率，不自动提权、安装驱动或改写启动项，让用户决定安装和运行方式。

首版先完成可靠的基础监控与设备选择。后续优先根据真实使用反馈完善兼容性、设置保存和启动体验，不以堆叠指标替代读数准确性。PulseAtlas 不是超频工具、压力测试工具，也不承担硬件保护或专业故障诊断。

## 开始使用

要求 Windows、Python 3.11+（含 tkinter）。先在源码目录测试演示模式：

```powershell
python -m pulseatlas --demo
```

演示含两张**同名、不同标识**的模拟显卡，第二张为高负载/高温。下拉选择一次只显示一张；点击“模拟断开 / 恢复显卡”检查失联状态。演示不访问硬件、LHM、NVIDIA 工具或网络。它不是电脑的实际读数。

真实采集需由你主动运行：

```powershell
python -m pulseatlas
# 如本机 LHM 使用其他端口：
python -m pulseatlas --lhm-port 8085
```

也可以运行 `python launch.py`。使用当前解释器与项目相对路径，不要求特定用户名或安装盘符。需要无控制台启动时可使用同一安装下的 `pythonw launch.py`；报错排查请使用 `python` 以保留终端提示。

## 数据从哪里来

| 指标 | 数据源 / 边界 |
| --- | --- |
| CPU 占用、物理内存占用和容量 | Windows API；首次 CPU 差分采样前显示不可用 |
| CPU 温度、功耗 | 本机 LibreHardwareMonitor；多 CPU 插槽不猜测聚合值 |
| 显卡指标 | 优先使用 LHM；没有 LHM GPU 设备时使用 PATH 中的 nvidia-smi |
| NVIDIA 兜底 | 按 UUID 区分设备，查询占用、核心温度、显存、功耗和风扇百分比 |
| 热点 / 显存温度 | 独立字段；不把显存温度当成 GPU 热点，不把 WMI 热区当成 CPU 温度 |

使用 LHM 时，请自行从 [LibreHardwareMonitor 官方项目](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) 获取并配置本机 Web Server。PulseAtlas 仅 GET `http://127.0.0.1:<端口>/data.json`，不使用代理，不跟随重定向；不安装驱动、不提权、不设置防火墙、不创建 URL 预留、不启动或关闭 LHM。

**客户端访问回环地址不代表 LHM 服务仅监听回环。** 请自行确认 LHM 服务的监听范围和防火墙，避免把硬件信息或控制接口暴露到局域网；本项目不会更改已有服务配置。

解析契约参考上游 [HttpServer.cs](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/master/LibreHardwareMonitor.Windows.Forms/Utilities/HttpServer.cs)：设备由 SensorId 的硬件部分分组，兼容没有 HardwareId 的旧树。使用数值 RawValue 或严格的带单位 Value；不猜测本地化数字或自定义传感器名称。同设备同名指标冲突时显示不可用，不以后来的读数覆盖前者。

## 显卡选择与降级

- 选择绑定设备标识，不绑定设备名称或列表序号，同名显卡可独立选择。
- 一次只显示一张显卡，切换后整体换成该设备的指标，不跨设备补数。
- 所选设备失联时保留选择并清空读数；恢复同一标识后自动显示。要查看其他设备可手动选择。
- LHM 与 nvidia-smi 的标识体系不同，不凭名称猜测它们是同一张卡。数据源切换后可能需要手动重新选择。
- 采集在后台进行，UI 不做网络请求。新快照失败时不复用旧 GPU 数据；超过 6 秒的快照不再展示读数。
- 只检测通信和本地采样新鲜度，无法证明上游传感器一直在刷新。固定数值可能是真实稳定值，也可能是上游缓存。
- 颜色阈值只是显示提示，不是厂商安全规格、过热保护或故障诊断。温度或占用越线都会变色。

显存容量以 GiB 展示：nvidia-smi 的 MiB 按 1024 换算；LHM 新旧版本的 RawValue 单位不同，因此容量只根据 Value 的显式单位换算，保留上游显示精度，不猜测无单位原始值。LHM 的 KB/MB/GB 使用 1024 进制，依据上游 [UnitManager.cs](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/master/LibreHardwareMonitor.Windows.Forms/UI/UnitManager.cs)。未提供某项数据的显卡仍可选择，但该项显示 `—`。

## 窗口与自启

拖动系统标题栏移动；右键或“设置”可切换置顶和透明度，按钮可折叠/展开、退出。窗口保留系统标题栏，便于正常移动与关闭。设备选择暂不跨进程保存。

默认不设置开机启动，也不修改任何已存在的监控或启动项。若需要自启，请主动创建指向当前 Python 和本项目 `launch.py` 的快捷方式，再自行放入 Windows 的当前用户启动文件夹；名称使用 PulseAtlas，与旧监控区分。快捷方式中的路径由自己的安装位置决定，不分发本机生成的快捷方式。

PulseAtlas 使用自己的当前用户会话实例锁，不以 `pythonw.exe` 是否存在判断重复启动。演示和真实模式使用不同锁；不会终止其他 Python 程序。

## 验证与限制

```powershell
python -m unittest discover -s tests -v
python -m compileall -q pulseatlas tests launch.py
```

测试使用合成传感器树、模拟进程输出与内存内 HTTP 响应，不需真实硬件、管理员权限或访问外部网络。不同品牌、多 GPU 实机、不同语言 LHM 和自启仍需用户验证，不宣称全硬件兼容。

v0.1.0 已通过 33 项自动化测试，并在一台 Windows 单显卡电脑上验证 Windows API 与 LHM 实时读取及桌面显示。多卡选择、断开恢复和过期状态有合成测试覆盖，不等于多显卡实机验证；NVIDIA 独立兜底与自启仍待实机验证。

首版不含 HWiNFO 共享内存读取器、WMI 温度兜底、驱动、安装包、自动更新、上传遥测或历史记录。HWiNFO 暂缓至其布局、边界和实机行为有独立验证后再考虑支持。

## 许可

自有代码使用 [MIT](LICENSE)。LHM、NVIDIA 驱动和工具不随仓库分发，各自许可独立。
