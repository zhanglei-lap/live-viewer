"""平台注册表契约测试：锁定 P9-B-1 重构后的声明式结构，防止退回二元 if/else。"""
import unittest
from unittest.mock import patch

from live_viewer.core import (
    COOKIE_DOMAINS, MEDIA_DOMAINS, REFERERS, PlatformClient, ViewerError,
    cookies_from_text, room_number,
)
from live_viewer.platforms import PLATFORM_SPECS, PlatformSpec, require_spec
from live_viewer.settings import PLATFORMS


class RegistryContractTests(unittest.TestCase):
    def test_every_spec_is_published_to_the_interface_exactly_once(self):
        self.assertEqual(list(PLATFORMS.values()), list(PLATFORM_SPECS))
        for caption, key in PLATFORMS.items():
            self.assertEqual(PLATFORM_SPECS[key].caption, caption)
        captions = [spec.caption for spec in PLATFORM_SPECS.values()]
        self.assertEqual(len(captions), len(set(captions)), "显示名必须唯一，否则界面下拉会重复")

    def test_legacy_maps_are_derived_from_the_registry(self):
        self.assertEqual(REFERERS, {k: v.referer for k, v in PLATFORM_SPECS.items()})
        self.assertEqual(COOKIE_DOMAINS, {k: v.cookie_domains for k, v in PLATFORM_SPECS.items()})
        self.assertEqual(MEDIA_DOMAINS, {k: v.media_domains for k, v in PLATFORM_SPECS.items()})

    def test_every_spec_declares_a_real_resolver(self):
        for key, spec in PLATFORM_SPECS.items():
            self.assertTrue(callable(getattr(PlatformClient, spec.resolve_method, None)),
                            f"{key} 的 resolve_method={spec.resolve_method!r} 在 PlatformClient 上不存在")

    def test_every_spec_declares_page_hosts_and_media_domains(self):
        for key, spec in PLATFORM_SPECS.items():
            self.assertTrue(spec.page_hosts, f"{key} 缺少 page_hosts")
            self.assertTrue(spec.media_domains, f"{key} 缺少 media_domains")
            self.assertTrue(spec.cookie_domains, f"{key} 缺少 cookie_domains")

    def test_qr_platforms_declare_required_credentials(self):
        for key, spec in PLATFORM_SPECS.items():
            if spec.qr_login:
                self.assertTrue(spec.qr_credentials, f"{key} 支持扫码却没有声明 qr_credentials")

    def test_unknown_platform_is_rejected_by_every_entry_point(self):
        for call in (lambda: require_spec("nope"),
                     lambda: PlatformClient("nope"),
                     lambda: room_number("nope", "123"),
                     lambda: cookies_from_text("a=b", "nope")):
            with self.assertRaises(ViewerError) as error:
                call()
            self.assertEqual(str(error.exception), "不支持的平台。")


class DeclarativeBehaviourTests(unittest.TestCase):
    """用一个临时平台证明行为由声明决定，而不是由写死的平台名决定。"""

    def setUp(self):
        self.added = []
        self.saved_plans = None

    def tearDown(self):
        for key in self.added:
            PLATFORM_SPECS.pop(key, None)

    def register(self, **overrides):
        values = {
            "key": "faketest", "caption": "测试平台", "referer": "https://example.test/",
            "page_hosts": ("example.test",), "cookie_domains": ("example.test",),
            "media_domains": ("cdn.test",), "resolve_method": "_bilibili",
            "preferred_quality": 0,
        }
        values.update(overrides)
        spec = PlatformSpec(**values)
        PLATFORM_SPECS[spec.key] = spec
        self.added.append(spec.key)
        return spec

    def test_qr_login_false_is_refused_before_any_http_client_is_built(self):
        self.register(qr_login=False)
        from live_viewer.qr_login import QrLogin
        with patch("live_viewer.qr_login.LoginHttp") as http:
            with self.assertRaises(ViewerError) as error:
                QrLogin("faketest")
        self.assertEqual(error.exception.code, "account.no_qr")
        self.assertEqual(error.exception.level, "WARN")
        http.assert_not_called()

    def test_missing_resolver_reports_instead_of_falling_back_to_another_platform(self):
        self.register(resolve_method="_does_not_exist")
        client = PlatformClient("faketest")
        try:
            with self.assertRaises(ViewerError) as error:
                client.resolve("123")
        finally:
            client.close()
        self.assertEqual(error.exception.code, "platform.unimplemented")

    def test_paste_alias_is_optional_and_platform_declared(self):
        self.register(paste_alias="")
        with self.assertRaises(ViewerError):
            cookies_from_text("barevalue", "faketest")
        jar = cookies_from_text("token=value", "faketest")
        self.assertEqual(jar.get("token"), "value")

    def test_import_credentials_are_enforced_only_when_declared(self):
        self.register(import_credentials=("need",))
        with self.assertRaises(ViewerError) as error:
            cookies_from_text("other=1", "faketest")
        self.assertIn("need", str(error.exception))
        self.assertIsNotNone(cookies_from_text("need=ok", "faketest").get("need"))

    def test_room_digit_limit_comes_from_the_spec(self):
        self.register(max_room_digits=9)
        self.assertEqual(room_number("faketest", "123456789"), "123456789")
        with self.assertRaises(ViewerError):
            room_number("faketest", "1234567890")
        self.assertEqual(room_number("faketest", "https://example.test/987654"), "987654")

    def test_topic_query_room_is_opt_in(self):
        self.register(topic_query_room=True)
        self.assertEqual(
            room_number("faketest", "https://example.test/topic/?rid=456"), "456")
        PLATFORM_SPECS["faketest"] = PlatformSpec(
            key="faketest", caption="测试平台", referer="https://example.test/",
            page_hosts=("example.test",), cookie_domains=("example.test",),
            media_domains=("cdn.test",), resolve_method="_bilibili", preferred_quality=0)
        with self.assertRaises(ViewerError):
            room_number("faketest", "https://example.test/topic/?rid=456")


class ExistingPlatformTests(unittest.TestCase):
    def test_douyu_topic_links_still_resolve(self):
        self.assertEqual(room_number("douyu", "https://www.douyu.com/topic/abc?rid=6657"), "6657")
        self.assertEqual(room_number("douyu", "https://www.douyu.com/6657"), "6657")

    def test_bilibili_still_only_accepts_its_own_live_host(self):
        self.assertEqual(room_number("bilibili", "https://live.bilibili.com/35"), "35")
        with self.assertRaises(ViewerError):
            room_number("bilibili", "https://www.bilibili.com/35")

    def test_douyu_keeps_its_unverified_login_message(self):
        client = PlatformClient("douyu")
        try:
            status = client.login_status()
        finally:
            client.close()
        self.assertIsNone(status["verified"])
        self.assertEqual(status["message"], "斗鱼 Cookie 已导入；账号有效性尚未单独验证")


if __name__ == "__main__":
    unittest.main()
