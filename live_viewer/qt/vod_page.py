"""Bilibili video page; async availability lookup, native playback, saved video IDs."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLineEdit, QListWidget,
                              QListWidgetItem, QSizePolicy, QVBoxLayout, QWidget)
from ..core import ViewerError
from ..events import safe_text
from ..vod import VodClient
from ..vod_state import resume_position
from .widgets import button, card, row, scrolling_page, text


class VodPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.source = self.video = None
        self.loaded_input = ""
        self.generation = 0
        self.last_keys = None
        self.saved_name = None
        self.expected_saved_cid = None
        self.auto_play_pending = False
        layout = QHBoxLayout(self);layout.setContentsMargins(0, 0, 0, 0);layout.setSpacing(16)
        saved, left = card("我的视频", "收藏以 BV 与分 P 的内容编号索引")
        saved.setMinimumWidth(190);saved.setMaximumWidth(250)
        self.search = QLineEdit();self.search.setPlaceholderText("搜索名称或 BV 编号")
        self.search.textChanged.connect(self.refresh_saved)
        self.saved_mode = QComboBox();self.saved_mode.addItems(["收藏", "最近观看"])
        self.saved_mode.currentIndexChanged.connect(self.refresh_saved)
        self.saved = QListWidget();self.saved.setWordWrap(True)
        self.saved.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.saved.currentItemChanged.connect(self.select_saved)
        self.saved.itemDoubleClicked.connect(lambda _: self.lookup(auto_play=True))
        left.addWidget(self.search);left.addWidget(self.saved_mode);left.addWidget(self.saved, 1)
        left.addLayout(row(button("改名", self.rename), button("删除", self.remove)))
        layout.addWidget(saved)
        column = QWidget()
        fixed = QVBoxLayout(column);fixed.setContentsMargins(0, 0, 0, 0);fixed.setSpacing(16)
        scroll, right = scrolling_page()
        entry, form = card("B 站视频", "输入普通视频链接或 BV / AV 编号，选择分 P 和实际可用画质。")
        self.input = QLineEdit();self.input.setPlaceholderText("BV17x411w7KC 或 https://www.bilibili.com/video/…?p=2")
        self.input.textChanged.connect(self.invalidate_input)
        self.input.returnPressed.connect(self.lookup)
        self.fetch = button("获取视频", self.lookup, "Primary")
        form.addWidget(text("视频链接或编号", "FieldLabel"));form.addLayout(row(self.input, self.fetch))
        self.title = text("尚未获取视频", "CardTitle", True)
        self.detail = text("", "Caption", True)
        form.addWidget(self.title);form.addWidget(self.detail)
        self.parts = QComboBox();self.parts.currentIndexChanged.connect(self.part_changed)
        self.parts.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        form.addWidget(text("分 P", "FieldLabel"));form.addWidget(self.parts)
        self.mode = QComboBox();self.mode.addItem("匿名", "匿名");self.mode.addItem("使用已登录 B 站账号", "导入 Cookie")
        self.mode.currentIndexChanged.connect(self.invalidate_tracks)
        form.addWidget(text("账号", "FieldLabel"));form.addWidget(self.mode)
        self.videos, self.audios = QComboBox(), QComboBox()
        for box in (self.videos, self.audios):
            box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        form.addWidget(text("视频画质", "FieldLabel"));form.addWidget(self.videos)
        form.addWidget(text("音频音质", "FieldLabel"));form.addWidget(self.audios)
        form.addWidget(text("支持 SDR AVC / HEVC / AV1 与 AAC，画质以当前账号实际返回为准。", "Caption", True))
        self.subtitle_box = QComboBox();self.subtitle_box.addItem("不加载字幕", None)
        self.subtitle_box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.subtitle_status = text("字幕选择在下次播放生效。", "Caption", True)
        self.subtitle_button = button("获取字幕", self.load_subtitles)
        form.addWidget(text("平台字幕", "FieldLabel"));form.addLayout(row(self.subtitle_box, self.subtitle_button));form.addWidget(self.subtitle_status)
        self.name = QLineEdit();self.name.setMaxLength(80);self.name.setPlaceholderText("收藏名称，可自行修改")
        form.addWidget(text("收藏名称", "FieldLabel"));form.addLayout(row(self.name, button("收藏当前分 P", self.save)))
        right.addWidget(entry)
        playback, body = card()
        self.status = text("准备播放视频", "CardTitle", True)
        self.status_detail = text("", "Caption", True)
        self.play_button = button("播放视频", self.play, "Primary")
        self.pause_button = button("暂停", window.pause)
        self.stop_button = button("停止", window.stop)
        self.retry_button = button("恢复当前视频", lambda: (window.runtime.reconnect(), window.tick()))
        body.addLayout(row(self.status, self.retry_button));body.addWidget(self.status_detail)
        body.addLayout(row(self.play_button, self.pause_button, self.stop_button))
        self.resume_box = QCheckBox("从上次位置继续")
        self.resume_box.setChecked(window.config.get("vod_resume", True))
        self.resume_box.toggled.connect(self.save_resume_option)
        body.addWidget(self.resume_box)
        body.addWidget(text("在 PotPlayer 进度条拖动；暂停后可从原位置继续。", "Caption", True))
        right.addStretch()
        fixed.addWidget(scroll, 1);fixed.addWidget(playback)
        layout.addWidget(column, 1)
        self.refresh_saved()
        previous = window.config.get("last_video")
        if previous:
            self.select_record(previous)

    def invalidate_input(self):
        self.generation += 1
        self.auto_play_pending = False
        self.window.runtime.jobs.cancel("vod.lookup")
        self.source = self.video = None
        self.saved_name = None
        self.expected_saved_cid = None
        for box in (self.parts, self.videos, self.audios):
            box.blockSignals(True);box.clear();box.blockSignals(False)
        self.title.setText("尚未获取视频");self.detail.clear()
        self.name.clear()
        self.clear_subtitles()

    def invalidate_tracks(self):
        self.generation += 1
        self.auto_play_pending = False
        self.window.runtime.jobs.cancel("vod.lookup")
        self.source = None
        self.videos.clear();self.audios.clear()
        self.clear_subtitles()

    def part_changed(self):
        self.invalidate_tracks()
        if self.video is not None and self.parts.currentData() is not None:
            self.lookup()

    def lookup(self, *, auto_play=False):
        window = self.window
        value = self.input.text().strip()
        mode, route = self.mode.currentData(), window.config["network_mode"]
        jar = window.cookies.get("bilibili") if mode == "导入 Cookie" else None
        if mode == "导入 Cookie" and jar is None:
            window.runtime.error(ViewerError("请先到账号页登录 B 站，或选择匿名。", code="account.missing", level="WARN"), scope="vod.lookup")
            return
        page = self.parts.currentData() if self.video is not None and self.loaded_input == value else None
        self.generation += 1;generation = self.generation
        self.auto_play_pending = auto_play
        self.source = None;self.videos.clear();self.audios.clear()
        expected_cid = self.expected_saved_cid
        window.activity.clear("vod.lookup")
        window.activity.emit("vod.lookup_started", "正在获取视频信息与可用画质。", source="vod")
        def work():
            client = VodClient(jar, network_mode=route)
            try:
                result = client.resolve(value, page)
                if expected_cid is not None and result.page["cid"] != expected_cid:
                    moved = next((p for p in result.video.pages if p["cid"] == expected_cid), None)
                    if moved is None:
                        raise ViewerError("收藏的分 P 已变更或删除，请重新输入 BV 编号选择新的分 P。", code="vod.saved_part_changed", level="WARN")
                    result = client.resolve(value, moved["page"])
                return result
            finally:client.close()
        def done(source):
            self.auto_play_pending = False
            self.set_source(source, value)
            window.activity.clear("vod.lookup")
            window.activity.emit("vod.lookup_completed", f"已获取 {source.video.title} · P{source.page['page']}，可用画质 {len(source.videos)} 档。", source="vod", target=source.video.bvid)
            if auto_play:self.play()
        def failed(exc):
            self.auto_play_pending = False
            window.runtime.error(exc, scope="vod.lookup")
        window.runtime.jobs.submit("vod.lookup", work, done, failed,
            valid=lambda: not window.closing and generation == self.generation and route == window.config["network_mode"])

    def set_source(self, source, value=None):
        self.clear_subtitles()
        self.source, self.video = source, source.video
        self.loaded_input = self.input.text().strip() if value is None else value
        self.title.setText(safe_text(source.video.title, 300))
        self.detail.setText(f"{source.video.bvid} · {source.video.owner} · {len(source.video.pages)} 个分 P")
        self.parts.blockSignals(True);self.parts.clear()
        for part in source.video.pages:
            self.parts.addItem(f"P{part['page']} · {part['title']} · {part['duration']} 秒", part["page"])
        self.parts.setCurrentIndex(self.parts.findData(source.page["page"]));self.parts.blockSignals(False)
        for box, tracks in ((self.videos, source.videos), (self.audios, source.audios)):
            box.clear()
            for track in tracks:box.addItem(track.label, track.key)
        previous = self.window.config.get("video_progress", {}).get(f"{source.video.bvid}:{source.page['cid']}", {})
        for box, field in ((self.videos, "video_key"), (self.audios, "audio_key")):
            index = box.findData(previous.get(field))
            if index >= 0:box.setCurrentIndex(index)
        self.name.setText(self.saved_name or source.video.title[:65] + f" · P{source.page['page']}")
        self.saved_name = None
        self.expected_saved_cid = None

    def play(self):
        if self.source is None:
            self.lookup(auto_play=True);return
        window, source = self.window, self.source
        window.bangumi_page.cancel_lookup()
        window.runtime.save_video_progress()
        self.cancel_lookup()
        window.update_options()
        selected_quality = next((t.quality for t in source.videos if t.key == self.videos.currentData()), source.selected_video.quality)
        request = {"kind": "vod", "platform": "bilibili", "room_input": source.video.bvid + f":P{source.page['page']}",
                   "room_name": self.name.text().strip() or source.video.title, "video_input": source.video.bvid,
                   "bvid": source.video.bvid, "cid": source.page["cid"], "page": source.page["page"],
                   "video_key": self.videos.currentData(), "audio_key": self.audios.currentData(),
                   "subtitle_key": self.subtitle_box.currentData(),
                   "resume_position": resume_position(window.config, source.video.bvid, source.page["cid"]) if self.resume_box.isChecked() else 0,
                   "requested": selected_quality, "session": self.mode.currentData(), "backend": "potplayer",
                   "network_mode": window.config["network_mode"], "low_latency": False, "replay_mode": False,
                   "vsr": window.config["vsr"]}
        if window.runtime.play(request):
            window.confirmed.setChecked(False);window.note.clear()
        window.tick()

    def cancel_lookup(self):
        self.generation += 1
        self.auto_play_pending = False
        self.window.runtime.jobs.cancel("vod.lookup")
        self.window.runtime.jobs.cancel("vod.subtitles")

    def save_resume_option(self):
        self.window.config["vod_resume"] = self.resume_box.isChecked();self.window.save_settings()

    def clear_subtitles(self):
        if hasattr(self, "subtitle_box"):
            self.subtitle_box.clear();self.subtitle_box.addItem("不加载字幕", None)
            self.subtitle_status.setText("字幕选择在下次播放生效。")
        self.window.runtime.jobs.cancel("vod.subtitles")
        self.window.activity.clear("vod.subtitles")

    def load_subtitles(self):
        if self.source is None:
            self.window.runtime.error(ViewerError("请先获取视频，再查询当前分 P 字幕。", code="vod.metadata_needed", level="WARN"), scope="vod.subtitles")
            return
        source, window, generation = self.source, self.window, self.generation
        route = window.config["network_mode"]
        jar = window.cookies.get("bilibili") if self.mode.currentData() == "导入 Cookie" else None
        if self.mode.currentData() == "导入 Cookie" and jar is None:
            window.runtime.error(ViewerError("请先登录 B 站，或选择匿名后获取字幕。", code="vod.subtitle_login_missing", level="WARN"), scope="vod.subtitles")
            return
        def work():
            client = VodClient(jar, network_mode=route)
            try:return client.subtitle_catalog(source.video.bvid, source.page["cid"])
            finally:client.close()
        def done(result):
            tracks, login_required = result
            self.subtitle_box.clear();self.subtitle_box.addItem("不加载字幕", None)
            for track in tracks:self.subtitle_box.addItem(track.name, track.key)
            previous = window.config.get("video_progress", {}).get(f"{source.video.bvid}:{source.page['cid']}", {}).get("subtitle_key")
            index = self.subtitle_box.findData(previous)
            if index >= 0:self.subtitle_box.setCurrentIndex(index)
            self.subtitle_status.setText("当前会话无可用字幕；可登录后再查。" if login_required and not tracks else "当前分 P 没有可用平台字幕。" if not tracks else f"可用字幕 {len(tracks)} 条，下次播放生效。")
            window.activity.clear("vod.subtitles")
            window.activity.emit("vod.subtitles_loaded", self.subtitle_status.text(), source="vod", target=source.video.bvid)
        def failed(exc):
            window.runtime.error(ViewerError(str(exc), code="vod.subtitle_lookup_failed", level=exc.level), scope="vod.subtitles")
        window.runtime.jobs.submit("vod.subtitles", work, done, failed,
            valid=lambda: not window.closing and self.generation == generation and self.source is source and route == window.config["network_mode"])

    def record(self):
        if self.source is None:
            raise ViewerError("请先获取视频信息后收藏分 P。", code="vod.metadata_needed", level="WARN")
        return {"bvid": self.source.video.bvid, "cid": self.source.page["cid"], "page": self.source.page["page"],
                "name": self.name.text().strip() or self.source.video.title[:80]}

    def save(self):
        try:record = self.record()
        except ViewerError as exc:self.window.runtime.error(exc, scope="vod.lookup");return
        values = self.window.config["video_favorites"]
        existing = next((i for i, r in enumerate(values) if (r["bvid"], r["cid"]) == (record["bvid"], record["cid"])), None)
        if existing is None:
            if len(values) >= 500:
                self.window.runtime.error(ViewerError("视频收藏已达 500 项。", code="vod.favorite_limit", level="WARN"), scope="vod.lookup");return
            values.append(record)
        else:values[existing] = record
        self.window.save_settings();self.refresh_saved()
        self.window.activity.emit("vod.favorite_saved", "已收藏 " + record["name"] + "。", source="vod", target=record["bvid"])

    def refresh_saved(self):
        field = "video_history" if self.saved_mode.currentIndex() else "video_favorites"
        values = self.window.config[field]
        keys = (field, self.search.text(), tuple((r["bvid"], r["cid"], r["name"]) for r in values))
        if keys == self.last_keys:return
        self.last_keys = keys
        self.saved.blockSignals(True);self.saved.clear()
        query = self.search.text().lower().strip()
        for record in values:
            if query and query not in (record["name"] + record["bvid"]).lower():continue
            item = QListWidgetItem(record["name"] + f"\n{record['bvid']} · P{record['page']}")
            item.setData(Qt.ItemDataRole.UserRole, record);self.saved.addItem(item)
        self.saved.blockSignals(False)

    def select_record(self, record):
        self.input.setText(f"https://www.bilibili.com/video/{record['bvid']}?p={record['page']}")
        self.saved_name = record["name"];self.name.setText(record["name"])
        self.expected_saved_cid = record["cid"]

    def select_saved(self, item, previous=None):
        if item:self.select_record(item.data(Qt.ItemDataRole.UserRole))

    def rename(self):
        item = self.saved.currentItem()
        if item is None:return
        record = item.data(Qt.ItemDataRole.UserRole)
        name, ok = QInputDialog.getText(self, "视频改名", "收藏名称", text=record["name"])
        if ok and name.strip():
            for field in ("video_favorites", "video_history"):
                for r in self.window.config[field]:
                    if (r["bvid"], r["cid"]) == (record["bvid"], record["cid"]):r["name"] = name.strip()[:80]
            self.window.save_settings();self.refresh_saved()
            self.window.activity.emit("vod.favorite_renamed", "视频名称已更新。", source="vod", target=record["bvid"])

    def remove(self):
        item = self.saved.currentItem()
        if item is None:return
        record = item.data(Qt.ItemDataRole.UserRole)
        field = "video_history" if self.saved_mode.currentIndex() else "video_favorites"
        self.window.config[field] = [r for r in self.window.config[field] if (r["bvid"], r["cid"]) != (record["bvid"], record["cid"])]
        self.window.save_settings();self.refresh_saved()
        self.window.activity.emit("vod.favorite_removed", "已从列表移除视频。", source="vod", target=record["bvid"])

    def tick(self):
        runtime = self.window.runtime
        vod = runtime.active is not None and runtime.active.get("kind") == "vod" and runtime.active.get("content_type") != "pgc"
        busy = runtime.busy or "vod.lookup" in runtime.jobs.pending
        self.fetch.setText("正在获取…" if "vod.lookup" in runtime.jobs.pending else "获取视频")
        self.play_button.setEnabled(not busy)
        self.pause_button.setEnabled(vod and not runtime.busy and runtime.controller.state in ("playing", "paused"))
        self.stop_button.setEnabled(vod or self.auto_play_pending)
        self.retry_button.setEnabled(not runtime.busy and (runtime.last_request or {}).get("kind") == "vod" and (runtime.last_request or {}).get("content_type") != "pgc")
        self.pause_button.setText("继续" if runtime.paused else "暂停")
        self.subtitle_button.setEnabled(self.source is not None and "vod.subtitles" not in runtime.jobs.pending)
        if vod:
            self.status.setText({"resolving": "正在获取视频", "connecting": "正在连接播放器", "reconnecting": "正在恢复视频", "playing": "正在播放视频",
                "paused": "视频已暂停", "ended": "视频播放完毕", "error": "视频播放遇到问题"}.get(runtime.controller.state, "准备播放视频"))
            self.status.setToolTip(runtime.controller.message)
            native = runtime.player.snapshot() if runtime.player is not None else {}
            self.status_detail.setText(safe_text(runtime.active["room_name"], 45) + f"\n{native.get('position_seconds') or 0:.1f} / {native.get('duration_seconds') or 0:.1f} 秒" +
                                      (" · 实际 " + runtime.stream.actual_name if runtime.stream is not None else ""))
        elif (runtime.last_request or {}).get("kind") == "vod" and (runtime.last_request or {}).get("content_type") != "pgc":
            self.status.setText(runtime.controller.message)
        else:
            self.status.setText("准备播放视频");self.status_detail.clear()
        self.refresh_saved()
