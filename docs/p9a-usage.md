# P9-A 抖音取流可行性验证 · 工具说明

只读探针，用于在改动产品代码之前，用真实网络证明抖音直播可以稳定取流。
工具不会写入用户数据、不会读取正常账号凭据，证据文件不含签名媒体地址。

## 运行

```powershell
# 基本用法：针对一个正在直播的房间
.venv-p0\Scripts\python.exe tools\probe-douyin.py --room https://live.douyin.com/67876457927

# 多个房间 + 完整调用次数 + 地址有效期复测
.venv-p0\Scripts\python.exe tools\probe-douyin.py `
    --room 67876457927 --room 123456789 --attempts 20 --expiry 10,30

# 走系统 / 环境代理
.venv-p0\Scripts\python.exe tools\probe-douyin.py --room 67876457927 --network-mode system
```

`--room` 接受 `live.douyin.com/<数字>` 链接或 12–15 位房间号。

注意：抖音首页是 SPA 空壳，房间列表由签名接口提供并返回 HTTP 444，
因此**无法自动发现正在直播的房间**，必须用 `--room` 指定。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--room` | 无 | 房间链接或短号，可重复 |
| `--attempts` | 20 | 取流接口连续调用次数，用于统计成功率与 HTTP 444 |
| `--seconds` | 8 | 每个地址的真实拉流采样秒数 |
| `--expiry` | 空 | 有效期复测间隔（分钟，逗号分隔） |
| `--network-mode` | direct | `direct` 或 `system`，与产品设置一致 |
| `--output` | `.research/p9a-probe-<时间戳>.json` | 证据输出路径 |

## 输出

证据写入 `.research/`（已被 `.gitignore` 排除，不进入发行包）。写入前由 `strip_urls()`
移除所有签名媒体地址，只保留 host、档位、编码、码率等元数据。

一次运行依次输出六步：

1. `ttwid` 获取（取流接口的唯一前提）
2. 候选房间（不指定 `--room` 时尝试自动发现）
3. 取流接口连续调用成功率与 `status_code` 分布
4. 房间状态与 `stream_url` 结构解析（新结构优先，旧结构兜底）
5. 真实拉流采样：FLV 头、视频编码 ID、序列头
6. 签名地址有效期复测

## 源码依赖

工具复用产品的网络栈，因此默认代理策略、IPv4 优先与证书校验行为与正式程序一致：

```python
from live_viewer.network import configure_session
```

## 已知结论

见 [P9-A 验收](p9a-acceptance.md)。
