# 工程总览 · 本地直播 / LiveViewer 0.10.0

本文件是对现有代码的现状总结，供新增功能前对照使用。基于源码实际读取，不替代各阶段验收文档。

- 规模：49 个 Python 源文件；31 个测试文件；40 余篇文档（P9 后统计）
- 入口：`qt_main.py`（=`live_viewer.qt.app:main`，版本 0.10.0）；`legacy_main.py`（旧 Tk，0.5.2-p2，已冻结）
- 平台：斗鱼、B 站、抖音（抖音为 P9 新增，只支持 Cookie 导入，不支持扫码）
- 版本控制：Git，基线 `e6197ab`（0.9.0），P9 在 `feature/douyin-live` 分支

---

## 1. 一句话定位

Windows 桌面观看软件：解析斗鱼 / B 站直播与 B 站普通视频、番剧，在本机起一个 **只监听 127.0.0.1 的转发服务**（HTTP-FLV 或本地 RTMP），把该本地地址交给**受控的 PotPlayer 进程**播放。平台 Cookie 与签名 CDN 地址从不离开主程序。

主程序负责：取流、登录、收藏、历史、诊断、会话与事件；PotPlayer 负责窗口、进度条、全屏、渲染。

---

## 2. 分层架构

```
┌─ 入口 ────────────────────────────────────────────┐
│ main.py / qt_main.py → live_viewer/qt/app.py:main │
└──────────────────────┬────────────────────────────┘
                       │
┌─ Qt 界面层 live_viewer/qt/ ───────────────────────┐
│ ViewerWindow(7 页) · VodPage · BangumiPage        │
│ widgets / logs / notifications / qr_dialog        │
│ 只调 LiveRuntime 与 EventCenter，不直接碰网络     │
└──────────────────────┬────────────────────────────┘
                       │  QTimer 200ms → tick()
┌─ 业务编排层 ──────────────────────────────────────┐
│ runtime.py    LiveRuntime（门面）· Jobs（线程池） │
│ playback.py   PlaybackController（状态机/令牌）   │
│               PlaybackService（解析+建 relay）    │
│ events.py     EventCenter / JsonlStore（脱敏）    │
│ reports.py    报告 schema + 白名单裁剪            │
│ reconnect.py  ReconnectPolicy（1/2/5/10/20/30s）  │
│ resources.py  内存读数 · CacheLease              │
└──────────────────────┬────────────────────────────┘
                       │
┌─ 传输层 ──────────────────────────────────────────┐
│ relay.py      LocalRelay + RelayEndpoint(HTTP-FLV)│
│ rtmp.py       ReplayRelay + RtmpEndpoint(RTMP)    │
│ live_buffer.py LiveBuffer（>3s/8MiB 判定积压）    │
│ timing.py     FlvClock（FLV 时间戳）              │
│ vod_proxy.py  VodProxy（Range 请求代理, 双轨）    │
└──────────────────────┬────────────────────────────┘
                       │
┌─ 平台 / 网络层 ───────────────────────────────────┐
│ core.py       PlatformClient / Stream / Quality   │
│ network.py    IPv4 优先 adapter · 连接错误分类    │
│ qr_login.py   扫码登录   credentials.py DPAPI     │
│ accounts.py   verify_login · CredentialWriter     │
│ settings.py   StateStore · sanitize_settings      │
└──────────────────────┬────────────────────────────┘
                       │
┌─ 播放器层 ────────────────────────────────────────┐
│ potplayer.py  PotPlayerAdapter（Qt 版唯一后端）   │
│               PrivateProfile · Win32Player        │
│ mpv_player.py libmpv ctypes（仅旧 Tk 版使用）     │
└───────────────────────────────────────────────────┘
```

内容源旁支：`vod.py`（普通投稿 BV/AV/分P、WBI 签名、DASH 轨道）、`bangumi.py`（PGC 季/集/EP 权限）、`subtitles.py`（CC → SRT）、`vod_state.py` / `bangumi_state.py`（进度书签）。

