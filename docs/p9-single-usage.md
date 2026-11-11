# 0.10.0 单文件版

`dist/p9-single/LiveViewer-0.10.0-single.exe` 是独立启动的 Windows 11 x64 程序。
发送给另一台电脑时只需复制这个 EXE，不需要旁边有 runtime 文件夹，也不需要安装
Python 或 Qt。

文件夹版 `dist/p9/LiveViewer-0.10.0/LiveViewer-0.10.0.exe` 需要同目录的完整
runtime 文件夹。只复制原 EXE 会出现找不到 `runtime/python314.dll` 的启动错误；
这不是直播解析失败。

## 使用和数据

- 把单文件 EXE 放到可写目录，双击运行。在另一台电脑安装 PotPlayer，并在程序
  “设置”页选择该电脑的实际路径。
- 依赖已嵌入 EXE，启动时自动解压到 Windows 临时目录，正常退出后清理。
  首次启动比文件夹版稍慢；缓存解压目录不是收藏和账号的保存位置。
  此行为见 [PyInstaller 单文件说明](https://pyinstaller.org/en/stable/operating-mode.html#how-the-one-file-program-works)。
- 收藏、历史、设置和本机加密登录继续存放在 EXE 旁的 data 文件夹。
  只复制 EXE 可以启动，但不会带走原来的收藏和设置；需要迁移时，退出程序后
  复制旧 data，并保留原目录。
- Windows 加密登录绑定原 Windows 用户，换电脑应重新扫码。无需在聊天中提供 Cookie。
- 程序功能与文件夹版 0.10.0 相同：斗鱼 / B 站 / 抖音直播、B 站普通视频与番剧、
  PotPlayer 原生控制与回看。抖音只支持 Cookie 导入，不提供扫码登录。

## 构建与分发文件

源码环境安装好 `requirements-qt-build.txt` 后，先执行 `tools/build-p9.py` 生成
文件夹版基线，再执行 `Build-Single-EXE.cmd` 或 `tools/build-p9-single.py`。
两个输出目录独立，0.9.0 稳定包保留。

`LiveViewer-0.10.0-source-and-licenses.zip` 提供对应源码、构建说明与依赖许可证，
也嵌入单文件 EXE 的 release 资源。它无需放在 EXE 旁才能运行。
构建只收集程序资源，不包含个人 data、Cookie、登录或 PotPlayer 本体。

## 与 0.9.0 差别的验证边界

单文件打包沿用已验证的 0.10.0 播放逻辑。抖音相关的取流、档位、CDN 白名单与
延迟结论见 [P9 验收](p9-acceptance.md)；单文件版未单独做抖音实播验收，
需要时可在另一台机器用新 EXE 复测。

另一台 Windows 11 应使用新的 `LiveViewer-0.10.0-single.exe` 复测；
本机隔离测试不能替代另一台机器的全部兼容性验收。
