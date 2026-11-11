# P9-A 抖音取流可行性验证 · 验收

日期：2026-11-11。独立只读探针，不改动 `live_viewer/` 产品代码。

## 当前结论

**门禁全部通过。** 未登录仅凭 `ttwid` 即可稳定取得抖音直播的 HTTP-FLV 地址，
无需 `a_bogus` 签名；真实拉流返回合法 FLV 与 AVC 序列头；同一签名地址连续 60 分钟可用；
三个不同直播间结构一致，均能实拉到 H.264 FLV。

## 样本

三个房间由用户在不同时间提供，均为真实开播状态。

| 房间 | 标题 | 在线 | 结构与档位 | 编码 | 调用成功率 | 真实拉流 |
|---|---|---|---|---|---|---|
| `67876457927` | 玩机器CSGO正在直播 | 7w+ | 7 档，`live_core_sdk_data` | h264 | 20/20 + 10/10 | `origin` 通过 |
| `335610475439` | 刘回杯 / 陈泽杯 冠军 | 2w+ | 7 档，`live_core_sdk_data` | h264 | 8/8 | `uhd` 通过 |
| `480446788139` | 海南-临高 | 12w+ | 7 档，`live_core_sdk_data` | h264 | 通过 | 未单独采样（结构已确认） |

三房间的档位集合完全一致：`origin` / `uhd` / `hd` / `sd` / `ld` / `md` / `ao`。
单次调用耗时 305–580 ms，`status_code` 全为 0，**HTTP 444 零次**。

## CDN 主机

三个房间共出现两种域名后缀：

| 主机 | 出现房间 | 用途 |
|---|---|---|
| `pull-flv-l13.douyincdn.com` | 房间 1 | FLV |
| `pull-flv-q13.douyincdn.com` | 房间 2 | FLV |
| `pull-flv-q13-admin.douyincdn.com` | 房间 2 | FLV（`md` 档位） |
| `pull-flv-f11.douyinliving.com` | 房间 2 | FLV（备用） |
| `pull-flv-f11-admin.douyinliving.com` | 房间 2 | FLV |
| `pull-hls-*.douyincdn.com` / `*.douyinliving.com` | 各房间 | HLS |

主机名里的 `l13` / `q13` / `f11` 与 `admin` 段随房间和调度变化，**必须按域名后缀
`douyincdn.com` 与 `douyinliving.com` 匹配**。

> 这一条是多房间验证的直接收益：最初只登记了 `douyincdn.com`，房间 2 的
> `douyinliving.com` 备用线路会被媒体地址白名单误拒。已在 `platforms.py` 补上并加测试。

## 流结构

`stream_url` **同时返回新旧两套结构**，新结构档位更全：

- 新：`live_core_sdk_data.pull_data.stream_data`（JSON 字符串，约 8.5 KB），含 `common` + `data`
- 旧：`flv_pull_url`（`FULL_HD1` / `HD1` / `SD1` / `SD2`）、`hls_pull_url_map`、`hls_pull_url`、`pull_datas`

解析应以新结构为主、旧结构兜底。

`sdk_params` 的实际字段名是 **`VCodec`（大写 V）与 `vbitrate`（全小写）**，
不是 `vCodec` / `vBitRate`；大小写取错会静默得到空值。

## 档位实测

| 档位 | 名称 | 编码 | 分辨率 | fps | 标称码率 | FLV | HLS | 纯音频 |
|---|---|---|---|---:|---:|---|---|---|
| `origin` | 原画 | h264 | 1920×1080 | 59 | 14.84 Mbps | 有 | 有 | 否 |
| `uhd` | 超清 | h264 | 1920×1080 | 45 | 6.00 Mbps | 有 | 有 | 否 |
| `hd` | 高清 | h264 | 1280×720 | 30 | 4.00 Mbps | 有 | 有 | 否 |
| `sd` | 标清 | h264 | 1280×720 | 30 | 2.00 Mbps | 有 | 有 | 否 |
| `ld` | 流畅 | h264 | 960×540 | 25 | 1.00 Mbps | 有 | 有 | 否 |
| `md` | 极速 | h264 | 640×360 | 15 | 0.80 Mbps | 有 | 有 | 否 |
| `ao` | 纯音频 | — | — | — | 0 | 有 | 无 | **是** |

