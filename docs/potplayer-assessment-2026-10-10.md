# PotPlayer、B 站点播与 RTX 超分评估

评估日期：2026-10-10。依据当前 0.5.0 代码、安装目录中的播放器说明、公开接口抽样及开发者资料。目标设备为 Windows 11、RTX 5060、2560×1440 显示器。工作量按一名熟悉当前代码的开发者估算，1 人日约 6–8 小时有效开发；属于范围估算，不能直接当作日历交付承诺。

## 结论

建议以外部 PotPlayer 为主播放器，工具继续管理平台解析、登录、收藏与本机媒体服务。现有代码已经有 PotPlayer 分支，基础切换成本低。未来支持 B 站普通点播可行，主要新增工作是音视频分轨、随机读取及播放状态管理。RTX 5060 满足 RTX Video 硬件范围，优先验证 PotPlayer 内置的 D3D11 GPU 超分。

“任意 B 站视频”不能统一承诺：范围应先限定为当前账号能访问、平台返回标准可播放媒体的普通投稿。番剧、电影、课程等另有内容接口及权限；地区限制、付费、删除、风控或需要专有解密的媒体，不能只靠更换播放器解决。[yt-dlp 的 B 站适配实现](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)

## 现有基础与迁移范围

代码 app.py 已通过 `/current /autoplay` 打开本机直播地址；settings.py 已保存 PotPlayer 路径和后端选择。用户此前已验证这条播放路径。扫码、Windows 用户加密登录、房间命名、收藏及已修好的网络模式可以保留。

基础迁移需要调整默认选择、界面提示、播放器控制封装和打包检查。现有构建无条件下载、携带 libmpv；PotPlayer 主版本可把它改成可选组件。调用用户已安装的 PotPlayer，能保持其原有皮肤、全屏控制和快捷键。

安装目录中的 CmdLine64.txt 确认支持 `/new`、`/current`、`/seek`、`/aud`、`/title` 与 `config="配置名"`。其中 `/aud` 可打开独立音轨，是点播试验的一个入口。仅有这些参数不能证明所有 DASH 组合及拖动同步已经可靠。

不建议把 PotPlayer 窗口强行挂进 Tk 窗体：当前没有可直接替代 libmpv 的统一嵌入接口证据。Windows 窗口挂接还需处理皮肤、焦点、DPI 和全屏生命周期。外部播放器更能保留用户选择 PotPlayer 的界面体验。

## 断网、旧源与弹窗

当前自动重连仅作用于 mpv；PotPlayer 分支重新开源时会生成新的本机地址，停止转发也没有同步发出播放器停止命令。这正是迁移中需要补齐的部分。

拟采用独立播放器实例或专用配置，记录本工具对应的窗口与进程，避免操作用户其他播放会话；重连时停止旧播放项、替换源并恢复播放。播放状态用于区分断流、手动暂停和关闭，避免自动重连违背用户操作。可考虑稳定的会话地址，但地址不变并不能单独保证播放列表不重复。

