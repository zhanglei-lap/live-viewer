# P8-A 番剧验证工具

这是独立可行性工具，不是 P8-B 的选集、收藏正式界面。0.8.0 稳定版与发行包保持原样。

## 启动与测试顺序

源码环境双击项目根目录的 `Validate-P8-A.cmd`。独立便携包启动 `BangumiValidation-P8A.exe`。默认 PotPlayer 路径为 `D:\potplayer\PotPlayerMini64.exe`，可在窗口修改。

1. 点击“查询匿名六集”，保留匿名结果。
2. 点击“B 站扫码登录”，在手机 App 扫码并确认。点击“查询会员六集”，核对账号大会员状态、每集整集资格和实际视频轨道。
3. 勾选“当前集完整观看”，点击“会员六集顺序实播”。第一集完整播放，其余每集连续五分钟，总共约 50 分钟。播放后自动检查前后拖动、暂停、签名地址失效后的 PGC 续期、继续播放和关闭清理。
4. 在官网用同账号、同网络核对整集与画质；在工具中确认内容、声音及音画同步，再勾选对应确认项。这些人工项目不会由程序自动判定。
5. 对同一集分别选择“请求普通 1080P”和“请求 1080P 高码率”，保持轨道下拉框为“最高实际可用 SDR”，各进行一次实播；也可直接选实际轨道。对比分辨率、轨道 ID、编码和标称带宽。同为 1920×1080 不代表高码率未生效，下载速度也不代表编码码率。
6. 点击“保存脱敏报告”。“取消并关闭专用播放器”会等待在途请求结束并清理，关闭工具亦会取消，不会因为迟到解析结果重新播放。

扫码登录只保留在本工具内存，退出后需要重新扫码；不读取浏览器 Cookie 数据库，也不读取或改写稳定版加密登录。报告默认在工具旁的 `data-p8a/p8a-report.json`，不包含 Cookie、二维码、UID、完整签名播放地址或字幕正文。专用播放器缓存与本轮配置退出后清理，可复用的专用引擎副本仍留在这个独立数据目录。

如果启动时带 `--auto-member --show-login --full-episode`，手机确认扫码后直接开始第 3 步。可以随时取消。

## 固定样本

采用用户指定的三部作品，日语原版第一季：

| 作品 | 季度 | 首集 | 另一集 |
|---|---|---|---|
| 孤独摇滚！ | [SS43164](https://www.bilibili.com/bangumi/play/ss43164) | [EP693247](https://www.bilibili.com/bangumi/play/ep693247)，第 1 集 | [EP706287](https://www.bilibili.com/bangumi/play/ep706287)，第 7 集 |
| 葬送的芙莉莲 | [SS46089](https://www.bilibili.com/bangumi/play/ss46089) | [EP779775](https://www.bilibili.com/bangumi/play/ep779775)，第 1 集 | [EP809819](https://www.bilibili.com/bangumi/play/ep809819)，第 15 集 |
| 间谍过家家 | [SS41410](https://www.bilibili.com/bangumi/play/ss41410) | [EP508404](https://www.bilibili.com/bangumi/play/ep508404)，第 1 集 | [EP516901](https://www.bilibili.com/bangumi/play/ep516901)，第 8 集 |

输入框亦接受其他 EP / SS 编号、EP 裸数字或完整 `/bangumi/play/ep…` / `ss…` 链接。SS 在独立探针中默认定位其首个正片；正式季度选集属于 P8-B。

## 网络条件

所有平台 API 和 CDN 请求固定使用 direct，忽略系统与环境代理，PotPlayer 仅访问本机 Range 服务。

`Validate-P8-A.cmd --proxy-configured` 会给**这个工具进程**设置 `127.0.0.1:9` 的不可达环境代理，然后进行实际 API / CDN 验证；不修改 Windows 系统代理，也不修改其他程序。报告记录 `trust_env=false` 和合并代理数量为 0。

direct 不能改变 VPN 的全局路由。只有实际在中国大陆网络、关闭 VPN / 全局隧道完成测试后，才能勾选对应网络确认项。未完成时保持“未验证”，不能由本机成功推断所有大陆网络都可用。

## 结果判定

- `whole`：接口没有试看 / 无权标记，媒体时长与 EP 元数据一致，存在普通 SDR DASH 视频与 AAC 音轨；仍需要实际播放验证。
- `preview_only`：仅获试看。即使接口 `timelength` 写着整集时长，也不会把 3 分钟试看作为整集。
- `entitlement_required` / `access_denied`：平台未授权。大会员状态有效也不能替代当前集的授权，另付费等具体权益需对照官网。
- `region_restricted`：平台明确给出地区限制；普通 DNS / TLS / CDN 失败不会归入地区限制。
- `protected_media`：返回 DRM / 加密媒体，不纳入本次普通 DASH 支持。
- `not_published` / `episode_missing` / `identity_mismatch`：尚无媒体、没有目标 EP，或 EP / cid / 季度不匹配；停止该次播放。
- `passed`：对应原生实播及控制、续期、清理检查通过；短时探测不能替代五分钟或完整单集的验收。

报告的 `gate` 单独检查六集会员实播、真实会员高清、完整一集、代理隔离与人工基准。未达标会列出缺项。4K / 60fps / 字幕没有实际样本时仍是未覆盖；本工具不提供 DRM 授权组件、永久下载、自动连播或跳片头片尾。

源码匿名自动测试示例：

```powershell
.venv-p0\Scripts\python.exe tools/validate-p8a.py --anonymous --proxy-configured --native-seconds 300 --data-dir .research/p8a-anonymous-native --report .research/p8a-anonymous-native.json
```

此命令不读取任何用户登录；匿名受限结果是调查证据，不是会员验收通过。会员验证只在用户扫码确认后的可见工具中进行。