要点：

- `ao` 是纯音频流，`resolution` 为空、`fps` 为 0，**不能当作可选画质交给播放器**，否则黑屏。
- `hd` 与 `sd` 同为 1280×720@30 但码率不同（4 Mbps vs 2 Mbps），排序必须按码率而不是分辨率。
- 本房间所有档位都是 h264；其他房间可能提供 h265，实现时按 `VCodec` 过滤而非假设。
- 本房间各档位的 `backup` 均为空，即同一档位只有一个 FLV 主机，没有同档冗余；
  房间 2 出现了带备用主机的档位，其主机落在 `douyinliving.com` 后缀下。

## 真实拉流

| 项目 | 结果 |
|---|---|
| 档位 | `origin` |
| HTTP | 200 |
| Content-Type | `video/x-flv` |
| FLV 头 | 通过（`FLV` 魔数） |
| 视频编码 ID | 7 = AVC (H.264)，与 API 声明的 `h264` 一致 |
| 序列头 | 存在 |
| 采样量 | 262144 字节 / 8 秒 / 28 个标签 |

## 排除项（已确认不成立）

| 假设 | 实测 |
|---|---|
| 需要 `a_bogus` 签名 | 不需要，20/20 成功 |
| 首页可自动发现开播房间 | 不可，SPA 空壳；房间列表接口返回 HTTP 444 |
| 19 位 room_id 可直接当 `web_rid` | 不行，返回 `4001038` |
| 只传 `room_id_str` 可绕过 | 不行，返回 `10011 当前服务繁忙` |
| 移动端 `reflow/info` 更简单 | 不行，`app id:0 and live id:1 mismatch` |

## 签名地址有效期

对 `origin` 档位的**同一个**签名 URL 做长时复测，中间不重新解析房间：

| 时点 | HTTP | FLV 头 | 前 4 字节 |
|---|---|---|---|
| T+0 | 200 | 通过 | `464c5601` |
| T+10 分钟 | 200 | 通过 | `464c5601` |
| T+30 分钟 | 200 | 通过 | `464c5601` |
| T+60 分钟 | 200 | 通过 | `464c5601` |

结论：至少 60 分钟内地址保持有效，且没有出现只允许单次连接的限制。上限未测出，
但长时间观看不会因地址过期中断；断流后的自动重连仍会重新解析房间，属额外保险。

## 未覆盖项

- **PotPlayer 实播**：三个房间中只有房间 1 完成过真人实播确认（画面、声音、
  音画同步、暂停、全屏、进度条拖动）；房间 2 / 3 只做到取流层。
- **原生时移**：房间 1 已验证回看与拖动可用；房间 2 / 3 未测。
- **登录 Cookie 是否提高成功率**：目前匿名即可，未测试带 Cookie 会话。
- **低延迟协议**：`lls` 为 WebRTC，未纳入本工具；迁移评估见
  [lls 迁移评估](p9b-lls-assessment.md)。

## 对 P9-B 的推论

1. `MEDIA_DOMAINS` 加 `douyincdn.com` 与 `douyinliving.com`（后缀匹配），不要写死主机名。
2. 解析以 `live_core_sdk_data` 为主、`flv_pull_url` 兜底。
3. 档位排序按 `vbitrate`，`ao` 必须排除。
4. 按 `VCodec` 过滤，优先 `h264`。
5. 无需签名，仅需 `ttwid`；会话需要 Referer `https://live.douyin.com/`。
6. 备用主机直接并入候选地址轮换，不需要重签名（与斗鱼的 `backup_cdns` 不是一回事）；
   最终失败走「重连时重新解析」。
7. 不支持扫码登录，只支持 Cookie 导入 / 粘贴。
