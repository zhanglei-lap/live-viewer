# 抖音 `lls` 低延迟协议迁移评估

日期：2026-11-11。触发原因：抖音直播在 PotPlayer 中恒定落后网页版约 3 秒，
见 [抖音延迟调查](p9-latency-2026-11-11.md)。

## 结论摘要

**`lls` 不是 HLS，是 WebRTC（WHIP 类信令）。** 迁移等于在 Python 里实现一套
WebRTC 接收端并把 RTP 重新封装成 FLV，**约 25–40 人日，风险高**，且有一个可能
推翻方案的未知项（音频编码）。

**建议：暂不迁移。** 现有 FLV 链路 3.7 秒恒定延迟已是该协议的合理下限。
若确实要低延迟，优先考虑「外挂成熟实现」而不是自研。

## `lls` 是什么（实测证据）

三个房间的每个档位都同时提供三种地址：

| 协议 | 地址形态 | 实测 |
|---|---|---|
| `flv` | `pull-flv-q13.douyincdn.com/…/stream-xxx_or4.flv` | HTTP-FLV，`video/x-flv`，当前使用 |
| `hls` | `pull-hls-q13.douyincdn.com/…/stream-xxx_or4.m3u8` | 普通 HLS 主清单（`application/x-mpegurl`），**无 `EXT-X-PART`，不是 LL-HLS** |
| `lls` | `pull-lls-q13.douyincdn.com/…/stream-xxx_or4.sdp` | **WebRTC 信令端点** |

对 `lls` 端点的实测：

| 请求 | 响应 |
|---|---|
| `GET .sdp` | `HTTP 200`，`application/json`，`{"code":400,"message":"body error"}` |
| `POST` `text/plain`（带 SDP） | `{"errcode":101,"errmsg":"format error","remotesdp":{"type":"","sdp":""},"svrsig":""}` |
| `POST` `application/sdp`（带 SDP） | 读取超时（服务端接受了 offer 并等待媒体会话建立） |

`remotesdp: {type, sdp}` 加 `svrsig` 的结构是 WebRTC SDP 交换的典型形态，
配合 `application/sdp` 提交会挂起的行为，判定为 **WHIP 类 WebRTC 拉流**。
`.sdp` 后缀、UDP 媒体面、`svrsig` 签名都指向这一点。

因此网页版很可能用 `lls`（WebRTC）获得低延迟。**这是推断**：未抓取网页播放器的
实际协商过程，只基于响应字段与端点行为。

## 迁移需要实现的层次

WebRTC 不是改个地址就能用，它是一条完整的独立传输栈：

1. **信令**：构造合法 SDP offer（ICE ufrag/pwd、DTLS fingerprint、H.264/AAC 载荷），
   处理 `remotesdp` 与 `svrsig` 回传。
2. **ICE**：候选收集、STUN 连通性检查、候选对选择。
3. **DTLS + SRTP**：握手、密钥导出、逐包解密。
4. **RTP 接收**：序列号、时间戳、乱序重排、抖动缓冲、丢包与 NACK。
5. **去包化**：H.264 的 STAP-A / FU-A 拼回完整 NAL，提取 SPS/PPS。
6. **重新封装**：Annex-B 转 AVCC、构造 `AVCDecoderConfigurationRecord`、
   按 FLV tag 结构输出并维护时间戳，才能复用现有 `LocalRelay` 交给 PotPlayer。
7. **音频**：取决于 RTP 里是 AAC 还是 Opus，见风险一节。

现有转发层的设计是「FLV tag 原样透传，不解码不重编码」（`relay.py` 顶部注释）。
第 5–6 步引入的是真正的比特流层处理，性质与现有代码完全不同。

## 工作量分解