---

## 3. 一次直播播放的完整链路

```
用户点“开始播放”
  → ViewerWindow.start() 组装 request dict
     {platform, room_input, requested, room_name, session, low_latency, network_mode,...}
  → LiveRuntime.play(request)                       runtime.py:142
     ├ 校验 config["player"] 是 .exe（PotPlayer 路径）
     ├ session=="导入 Cookie" 时取 self.cookies[platform]
     ├ 复用/重建 PotPlayerAdapter（profile 或 options 变化则关旧进程）
     └ controller.begin(request) → SessionToken(session_id, generation, target)

  → jobs.submit("playback.prepare", work, done, failed, valid=current(token))
     └ 工作线程：player.prepare() + PlaybackService.prepare()
        ├ PlatformClient(platform, cookies).resolve(room, requested)
        │    ├ _bilibili: room_init → getRoomPlayInfo（http_stream/flv/avc）
        │    └ _douyu:   房间网页取真实房间号 → websec 签名 → getH5PlayV1
        │    返回 Stream(actual, qualities, urls, backup_cdns)
        ├ 构造 LocalRelay（low_latency）或 ReplayRelay（replay_mode → native_replay）
        └ relay.prepare_first_source()  // 只连上游，不启播

  → 回主线程 attach(result)                          runtime.py:212
     ├ relay.start()  → 得到 http://127.0.0.1:<port>/...（或 rtmp://127.0.0.1:...）
     └ player.open(relay.url, room_name, volume, mute, guard=...)

  → QTimer 200ms → LiveRuntime.tick()                runtime.py:544
     player.poll()/snapshot() → controller.publish()
     → EventCenter.emit() → 状态卡片 / 通知区 / 日志页
```

VOD / 番剧走同一条 `play()`，只是 `request["kind"]="vod"`（番剧再加 `content_type="pgc"`），`PlaybackService` 分派到 `VodClient` / `BangumiClient`，用 `VodProxy`（Range 代理）并给 `player.open(audio_url=...)` 第二路音轨。

---

## 4. 模块清单

### 入口与界面（`live_viewer/qt/`，3487 行）

| 文件 | 行 | 职责 |
|---|---:|---|
| `app.py` | 1323 | `ViewerWindow`：7 页导航、播放操作、诊断、账号、日志、关机清理；`main()` 解析全部 `--probe-*` 参数 |
| `vod_page.py` | 327 | B 站视频页：异步查询、分 P、轨道选择、收藏 |
| `bangumi_page.py` | 361 | 番剧页：季度/选集、EP 权限、收藏与续播 |
| `widgets.py` | 109 | `text/row/card/button/line_icon/scrolling_page` 控件工厂 |
| `logs.py` | 108 | `LogModel` + `LogFilter` + `SOURCES` 来源分类 |
| `notifications.py` | 143 | `NoticeManager`：Windows toast 被抑制时保留未读 |
| `qr_dialog.py` | 169 | 非模态扫码对话框 |
| `validate.py` / `validate_features.py` | 218 / 138 | 离线交互自检 |
| `live_probe.py` / `feature_probe.py` / `vod_probe.py` / `p7_probe.py` / `p8_probe.py` / `bangumi_validation.py` | 66 / 71 / 120 / 130 / 136 / 295 | 各阶段验收探针 |

`PAGE_TITLES = ("观看","账号","设置","活动日志","诊断","B 站视频","B 站番剧")`

### 业务编排

| 文件 | 行 | 关键符号 |
|---|---:|---|
| `runtime.py` | 621 | `LiveRuntime`、`Jobs`；`play/reconnect/pause/stop/tick/close/report` |
| `playback.py` | 335 | `PlaybackController`（状态机 + `SessionToken` 世代号）、`PlaybackService`、`OperationController` |
| `events.py` | 310 | `EventCenter`、`JsonlStore`、`safe_text`、`EVENT.LEVELS/IMPACTS/PRESENTATIONS` |
| `reports.py` | 142 | `shape()` schema 常量、`clean()`、`save_diagnostic_report()` |
| `reconnect.py` | 38 | `ReconnectPolicy` |
| `resources.py` | 58 | `available_memory`、`CacheLease` |
| `player_window.py` | 35 | 旧 Tk 播放外壳（遗留） |

