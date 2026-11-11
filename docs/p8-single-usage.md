# 0.9.0 单文件版

`dist/p8-single/LiveViewer-0.9.0-single.exe` 是独立启动的 Windows 11 x64 程序。发送给另一台电脑时只需复制这个 EXE，不需要旁边有 runtime 文件夹，也不需要安装 Python 或 Qt。

原文件夹版 `dist/p8/LiveViewer-0.9.0/LiveViewer-0.9.0.exe` 需要同目录的完整 runtime 文件夹。只复制原 EXE，会出现找不到 `runtime/python314.dll` 的启动错误；这不是直播解析失败。

## 使用和数据

- 把单文件 EXE 放到可写目录，双击运行。在另一台电脑安装 PotPlayer，并在程序“设置”页选择该电脑的实际路径。
- 依赖已嵌入 EXE，启动时自动解压到 Windows 临时目录，正常退出后清理。首次启动比文件夹版稍慢；缓存解压目录不是收藏和账号的保存位置。此行为见 [PyInstaller 单文件说明](https://pyinstaller.org/en/stable/operating-mode.html#how-the-one-file-program-works)。
- 收藏、历史、设置和本机加密登录继续存放在 EXE 旁的 data 文件夹。只复制 EXE 可以启动，但不会带走原来的收藏和设置；需要迁移时，退出程序后复制旧 data，并保留原目录。
- Windows 加密登录绑定原 Windows 用户，换电脑应重新扫码。无需在聊天中提供 Cookie。
- 程序功能与已验收的 0.9.0 相同，包括直播、普通视频、番剧和 PotPlayer 原生控制。具体范围见原 P8 使用说明及验收记录。

## 构建与分发文件

源码环境安装好 requirements-qt-build.txt 后，先执行 `tools/build-p8.py` 生成文件夹版基线，再执行 `Build-Single-EXE.cmd` 或 `tools/build-p8-single.py`。两个输出目录独立，原稳定包保留。

`LiveViewer-0.9.0-source-and-licenses.zip` 提供对应源码、构建说明与依赖许可证，也嵌入单文件 EXE 的 release 资源。它无需放在 EXE 旁才能运行。构建只收集程序资源，不包含个人 data、Cookie、登录或 PotPlayer 本体。

## 验证方式与范围

`tools/check-p8-single.py` 将 EXE 单独复制到带中文和空格的隔离目录，移除 Python / Qt 环境路径，核对内嵌依赖、普通启动和退出、默认 data 位置、页面交互及匿名播放。普通视频使用现有 P7 轨道 / 字幕 / 恢复探针；番剧使用 P8 匿名探针。所有验证使用新建数据，不读取正常账号或修改原设置。具体结果见本次单文件验收记录。

对照检查发现，现有 P6 普通视频探针在定位到末尾附近时，文件夹版和单文件版都会出现“输入提前结束”的状态误判。此问题单独保留在验收记录，本次单文件打包沿用已验证的 0.9.0 播放逻辑；不能将独立启动通过解读为已修复这个既有边界问题。

另一台 Windows 11 应使用新的 `LiveViewer-0.9.0-single.exe` 复测；本机隔离测试不能替代另一台机器的全部兼容性验收。
