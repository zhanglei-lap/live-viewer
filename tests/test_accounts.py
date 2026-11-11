import threading
import unittest
from unittest.mock import Mock, patch
from live_viewer.accounts import CredentialWriter, verify_login
from live_viewer.core import ViewerError, cookies_from_text


class AccountTests(unittest.TestCase):
    def test_verification_closes_client_and_returns_unverified_douyu_honestly(self):
        client = Mock()
        client.login_status.return_value = {"verified": None, "message": "尚未独立验证"}
        with patch("live_viewer.accounts.PlatformClient", return_value=client):
            self.assertIsNone(verify_login("douyu", None)["verified"])
        client.close.assert_called_once()

    def test_expired_login_is_typed_warning_and_client_is_closed(self):
        client = Mock()
        client.login_status.return_value = {"verified": False, "message": "已过期"}
        with patch("live_viewer.accounts.PlatformClient", return_value=client):
            with self.assertRaises(ViewerError) as error:
                verify_login("bilibili", None)
        self.assertEqual(error.exception.code, "account.expired")
        self.assertEqual(error.exception.level, "WARN")
        client.close.assert_called_once()

    def test_queued_save_is_obsolete_after_clear_and_does_not_resurrect_login(self):
        store = Mock()
        writer = CredentialWriter(store)
        old = writer.request("bilibili", cookies_from_text("SESSDATA=synthetic", "bilibili"))
        clear = writer.request("bilibili")
        self.assertTrue(writer.apply(clear))
        self.assertFalse(writer.apply(old))
        store.save.assert_not_called()
        store.delete.assert_called_once_with("bilibili")
        self.assertTrue(old.done.is_set())

    def test_clear_after_an_inflight_save_always_deletes_the_saved_login(self):
        entered, release, removed = threading.Event(), threading.Event(), threading.Event()
        store = Mock()
        def save(*_):
            entered.set()
            release.wait(2)
        store.save.side_effect = save
        store.delete.side_effect = lambda _: removed.set()
        writer = CredentialWriter(store)
        save_request = writer.request("douyu", cookies_from_text("acf_auth=synthetic", "douyu"))
        saving = threading.Thread(target=writer.apply, args=(save_request,))
        saving.start()
        self.assertTrue(entered.wait(1))
        clear_request = writer.request("douyu")
        clearing = threading.Thread(target=writer.apply, args=(clear_request,))
        clearing.start()
        self.assertFalse(removed.wait(.04))
        release.set()
        saving.join(2);clearing.join(2)
        self.assertTrue(removed.is_set())
        self.assertTrue(clear_request.done.is_set())
        self.assertIsNone(save_request.jar)

    def test_failed_write_releases_secret_and_signals_shutdown(self):
        store = Mock()
        store.save.side_effect = ViewerError("保存失败")
        writer = CredentialWriter(store)
        request = writer.request("douyu", cookies_from_text("acf_auth=synthetic-secret", "douyu"))
        self.assertNotIn("synthetic-secret", repr(request))
        with self.assertRaises(ViewerError):
            writer.apply(request)
        self.assertIsNone(request.jar)
        self.assertTrue(request.done.is_set())
        writer.close()