### 传输

| 文件 | 行 | 关键符号 |
|---|---:|---|
| `relay.py` | 397 | `LocalRelay`、`RelayEndpoint`；`url/start/prepare_first_source/snapshot/stop` |
| `rtmp.py` | 368 | `ReplayRelay`、`RtmpEndpoint`、AMF 编解码、`flv_tags` |
| `vod_proxy.py` | 333 | `VodProxy`、`byte_range` |
| `live_buffer.py` | 88 | `LiveBuffer`、`RelayBacklog` |
| `timing.py` | 73 | `FlvClock` |

### 平台 / 网络 / 凭据

| 文件 | 行 | 关键符号 |
|---|---:|---|
| `platforms.py` | 138 | `PlatformSpec`、`PLATFORM_SPECS`、`require_spec`；**新增平台的唯一入口** |
| `core.py` | 522 | `PlatformClient`（`resolve/_bilibili/_douyu/_douyu_play/media_urls`）、`Stream`、`Quality`、`parse_douyu_room`、`room_number` |
| `network.py` | 110 | `IPv4FirstAdapter` 系列、`connection_kind`、`connection_message` |
| `qr_login.py` | 242 | `QrLogin`、`LoginHttp`、`login_url`、`qr_png` |
| `credentials.py` | 91 | `CredentialStore`（DPAPI 当前用户） |
| `accounts.py` | 72 | `verify_login`、`CredentialWriter` |
| `settings.py` | 189 | `StateStore`、`sanitize_settings`、`atomic_write` |
| `qr_dialog.py` | 163 | 旧 Tk 扫码窗 |

### 内容源

| 文件 | 行 | 关键符号 |
|---|---:|---|
| `bangumi.py` | 367 | `BangumiClient`、`BangumiProxy`、`Season`、`Episode`、`BangumiSource` |
| `vod.py` | 219 | `VodClient`、`VideoSource`、`Track`、`wbi_params`、`MIXIN` |
| `subtitles.py` | 80 | `catalog`、`to_srt`、`download` |
| `vod_state.py` / `bangumi_state.py` | 50 / 42 | 进度书签、季/集收藏 |

### 播放器

| 文件 | 行 | 关键符号 |
|---|---:|---|
| `potplayer.py` | 805 | `PotPlayerAdapter`（`prepare/open/stop/pause/resume/seek/poll/snapshot/close`）、`PrivateProfile`、`Win32Player` |
| `mpv_player.py` | 306 | `MpvPlayer`（ctypes，仅旧版） |

### 诊断

`diagnostics.py`（208，打包后自检）、`p1_diagnostics.py`、`p2_diagnostics.py`。

---

## 5. 硬性设计约定（改代码必须遵守）

