# P2 PotPlayer 接管

日期：2026-10-11。版本：0.5.2-p2。安装版本、实播、恢复与发行包检查记录在 .research/p2-* 报告。

| 项目 | 实现 |
|---|---|
| 实例与配置 | 缓存已选择的 64 位安装文件，每次使用 LiveViewerPot.exe 与独立 INI；不复制原 INI、历史或播放列表 |
| 所有权 | 核对 Popen 进程、精确映像路径、主窗口 PID 与 PotPlayer64 类名，阻止错误窗口或进程接管 |
| 媒体替换 | 稳定本机监听器，每个 GET 捕获独立上游尝试；结束旧流再在专用窗口重开当前项，不追加地址、不拼接 FLV |
| 状态与恢复 | 后台查询播放、暂停、结束、关闭媒体、尺寸、FPS、进度与音量；按会话代次隔离迟到结果，用户意图优先 |
| 错误 | 独立配置使用实测 IgnoreErrorMsg=1；错误经工具告示和日志呈现；控制失败停止自动操作 |
| 发行 | 不打包 PotPlayer 或 libmpv，不包含个人 data，Qt 迁移仍在 P3 |

后台查询超时 200 毫秒；关闭媒体独立放宽到 3 秒，避免长暂停时清理缓冲的正常等待被误认为失败。关闭与重开之间再次检查取消指令。退出时关闭本工具的播放器并清理有所有权标记的运行目录，静态文件与专用偏好保留。

当前接管支持 64 位 PotPlayer。接口不是稳定官方 SDK，版本不兼容、窗口不匹配或控制超时会明确提示并停止自动控制。播放器返回状态、尺寸和进度是活动证据，不能等同于核实了画面与声音。

验证使用匿名会话、隔离目录、静音窗口。原设置对比只计算目标播放器配置摘要，不导出其值。断流只中断本工具的读取，不修改 Windows 网络。

测试覆盖文件与设置隔离、所有权、超时、取消后的迟到打开、稳定地址下完整流替换以及已有功能回归。真实检查覆盖三次手动重连、受控断流恢复、长暂停与继续、工具停止、外部播放器停止及取流期间关闭窗口。最终播放列表与发行包结果见验收报告。

- 127 项自动测试通过，无跳过。
- 本机 PotPlayer64.dll 1.7.22619.0，匿名斗鱼 6657：1920×1080、播放器报告约 30fps、静音实播；三次手动重连及一次受控断流恢复通过。
- 长暂停 / 继续、工具停止、播放器关闭播放、准备取流期间关闭窗口均通过；原配置摘要不变。
- 当前播放属于临时专辑，退出时不保存条目。已直接展开实际列表核对，三次手动重连及一次自动恢复后仍只有一项 live.flv；截图 .research/p2-final-validation.playlist.png。
- 实播全过程采集到的媒体确认对话框数量为 0；本工具故障提醒为非阻塞形式。
- 实际 EXE 的离线自检与 ZIP 检查通过：P1 / P2 检查、独立配置、完整流替换、凭据加密及设置恢复；缺少可选 libmpv 不影响通过。报告 .research/p2-packaged-validation.json、.research/p2-package-artifacts.json。

实播综合结果：.research/p2-final-validation.json。发行包约 21.2 MiB，未包含个人 data、PotPlayer 或 libmpv。Qt 前端与长时间稳定性检查按路线进入 P3 / P5。

重复检查：tools/probe-potplayer.py、主程序 --self-test、tests/test_potplayer.py、tests/test_app_events.py。

技术依据：[本机命令行与资源核对](potplayer-assessment-2026-10-10.md)、[命令表](https://github.com/ld3l/PotPlayerControl/blob/main/InternalSimpleCmd.h)、[超时消息接口](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw)、[Scoop 独立 INI](https://github.com/ScoopInstaller/Extras/blob/master/bucket/potplayer.json)。
