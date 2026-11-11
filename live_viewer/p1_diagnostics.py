"""Small frozen-app checks using isolated/synthetic data only."""
from __future__ import annotations

from .core import ViewerError


def check_p1(app):
    controller, events = app.controller, app.activity
    request = {"platform": "douyu", "room_input": "6657", "requested": None,
               "room_name": "自检房间", "session": "匿名", "backend": "potplayer",
               "network_mode": "direct", "low_latency": True}
    token = controller.begin(request)
    controller.fail(token, ViewerError("模拟证书连接失败。", code="network.tls_failed"))
    first_notice = events.notice()
    events.emit("account.imported", "模拟登录成功。", source="account", target="bilibili")
    checks = {"login_preserves_failure": events.notice() == first_notice}
    app._drain_activity()
    app.root.update_idletasks()
    checks["notice_outside_scroll"] = app.notice_frame.winfo_manager() == "pack" and app.notice_frame.master != app.canvas
    retry = controller.begin(request, automatic=True)
    controller.fail(retry, ViewerError("模拟重试仍然失败。"), recovering=True)
    checks["retry_same_session_new_generation"] = retry.session_id == token.session_id and retry.generation != token.generation
    checks["retry_no_duplicate_popup"] = events.drain()[1] == []
    controller.stop()
    controller.fail(retry, ViewerError("迟到错误，不应显示。"))
    checks["stop_rejects_stale_failure"] = controller.state == "stopped" and events.notice() is None
    events.emit("diagnostic.redaction", "https://example.invalid/live?token=SYNTHETIC_URL_SECRET\n"
                "Cookie: test=SYNTHETIC_COOKIE_SECRET", details={"qrcode_key": "SYNTHETIC_QR_SECRET"})
    exported = app.store.directory / "p1-events-export.json"
    events.store.export(exported)
    rows = events.store.read()
    text = exported.read_text(encoding="utf-8")
    checks["safe_durable_export"] = all(secret not in text for secret in
                                             ("SYNTHETIC_URL_SECRET", "SYNTHETIC_COOKIE_SECRET", "SYNTHETIC_QR_SECRET"))
    checks["real_schema_and_disk_log"] = bool(rows) and all(r["schema_version"] == 1 and r["demo"] is False for r in rows)
    checks["all_attempts_logged"] = any(r["code"] == "playback.retry_started" for r in rows)
    app._drain_activity()
    return {"success": all(checks.values()), "checks": checks}