1. **播放器只吃本地地址。** `potplayer.valid_local_url()` / `mpv_player.validate_local_stream()` 拒绝非 `127.0.0.1` 地址；Cookie 与签名 URL 永不出现在播放器命令行。
2. **日志/报告脱敏。** `events.safe_text()` 用正则滤掉 `http(s)/rtmp/ws` URL 与密钥；`reports.clean()` 按 `shape()` 白名单裁剪，**新字段不加 schema 就会被裁掉**。
3. **过期结果必须丢弃。** 所有异步回调带 `SessionToken`，`controller.current(token)` 为假就丢弃并发 `playback.stale_result`。改异步逻辑别绕过它。
4. **后台线程不碰 Qt。** 网络与播放器控制在 `Jobs` 工作线程，结果回主线程回调。`Jobs.cancel(channel)` 用于取消。
5. **等级 ≠ 提醒强度。** `level`(DEBUG…CRITICAL) 与 `impact`(none/degraded/start_blocked/interrupted/app_blocked) 和 `presentation`(log/toast/banner/strong) 分开判断；`dedupe_key` 防重复弹窗。
6. **设置原子写。** `atomic_write()` 临时文件 + fsync + replace；不可解析文件先备份 `settings-unreadable-*.bak`。
7. **凭据只经 DPAPI。** 当前 Windows 用户范围加密，存 `credentials/<平台>.dpapi`；未勾选“记住登录”时只在内存。
8. **不修改用户 PotPlayer 全局配置。** `PrivateProfile` 起独立便携 profile 与临时目录，退出清理本工具创建的进程与缓存。
9. **验证必须用独立 `--data-dir`。** `main()` 强制校验，禁止触碰正常用户数据。
10. **本地转发不重编码。** FLV tag 原样转发，保持源编码；不拼接两个 FLV 的字节。

---

## 6. 数据与持久化

- 源码运行：`<项目>/.local/`；EXE：`<EXE 目录>/data/`
- `settings.json`（`sanitize_settings` 归一化，`version: 2`）主要键：
  `favorites` `history` `last_room` `room_preferences(quality/mode)` `player` `playback_backend`
  `auto_reconnect` `native_replay` `replay_minutes` `vsr` `volume` `mute`
  `network_mode`(direct/system) `remember_login` `ui{strong_notifications,background_notifications}`
  `low_latency`
  `video_favorites` `video_history` `last_video` `video_progress` `vod_resume`
  `bangumi_favorites` `bangumi_history` `last_bangumi`
- 日志：`logs/*.jsonl`（`JsonlStore`，脱敏 + 轮转）
- 播放器私有 profile：`data/potplayer/engines|runs/<id>/`
- 收藏索引原则：**平台 + 数字房间号**（名称可重复、可改名，不作为索引）

---

## 7. 构建与验收

- 依赖：`requirements.txt` → `PySide6-Essentials 6.12.0` / `requests` / `qrcode[pil]`；构建用专用 `.venv-p0`、`.venv-build`
- 构建：`Build-Live-Viewer.cmd` → `tools/build-p8.py`（PyInstaller `--onedir --windowed --contents-directory runtime`，产物 `dist/p8/LiveViewer-0.9.0/` + ZIP）
- 单文件：`Build-Single-EXE.cmd` → `tools/build-p8-single.py`
- 校验：`tools/check-p8-ui.py`、`tools/check-p8-single.py`、`python -m unittest discover -s tests -v`
- 运行期自检参数（`qt/app.py:1270`）：`--validate` `--probe-live` `--probe-features` `--probe-vod` `--probe-p7` `--probe-p8` `--probe-p8-member` `--probe-p8-full`
- **版本号有 3 处耦合**：`live_viewer/qt/app.py` 的 `VERSION`、`tools/build-p8.py:16` 的校验字符串与 `name`、各 `docs/*.md`

---

## 8. 加功能落点速查表

### A. 新增一个直播平台
平台差异已收敛为声明式注册表 `live_viewer/platforms.py` 的 `PlatformSpec`，不再写平台名分支。

1. `platforms.py` `PLATFORM_SPECS` 增加一项：`key` / `caption` / `referer` / `page_hosts` /
   `cookie_domains` / `media_domains` / `resolve_method` / `preferred_quality`，以及按需的
   `import_credentials` / `paste_alias` / `qr_login` / `qr_credentials` / `qr_cancel_url` /
   `verify_login` / `device_cookie` / `resign_backups` / `topic_query_room` / `max_room_digits`
2. `core.py` 的 `PlatformClient` 上新增 `resolve_method` 指向的取流方法（接收 `room, requested`）
3. `REFERERS` / `COOKIE_DOMAINS` / `MEDIA_DOMAINS` / `settings.PLATFORMS` 均由注册表自动派生，
   **不需要再手改**
