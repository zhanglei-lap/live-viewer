# 0.2.0 日常使用版

日期：2026-10-06（香港时间）。用户已确认上一阶段基础测试无问题，并选择继续使用 PotPlayer。

## 本次完成

- 主界面分为观看、账号与播放器、画质对比与报告三个页面，默认窗口 1060×760，各页面支持滚动。
- 收藏支持自定义名称、改名、删除、直接播放；最近观看最多 30 个房间，只在实际转发到视频字节后记入，重复房间移动到顶部。
- 恢复上次房间、每个房间的画质和匿名 / Cookie 方式；没有可用 Cookie 时恢复为匿名，启动不会自动播放。
- 重新连接使用上次播放房间及档位，不受当前输入框选择其他房间影响。
- 登录记忆默认关闭，勾选后使用 Windows DPAPI 当前用户范围加密；取消勾选删除保存的登录但保留本次内存会话，清除登录同时删除两者。B 站登录失效时清除已保存登录。
- 设置写入失败保留原文件并提示，损坏文件在替换前备份；关闭窗口时清理轮询回调，后台任务不能在关闭后启动新的播放会话。
- 生成单文件 Windows x64 EXE、便携 ZIP 和构建脚本，旧收藏和播放器路径兼容迁移。ZIP 不包含个人设置或 Cookie。

## 验证结果

- 35 项自动化测试通过，含真实 Windows DPAPI 加密与解密，以及界面中记住登录的勾选、重新启动恢复、取消、清除和无效登录处理；账号测试全部使用合成凭据。
- 三页窗口截图检查通过，主要操作及状态栏均可见。
- 最终 EXE 在普通 Windows 11 25H2 桌面环境通过离线自检：Tk 界面、设置写入与恢复、房间画质恢复、登录加密/恢复/删除。
- 最终 EXE 直接访问公开房间 B 站 35、斗鱼 6657：匿名样本分别为 H.264 720p30 和 1080p30；两平台本机转发均为 HTTP 200，FLV 文件头有效。复测未导入用户账号，也未启动或打断已有 PotPlayer。
- EXE 首轮在工具受限环境下出现 TclError；普通桌面环境复测正常。该差异属于测试环境限制，不代表应用需要管理员权限。

## 交付与证据

- `dist/LiveViewer.exe`：本机版；首次构建已从旧设置迁移收藏和 PotPlayer 路径。
- `dist/LiveViewer-0.2.0-win-x64.zip`：只含 EXE 和使用说明的干净便携包，首次运行生成 data。
- `tools/build-windows.ps1`、`Build-Live-Viewer.cmd`：使用独立 .venv-build 重现构建。
- `.research/packaged-v02-live-check.json`：最终 EXE 离线与真实匿名取流自检。
- `.research/viewer-v02-watch.png`、`viewer-v02-account.png`、`viewer-v02-comparison.png`：界面检查。

登录使用 [微软 CryptProtectData / DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) 的当前用户范围，未使用 LOCAL_MACHINE 标志。打包采用 [PyInstaller 的单文件与无控制台模式](https://pyinstaller.org/en/stable/usage.html)。

## 后续验收

EXE 内置 Python 运行时，但尚未在另一台没有 Python 的干净机器验收。尚未完成 30–60 分钟持续观看、断网恢复与真实账号加密恢复后的跨启动实播验收。当前仅支持 PotPlayer、AVC HTTP-FLV，不包含内置播放器、扫码登录和无缝自动重连。