| 模块 | 内容 | 估算（人日） |
|---|---|---:|
| 信令 | SDP offer 构造、`svrsig`、错误分支 | 3–5 |
| ICE / DTLS / SRTP | 基于 `aioice` + `pylibsrtp` 接入；自研则 ×5 | 3–5 |
| RTP 接收与抖动缓冲 | 乱序、丢包、NACK | 4–6 |
| H.264 去包化 | STAP-A / FU-A、SPS/PPS | 3–5 |
| FLV 重封装 | Annex-B→AVCC、配置记录、时间戳 | 4–6 |
| 音频 | AAC 直通 1–2；若需 Opus→AAC 转码 +8–12 | 1–2 |
| 集成 | 双链路接入 `LiveRuntime` / 设置项 / 诊断 / 事件 | 3–5 |
| 打包与验收 | PyInstaller 冻结、体积、多房间稳定性实测 | 4–6 |
| **合计** | | **25–40**（不含转码） |

## 依赖与打包代价

| 依赖 | 作用 | 代价 |
|---|---|---|
| `aiortc` | ICE/DTLS/SRTP 与 RTP 收发 | 引入 asyncio 事件循环，与现有线程 + 队列模型并存需要桥接线程 |
| `av`（PyAV） | 解码/封装辅助 | 内含 FFmpeg 动态库，安装体积约 40–70 MB |
| `cryptography` / `pylibsrtp` | DTLS、SRTP | 额外本地库依赖 |

当前便携包 runtime 约 120 MB（主要是 Qt）。按上述依赖预计**再增加 60–120 MB**，
且 `aiortc` + PyAV 在 PyInstaller 下的冻结需要额外 hidden imports 与 OpenSSL DLL，
与「单文件 EXE 只复制一个文件即可启动」的目标直接冲突。

## 主要风险

| 风险 | 影响 | 说明 |
|---|---|---|
| **音频编码未知** | 可能推翻方案 | 若 RTP 里是 Opus，FLV 无法承载，只能转码 AAC。这会破坏「不重编码」的设计前提，并引入音频编码器依赖与音画同步问题 |
| 协议未公开 | 持续维护成本 | `lls` 是平台内部协议，字段与签名随时可能变化，没有稳定性承诺 |
| 依赖体积与冻结 | 分发形态改变 | 与单文件发行目标冲突，需要重新评估打包策略 |
| 稳定性 | 花屏/卡顿 | 朴素 RTP 实现丢包即花屏，需要 NACK/FEC 与足够的抖动缓冲 |
| 收益有限 | 投入产出比低 | 从 3.7 秒降到约 1 秒；只有在与网页版并排对比时才能明显感知 |

## 备选方案对比

| 方案 | 延迟 | 成本 | 风险 | 说明 |
|---|---|---|---|---|
| **A. 保持 HTTP-FLV** | 3.7 s 恒定 | 0 | 无 | 已是 FLV 链路合理下限；实测不随时间增长 |
| **B. 外挂成熟实现** | 约 1–2 s | 3–5 人日 + 约 100 MB 二进制 | 中 | 例如用 MediaMTX 之类支持 WHIP 接入的程序做 `lls → RTMP` 转换，再交给 PotPlayer。**与现有「只依赖 PotPlayer」的分发设计冲突**，并引入新的外部进程与许可问题 |
| **C. 自研 WebRTC→FLV** | 约 1–2 s | 25–40 人日 | 高 | 本文主要评估对象；音频编码是关键未知 |
| **D. 延迟敏感时用网页版** | 最低 | 0 | 无 | 不改变工具定位 |

## 建议

1. **暂不实施 C。** 25–40 人日、高风险、还要牺牲单文件分发，换取 2 秒左右，
   在「看直播」这个场景里不划算。
2. **保持 A 作为默认**，并把「约 3.7 秒恒定延迟」写进使用说明，让用户预期明确。
3. 若低延迟成为硬需求，**优先评估 B**：用成熟实现换取低延迟，代价是分发形态变化，
   而不是把 WebRTC 塞进这个代码库。
4. 若坚持自研，先只做一件最有价值的前置验证：**用最小 WebRTC 客户端拉取 `lls`
   并确认音频编码**。这一步 1–2 人日即可完成，若确认是 Opus，方案成本直接上涨一档，
   应在此处决策而不是在实现到一半时才发现。

## 证据

`.research/p9a-probe-*.json`（三房间结构）、本文中 `lls` 端点的三次请求结果。
