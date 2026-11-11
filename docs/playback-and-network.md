# 取流、播放器与直连说明（0.5.0）

## 整体链路

```text
用户电脑 → 平台房间 / 播放接口 → 平台 CDN（HTTPS-FLV）
          → 本机 127.0.0.1 随机端口转发 → mpv / PotPlayer
```

工具在运行它的电脑上完成所有请求，不连接本项目的远程解析服务器，不上传 Cookie，没有内置境外中转。国内电脑直连时，接口和 CDN 连接由该电脑的网络、DNS 和平台分配决定；本机转发不会改变访问平台的出口地域。VPN 全局隧道由操作系统控制，应用内直连不绕过操作系统路由。

## B 站

1. 从输入链接提取数字房间号，忽略 session_id 等跟踪参数。
2. GET `https://api.live.bilibili.com/room/v1/Room/room_init?id=<房间号>`，取真实 room_id、live_status。未开播与连接失败分别提示。
3. GET `https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo`，参数为 room_id、qn、protocol=0、format=0、codec=0、platform=web。
4. 在返回中选择 http_stream → flv → avc，拼接 `url_info.host + base_url + url_info.extra`。请求“原画优先”使用 qn=10000，实际档位以 current_qn 为准。accept_qn 只表示平台列出的档位，不证明本次拿到原画。
5. 登录会话携带本机 Cookie，并通过 `https://api.bilibili.com/x/web-interface/nav` 验证登录。匿名和登录走同一解析逻辑，平台决定是否降档。

## 斗鱼

1. GET 对应房间网页，只解析主房间的 JSON / HTML 数据，不执行网页脚本。短号 6657 本次解析为真实房间 6979222，不能直接用短号签名。
2. GET `https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did=<设备标识>`。从 HTTP Date 取得服务器时间，避免本机时钟偏差。
3. 按接口返回的 enc_time 次数对 rand_str 与 key 做 MD5 迭代；最终将结果与 key、真实房间号、服务器时间按 is_special 的规则生成 auth。MD5 在这里是平台签名协议，不用于保存登录密码。
4. POST `https://www.douyu.com/lapi/live/getH5PlayV1/<真实房间号>`，提交 enc_data、tt、did、auth、cdn、rate、hevc=0、fa=0、ive=0。组合返回的 rtmp_url 和 rtmp_live。字段名称虽然带 rtmp，当前选择的是 HTTPS-FLV。
5. “原画优先”请求 rate=0，实际以返回的 rate 为准。主 CDN 失败后，在同一会话中为平台提供的备用 CDN 重新签名，最多额外尝试三条，拒绝静默降档。每次重新连接取得新签名地址。

这些是平台当前网页使用的接口适配，平台变更后可能需要更新软件。工具不会提供会员权限、增加源流码率或生成平台未提供的画质。

## 本机转发与解码

转发保留 FLV 的原始字节，既不重编码，也不将视频保存为本地录像。上游保持 HTTPS 和证书校验，对每个重定向检查平台 CDN 域名。播放器只接收本机地址，不接收平台 Cookie 或签名参数。

0.5.0 使用随包的官方 Windows x64 LGPL libmpv，由 mpv 创建自己的播放窗口。视频输出使用 D3D11，默认尝试安全的硬件解码；暂停、音量、静音、窗口缩放和全屏由成熟引擎及其原生 OSC 控件处理。全屏没有 Tk 的标题区、按钮区或外框。

本项目提供一个直播补充脚本：显示本次观看的实际 seekable-ranges，允许点击 / 拖动缓存条、左右键在缓存内回看，L 或“回到直播”重新连接最新直播。前向缓存目标约 2 秒，后向缓存内存上限 32 MiB；可回看长度取决于码率与关键帧。清晰度切换、断流和重连会清空旧缓存，不提供整场回放或跨会话缓存。缓存前沿差不等于画面相对主播的绝对延迟。

关闭播放器会停止该会话，不触发重连；暂停抑制重连，继续播放重新取最新直播；断流恢复使用 replace 替换唯一播放项。PotPlayer 仍可选择，但它自身的代理、旧播放列表和弹窗由 PotPlayer 控制。

## 网络模式与大陆排查

“账号与播放器 → 网络连接”默认 **直连**：requests 会话 trust_env=False，不继承 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 和 Windows 代理；扫码的 Windows curl TLS 回退也遵循相同策略。可选择“系统 / 环境代理”以保留需要代理的网络环境。该选项影响下一次播放、登录和检查，不修改系统设置。

直连优先尝试 DNS 返回的 IPv4 地址，失败后仍尝试 IPv6；系统 / 环境代理模式保留系统地址排序。本次本机检查发现平台 DNS 将多个 IPv6 地址排在前面，而 IPv6 TCP 连接超时、IPv4 约 20 毫秒连通。调整仅作用于本工具的 TCP 地址顺序，保留原域名、HTTPS SNI 和证书校验，不修改系统 DNS、代理或路由，也不指定境外服务器。扫码的 Windows curl 回退同样优先 IPv4，网络连接失败时再使用默认地址选择。

内置 mpv 的 127.0.0.1 连接始终绕过环境代理。随包引擎默认的 libcurl 后端会继承代理；这里仅对本机媒体连接选择 FFmpeg 网络后端，并显式关闭代理继承。本机已通过失效环境代理测试。平台请求仍保持用户选定的网络模式。

朋友复测时关闭 VPN，选择“直连”，分别输入斗鱼 6657 和 B 站 1883358196，点击“检查当前直播间连接”，随后在报告页导出结果。检查分别验证房间 / 播放接口和 CDN 的 FLV 读取；失败提示包括公共域名、DNS / TLS / 代理 / 超时类别，不包含完整签名地址、Cookie 或代理凭据。未开播只说明房间状态，不能作为 CDN 可播放的证明。

本机直连已读到这两个房间，不等于已经验证中国大陆每个地区和运营商。若朋友直连仍失败，需要其失败阶段报告和同网络官网播放情况，才能判断是否还需要平台适配、线路回退或网络修正。

参考：[mpv OSC 与缓存文档](https://mpv.io/manual/stable/)、[mpv 当前网络后端文档](https://mpv.io/manual/master/)、[Requests 代理文档](https://requests.readthedocs.io/en/latest/user/advanced/#proxies)。实现入口：core.py、network.py、relay.py、mpv_player.py 与 assets/live-controls.lua。
