"""平台能力注册表：一门式声明每个直播平台的差异，取代散落的二元 if/else。

设计约束：
  - 本模块只描述“平台是什么”，不发起请求、不依赖 core / settings，避免循环导入。
  - core.py 继续导出 REFERERS / COOKIE_DOMAINS / MEDIA_DOMAINS 三个旧名字（由本表派生），
    以兼容冻结的 Tk 前端与现有测试。
  - 新增平台时只允许改本表 + 在 PlatformClient 上补一个 resolve_method 指向的方法；
    不允许再写 `platform == "x" if ... else ...` 这类二元分派。

字段语义见 PlatformSpec 文档字符串。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSpec:
    """一个直播平台的静态能力描述。

    key                  内部标识，写入设置、事件与报告
    caption              界面显示名，也是 PLATFORMS 字典的键
    referer              平台接口与媒体上游请求使用的 Referer
    page_hosts           允许粘贴的直播间链接域名（belongs_to 语义，含子域）
    cookie_domains       Cookie 作用域；第一个同时用于写入设备标识
    media_domains        媒体地址白名单，validate_media_url 据此拒绝跨站地址
    resolve_method       PlatformClient 上取流方法名（接收 room, requested）
    preferred_quality    “原画优先”时请求的档位值
    import_credentials   导入 / 粘贴 Cookie 时必需的 cookie 名；空表示不作校验
    paste_alias          允许只粘贴该 cookie 的值；空表示必须给完整 name=value
    qr_login             是否支持扫码登录
    qr_credentials       扫码成功后必需的 cookie 名
    qr_cancel_url        扫码未完成时用于作废二维码的接口；空表示无需清理
    verify_login         是否存在可用的登录状态校验接口
    unverified_message   verify_login 为 False 时展示给用户的说明
    device_cookie        平台设备标识 cookie 名；空表示不需要
    resign_backups       备用线路是否需要在同一会话内重新签名
    topic_query_room     房间号是否可能位于 /topic/?rid= 查询参数中
    max_room_digits      数字房间号允许的最大位数
    """

    key: str
    caption: str
    referer: str
    page_hosts: tuple[str, ...]
    cookie_domains: tuple[str, ...]
    media_domains: tuple[str, ...]
    resolve_method: str
    preferred_quality: int | None
    import_credentials: tuple[str, ...] = ()
    paste_alias: str = ""
    qr_login: bool = True
    qr_credentials: tuple[str, ...] = ()
    qr_cancel_url: str = ""
    verify_login: bool = False
    unverified_message: str = ""
    device_cookie: str = ""
    resign_backups: bool = False
    topic_query_room: bool = False
    max_room_digits: int = 15


# 顺序即界面中的展示顺序。
PLATFORM_SPECS: dict[str, PlatformSpec] = {
    "bilibili": PlatformSpec(
        key="bilibili",
        caption="B 站",
        referer="https://live.bilibili.com/",
        page_hosts=("live.bilibili.com",),
        cookie_domains=("bilibili.com",),
        media_domains=("bilivideo.com", "bilivideo.cn"),
        resolve_method="_bilibili",
        preferred_quality=10000,
        import_credentials=("SESSDATA",),
        paste_alias="SESSDATA",
        qr_login=True,
        qr_credentials=("SESSDATA",),
        verify_login=True,
        max_room_digits=15,
    ),
    "douyu": PlatformSpec(
        key="douyu",
        caption="斗鱼",
        referer="https://www.douyu.com/",
        page_hosts=("douyu.com",),
        cookie_domains=("douyu.com",),
        media_domains=("douyucdn.cn", "douyucdn2.cn", "douyucdn.com", "douyucdn2.com",
                       "douyu.com", "livehwc4.com", "edgesrv.com"),
        resolve_method="_douyu",
        preferred_quality=0,
        import_credentials=(),
        paste_alias="",
        qr_login=True,
        qr_credentials=("acf_auth",),
        qr_cancel_url="https://passport.douyu.com/scan/deleteCode",
        verify_login=False,
        unverified_message="斗鱼 Cookie 已导入；账号有效性尚未单独验证",
        device_cookie="dy_did",
        resign_backups=True,
        topic_query_room=True,
        max_room_digits=15,
    ),
}


def spec_for(platform: str) -> PlatformSpec | None:
    return PLATFORM_SPECS.get(platform)


def require_spec(platform: str) -> PlatformSpec:
    """返回平台描述，非注册平台抛统一的“不支持的平台”。"""
    from .core import ViewerError  # 延迟导入，避免模块级循环依赖

    spec = PLATFORM_SPECS.get(platform)
    if spec is None:
        raise ViewerError("不支持的平台。")
    return spec