本机 English.ini 包含将错误显示在 OSD 并继续播放的选项，说明有减少模态弹窗的配置入口；还需在实际版本中验证断网行为。[社区 PotPlayerControl](https://github.com/ld3l/PotPlayerControl) 提供播放状态、时间和播放控制的 Windows 消息封装，[命令表](https://github.com/ld3l/PotPlayerControl/blob/main/raw.md) 包含清理播放列表。它们是社区接口证据，需要做版本探针及超时处理，不能作为稳定官方 SDK 的承诺。

如果窗口控制无法稳定替换旧源，持续媒体会话需要单独评估转封装或分段输出；不能把两个 FLV 的原始字节直接拼接。该备选路线不包含在下面的基本重连估算中。

## B 站普通点播

推荐链路为：BV/AV 链接 → 投稿元数据和分 P 的 cid → WBI 签名播放接口 → 视频 / 音频轨选择 → 本机点播服务 → PotPlayer。

普通投稿可以从 `/x/web-interface/view` 取得分 P，再使用 `/x/player/wbi/playurl` 获取播放信息。高画质常用 DASH，视频与音频分别提供；实际权限和清晰度以返回的轨道为准。这里参考的是社区维护的提取器与接口资料，并非 B 站对本软件提供的稳定 SDK。[yt-dlp 实现](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)、[接口采集项目](https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/video/videostream_url.md)

本次匿名抽样 BV17x411w7KC：元数据及播放接口均返回 code=0，包含 10 个分 P；取得两条视频格式与一条音频轨。请求 qn=64，实际返回 qn=16、512×288，这不能泛化为所有视频的匿名上限。视频和音频 CDN 均接受 `Range: bytes=0-4095`，返回 HTTP 206 和 MP4 初始化头。每轨只读取 4 KiB；没有完整下载、解码或在 PotPlayer 中实播。脱敏结果在 .research/bili-vod-assessment-2026-10-10.json。

现有 LocalRelay 仅为持续 HTTP-FLV 设计，没有点播所需的 Range / 206 / Content-Length 处理；直播的积压限制也不适用于点播暂停。因此需要新的 VodProxy：转发 HEAD 与字节范围，支持取消过时的读取，保持音视频同步，必要时刷新过期签名地址。

首次试验可用本机视频地址配 `/aud=本机音轨地址`；也可构造引用本机轨道的静态 MPD。PotPlayer 官方近期更新持续改进 HLS / DASH 处理，但仍需验证当前安装版本的实际兼容性。无损转封装为文件可作为另一种交付方式，拖动更直接，代价是下载等待和磁盘占用。[官方更新记录](https://t1.kakaocdn.net/potplayer/PotPlayer/v4/Update2/UpdateEng.html)

若“导出来”指永久保存文件，应额外提供下载音视频、完整性校验与无重编码合并；临时签名播放地址不适合作为永久收藏。视频索引建议使用 BV/AV + cid，播放进度按该索引保存。

## RTX 5060 + 1440p

视频领域对应的技术是 RTX Video Super Resolution（VSR）。NVIDIA 将它与 DLSS 分开定义；它用于放大已解码视频及减轻压缩伪影，不能保证恢复真实原始细节。该硬件在官方支持范围内，但本次没有启用或实测 VSR。[NVIDIA FAQ](https://nvidia.custhelp.com/app/answers/detail/a_id/5448)

| 源画面 → 1440p 显示 | 评估 |
|---|---|
| 720p → 1440p | 每个方向放大 2 倍，适合检查清晰度改善与伪影 |
| 1080p → 1440p | 每个方向约 1.33 倍，适合现有斗鱼直播 |
| 1440p → 1440p | 没有尺寸放大；可检查去压缩伪影效果 |
| 高质量 4K → 1440p | 优先正常缩小显示，不能为了超分而默认降低源画质 |

PotPlayer 官方在 2023-04-05 的更新中加入内置 D3D11 GPU 超分；本机 1.7.22619 的语言资源也存在该开关。首选内置 Direct3D 11 渲染器、RTX 5060 渲染设备和 D3D11 GPU 超分设置，再核对 NVIDIA App 中的启用状态。质量先用 Auto。直播与点播都应分别做开关对比、状态检查和帧率验收，不能把 GPU 忙碌等同于 VSR 已激活。[PotPlayer 更新记录](https://t1.kakaocdn.net/potplayer/PotPlayer/v4/Update2/UpdateEng.html)、[NVIDIA 设置说明](https://nvidia.custhelp.com/app/answers/detail/a_id/5448)

社区存在 MPC Video Renderer 的 RTX 扩展，可作兼容性备选；其版本、输出格式和播放器支持需另外验证。[作者项目](https://github.com/emoose/VideoRenderer/releases/tag/rtx-hdr-1.2) 自研渲染器另接 RTX Video SDK 会增加帧访问与 GPU 管线工作，当前外部 PotPlayer 路线无需先走这一步。[RTX Video SDK](https://developer.nvidia.com/rtx-video-sdk)

## 工作量与验收

| 工作项 | 增量估算 | 范围 / 验收 |
|---|---:|---|
| PotPlayer 默认播放及打包调整 | 0.5–1 人日 | 播放路径识别、保留房间与登录、可选 mpv 包 |
| 专用实例、旧源清理和断流恢复 | 2–4 人日 | 先验证窗口命令与 OSD 错误模式；不重复源，暂停 / 关闭不自动恢复 |
| RTX VSR 配置与验收 | 0.5–1 人日 | 现有设备 720p / 1080p、30 / 60fps 的激活与效果对比 |
| B 站点播 MVP | 4–6 人日 | 普通 BV/AV、分 P、现有登录、音视频同步、Range 拖动、收藏 |
| 点播完善 | 额外 3–5 人日 | 签名过期、长暂停、断点续播、字幕、更多编码及错误分类 |
| 永久文件导出 | 可选，额外 1–3 人日 | 双轨下载、无损合并、进度与失败恢复；限已支持的普通媒体 |

按上述范围，直播切换 + VSR + 点播 MVP 合计约 7–12 人日；含点播完善约 10–17 人日。特殊内容类型、播放器嵌入、任意受保护视频和自研 AI 引擎没有包含在总数内。平台接口变化或窗口控制验证失败会使估算上调。

扩展性判断：平台和内容类型适配较强，标准媒体播放较强；播放器皮肤与增强依赖 PotPlayer，本工具对内部帧和行为的控制较弱。建议保留独立的 SourceResolver、LiveRelay / VodProxy 和 PlayerAdapter 边界，后续可以增加来源或其他播放器。

推荐顺序：先完成 PotPlayer 默认播放与 VSR 验收，再解决断流和旧源，随后进入普通 B 站点播 MVP。本次评估完成了代码审查、安装资源核对及公开接口探测，尚未实现以上新增功能。