4. 需要登录校验时实现 `login_status()` 分支并把 `verify_login=True`；否则填 `unverified_message`
5. 无扫码入口就设 `qr_login=False`，`QrLogin` 会先拒绝再建 HTTP 客户端，账号页自动隐藏扫码按钮
6. `core.py` 的 `Stream.qualities` / `backup_cdns` 备份线路语义要对齐

### B. 新增播放器后端
- 实现与 `PotPlayerAdapter` 同形的接口：`prepare / open / stop / pause / resume / seek / poll / snapshot / close`
- 注入点 `runtime.py:87` `player_factory=PotPlayerAdapter`
- 注意 `runtime.py:156-166` 硬编码了 PotPlayer 的 `.exe` 校验与 `player.profile.source` 比较，需一并抽象

### C. 新增 Qt 页面
1. `qt/app.py:66` `PAGE_TITLES` 加标题（导航按钮自动生成）
2. `qt/app.py:40` `CAPTIONS` 加副标题（与索引对齐）
3. `qt/app.py:181-185` 实例化并 `self.stack.addWidget(...)`
4. `qt/app.py:694 tick()` 加 `self.xxx_page.tick()`
5. 注意 `go_page(3)` 等硬编码索引（日志页）

### D. 新增内容源（点播类）
1. 仿 `vod.py` / `bangumi.py` 新写 `xxx.py`：`XxxClient` + `XxxSource` + `XxxProxy(VodProxy)`
2. `playback.py:200` `PlaybackService.prepare()` 按 `request["kind"] / ["content_type"]` 加分支
3. 新增 `qt/xxx_page.py` 并按 C 注册
4. `settings.py:52 sanitize_settings` 加数据项；`reports.py` 加 schema

### E. 新增设置项
1. `settings.py:52 sanitize_settings` 加默认值 + 类型/范围白名单（考虑 `version` 升 3）
2. `qt/app.py:308 _settings_page` 加控件
3. `qt/app.py:485 save_settings` / `:495 update_options` 接上

### F. 新增事件 / 通知
- 统一走 `EventCenter.emit(code, message, source=..., level=..., presentation=..., impact=..., scope=..., dedupe_key=...)`
- 枚举在 `events.py:6-8`；日志页来源分类在 `qt/logs.py` `SOURCES`

### G. 新增诊断报告字段
- 在 `reports.py` 的 `shape(...)` 里声明，否则 `clean()` 会丢弃

---

## 9. 现状风险

1. **没有版本控制。** 目录不是 Git 仓库，任何改动不可回滚。加功能前建议先 `git init` + 首次提交（`.gitignore` 已备好）。
2. **`.research/` 与 `dist/` 有两万余个 PotPlayer 副本文件**，占空间且干扰检索；已在 `.gitignore` 中排除 `.research/vendor/`、`dist/`。
3. **接口脆弱。** 斗鱼签名（`_douyu_play` 的 MD5 轮数 + auth）和 B 站 `getRoomPlayInfo` 会随平台改版失效，是主要维护点。
4. **双前端并存。** `live_viewer/app.py`（Tk，1342 行）与 `mpv_player.py` 已冻结但仍随包分发，改动共享模块（`core/relay/settings/events`）时**不能破坏 Tk 路径**，或需明确弃用。
5. **`LiveRuntime` 偏大（621 行）**，`play()` 与 `tick()` 里交织了重连、VOD 恢复、播放器替换三类逻辑，扩展时优先在新分支里加而不要继续内联膨胀。
6. **平台分支已收敛为注册表**（P9-B-1），加平台不再需要改 `core.py` 的分派逻辑；但 `PlatformClient`
   上仍要手写该平台的取流方法，且 `Stream` / `Quality` 的语义要自己对齐。
7. **真机验收依赖人工。** 会员资格、音画同步、长时间稳定性、大陆网络均需用户在 PotPlayer 中确认，自动化只能覆盖到“转发与状态”。
