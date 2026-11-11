"""Season selection, episode rights, portable favorites and native PGC playback."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QSizePolicy, QVBoxLayout, QWidget

from ..bangumi import BangumiClient, bangumi_input
from ..bangumi_state import record, saved_name
from ..core import ViewerError
from ..events import safe_text
from .widgets import button, card, row, scrolling_page, text


class BangumiPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.season = self.episode = self.source = None
        self.generation = 0
        self.auto_play_pending = False
        self.expected_cid = self.selected_ep = None
        self.last_saved = None
        self.last_jar = window.cookies.get("bilibili")
        layout = QHBoxLayout(self);layout.setContentsMargins(0, 0, 0, 0);layout.setSpacing(16)
        saved, left = card("我的番剧", "作品收藏与单集观看历史")
        saved.setMinimumWidth(180);saved.setMaximumWidth(230)
        self.search = QLineEdit();self.search.setPlaceholderText("名称或 SS / EP 编号")
        self.search.textChanged.connect(self.refresh_saved)
        self.saved_mode = QComboBox();self.saved_mode.addItems(["作品收藏", "剧集历史"])
        self.saved_mode.currentIndexChanged.connect(self.refresh_saved)
        self.saved = QListWidget();self.saved.setWordWrap(True)
        self.saved.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.saved.currentItemChanged.connect(self.select_saved)
        self.saved.itemDoubleClicked.connect(lambda _: self.lookup(auto_play=True))
        left.addWidget(self.search);left.addWidget(self.saved_mode);left.addWidget(self.saved, 1)
        left.addLayout(row(button("改名", self.rename), button("移除", self.remove)))
        layout.addWidget(saved)
        column = QWidget();fixed = QVBoxLayout(column)
        fixed.setContentsMargins(0, 0, 0, 0);fixed.setSpacing(16)
        scroll, right = scrolling_page()
        entry, form = card("番剧与季度", "输入 EP / SS 或番剧链接，选择剧集后检查当前账号的整集资格。")
        self.input = QLineEdit();self.input.setPlaceholderText("ep693247、ss43164 或 /bangumi/play/ 链接")
        self.input.textChanged.connect(self.invalidate_input);self.input.returnPressed.connect(self.lookup)
        self.fetch = button("获取番剧", self.lookup, "Primary")
        form.addLayout(row(self.input, self.fetch))
        self.title = text("尚未选择作品", "CardTitle", True)
        self.detail = text("", "Caption", True)
        form.addWidget(self.title);form.addWidget(self.detail)
        self.seasons = QComboBox();self.seasons.currentIndexChanged.connect(self.season_changed)
        self.seasons.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        form.addWidget(text("季度 / 版本", "FieldLabel"));form.addWidget(self.seasons)
        self.name = QLineEdit();self.name.setMaxLength(80);self.name.setPlaceholderText("作品收藏名，可自定义")
        form.addLayout(row(self.name, button("收藏作品", self.save)))
        self.episodes = QListWidget();self.episodes.setWordWrap(True)
        self.episodes.setMinimumHeight(140);self.episodes.setMaximumHeight(240)
        self.episodes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.episodes.currentItemChanged.connect(self.episode_changed)
        self.episodes.itemDoubleClicked.connect(lambda _: self.play())
        form.addWidget(text("剧集 · 正片与附加内容", "FieldLabel"));form.addWidget(self.episodes)
        right.addWidget(entry)
        tracks, body = card("本集观看资格与画质")
        self.mode = QComboBox();self.mode.addItem("匿名", "匿名");self.mode.addItem("使用已登录 B 站账号", "导入 Cookie")
        self.mode.currentIndexChanged.connect(self.invalidate_tracks)
        self.qr_button = button("扫码登录", self.login)
        body.addLayout(row(self.mode, self.qr_button, button("检查本集", self.load_tracks)))
        self.rights = text("选择剧集后查询；会员标记是平台信息，整集资格需要当前会话实查。", "StatusContext", True)
        body.addWidget(self.rights)
        self.videos, self.audios = QComboBox(), QComboBox()
        for box in (self.videos, self.audios):box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        body.addWidget(text("实际可用视频轨道", "FieldLabel"));body.addWidget(self.videos)
        body.addWidget(text("音轨", "FieldLabel"));body.addWidget(self.audios)
        self.subtitle_box = QComboBox();self.subtitle_box.addItem("不加载 CC 字幕", None)
        self.subtitle_box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.subtitle_button = button("查询 CC 字幕", self.load_subtitles)
        self.subtitle_status = text("画面内嵌字幕会随视频播放；这里仅查询额外的平台 CC 字幕。", "Caption", True)
        body.addLayout(row(self.subtitle_box, self.subtitle_button));body.addWidget(self.subtitle_status)
        right.addWidget(tracks);right.addStretch()
        playback, controls = card()
        self.status = text("准备播放番剧", "CardTitle", True)
        self.status_detail = text("", "Caption", True)
        self.play_button = button("播放当前集", self.play, "Primary")
        self.pause_button = button("暂停", window.pause)
        self.stop_button = button("停止", window.stop)
        self.retry_button = button("恢复当前集", lambda: (window.runtime.reconnect(), window.tick()))
        controls.addLayout(row(self.status, self.retry_button));controls.addWidget(self.status_detail)
        controls.addLayout(row(self.play_button, self.pause_button, self.stop_button))
        self.resume_box = QCheckBox("从本集上次位置继续")
        self.resume_box.setChecked(window.config.get("vod_resume", True))
        self.resume_box.toggled.connect(self.save_resume_option)
        controls.addWidget(self.resume_box)
        fixed.addWidget(scroll, 1);fixed.addWidget(playback)
        layout.addWidget(column, 1)
        self.refresh_saved()
        if window.config.get("last_bangumi"):self.select_record(window.config["last_bangumi"])

    def login(self):
        self.mode.setCurrentIndex(1)
        self.window.qr_login("bilibili")

    def cancel_lookup(self):
        self.generation += 1;self.auto_play_pending = False
        for channel in ("pgc.lookup", "pgc.tracks", "pgc.subtitles"):self.window.runtime.jobs.cancel(channel)

    def invalidate_input(self):
        self.cancel_lookup()
        self.season = self.episode = self.source = None
        self.selected_ep = self.expected_cid = None
        for box in (self.seasons, self.episodes):
            box.blockSignals(True);box.clear();box.blockSignals(False)
        self.title.setText("尚未选择作品");self.detail.clear();self.name.clear()
        self.invalidate_tracks()

    def invalidate_tracks(self):
        self.cancel_lookup();self.source = None
        self.videos.clear();self.audios.clear()
        self.clear_subtitles()
        self.rights.setText("请检查当前集的账号资格与可用画质。")

    def clear_subtitles(self):
        self.window.runtime.jobs.cancel("pgc.subtitles")
        self.window.activity.clear("pgc.subtitles")
        self.subtitle_box.clear();self.subtitle_box.addItem("不加载 CC 字幕", None)
        self.subtitle_status.setText("画面内嵌字幕随视频播放；CC 字幕需单独查询。")

    def lookup(self, *, auto_play=False):
        try:params = bangumi_input(self.input.text())
        except ViewerError as exc:self.window.runtime.error(exc, scope="pgc.lookup");return
        self.cancel_lookup();generation = self.generation
        self.auto_play_pending = auto_play
        value, route = self.input.text().strip(), self.window.config["network_mode"]
        target = params.get("ep_id") or self.selected_ep
        jar = self.window.cookies.get("bilibili") if self.mode.currentData() == "导入 Cookie" else None
        self.window.activity.clear("pgc.lookup")
        self.window.activity.emit("pgc.lookup_started", "正在获取番剧季度与剧集。", source="pgc")
        def work():
            client = BangumiClient(jar, network_mode=route)
            try:return client.metadata(value)
            finally:client.close()
        def done(season):
            self.season = season
            self.title.setText(safe_text(season.title, 300));self.detail.setText(f"SS{season.season_id} · {len(season.episodes)} 项内容")
            self.name.setText(saved_name(self.window.config, season.season_id, season.title)[:80])
            self.seasons.blockSignals(True);self.seasons.clear()
            for s in season.seasons:self.seasons.addItem(s["title"], s["season_id"])
            self.seasons.setCurrentIndex(self.seasons.findData(season.season_id));self.seasons.blockSignals(False)
            selected = next((ep for ep in season.episodes if ep.ep_id == target), None)
            if target is not None and selected is None:
                self.auto_play_pending = False
                self.window.runtime.error(ViewerError("保存的 EP 不在当前季度中，请重新选择剧集。", code="pgc.episode_missing", level="WARN"), scope="pgc.lookup")
                self.populate_episodes(None);return
            selected = selected or next((e for e in season.episodes if e.section == "正片"), None)
            self.populate_episodes(selected.ep_id if selected else None)
            self.window.activity.clear("pgc.lookup")
            self.window.activity.emit("pgc.lookup_completed", "已获取 " + season.title + "。", source="pgc", target=f"ss{season.season_id}")
            if selected:
                self.episode = selected
                self.load_tracks(auto_play=auto_play)
            else:self.auto_play_pending = False
        def failed(exc):
            self.auto_play_pending = False;self.window.runtime.error(exc, scope="pgc.lookup")
        self.window.runtime.jobs.submit("pgc.lookup", work, done, failed,
            valid=lambda: not self.window.closing and generation == self.generation and route == self.window.config["network_mode"])

    def populate_episodes(self, selected=None):
        self.episodes.blockSignals(True);self.episodes.clear()
        for ep in self.season.episodes:
            item = QListWidgetItem(self.episode_label(ep))
            item.setData(Qt.ItemDataRole.UserRole, ep);self.episodes.addItem(item)
            if ep.ep_id == selected:self.episodes.setCurrentItem(item)
        self.episodes.blockSignals(False)

    def episode_label(self, ep):
        progress = self.window.config.get("video_progress", {}).get(f"pgc:{ep.ep_id}:{ep.cid}", {})
        state = "未上线 / 无媒体" if not ep.cid else ep.badge or ("会员" if ep.status == 13 else "已上线")
        watch = " · 已看完" if progress.get("completed") else f" · 上次 {progress['position']:.0f} 秒" if progress.get("position") else ""
        return f"{ep.section} · {ep.number}  {ep.title}\n{state} · EP{ep.ep_id}{watch}"

    def season_changed(self):
        sid = self.seasons.currentData()
        if self.season and sid and sid != self.season.season_id:
            self.input.setText(f"ss{sid}");self.lookup()

    def episode_changed(self, item, previous=None):
        self.invalidate_tracks();self.expected_cid = None
        self.episode = item.data(Qt.ItemDataRole.UserRole) if item else None
        if self.episode:self.load_tracks()

    def load_tracks(self, *, auto_play=False):
        if self.episode is None:
            self.lookup(auto_play=auto_play);return
        self.invalidate_tracks();self.auto_play_pending = auto_play
        episode, generation = self.episode, self.generation
        route, mode = self.window.config["network_mode"], self.mode.currentData()
        jar = self.window.cookies.get("bilibili") if mode == "导入 Cookie" else None
        if mode == "导入 Cookie" and jar is None:
            self.auto_play_pending = False
            self.window.runtime.error(ViewerError("请先扫码登录 B 站，或选择匿名检查。", code="account.missing", level="WARN"), scope="pgc.tracks");return
        expected = self.expected_cid if self.expected_cid is not None else episode.cid
        self.window.activity.clear("pgc.tracks")
        self.rights.setText("正在检查当前集的整集资格与实际轨道…")
        def work():
            client = BangumiClient(jar, network_mode=route)
            try:return client.inspect(f"ep{episode.ep_id}", expected_cid=expected)
            finally:client.close()
        def done(result):
            self.auto_play_pending = False
            status = "已登录" if result.account["login"] == "authenticated" else "匿名"
            vip = "大会员有效" if result.account["vip"] == "active" else "无有效大会员"
            self.rights.setText(f"{status} · {vip}\n{result.message}")
            if result.source is None:
                self.window.runtime.error(ViewerError(result.message, code=result.status, level="WARN"), scope="pgc.tracks");return
            self.source = result.source;self.expected_cid = None
            for box, tracks in ((self.videos, self.source.videos), (self.audios, self.source.audios)):
                for track in tracks:box.addItem(track.label + (f" · {track.bandwidth/1000000:.2f} Mbps" if track.kind == "video" else ""), track.key)
            progress = self.progress()
            for box, key in ((self.videos, "video_key"), (self.audios, "audio_key")):
                index = box.findData(progress.get(key))
                if index >= 0:box.setCurrentIndex(index)
            self.window.activity.clear("pgc.tracks")
            self.window.activity.emit("pgc.tracks_loaded", f"EP{episode.ep_id} 整集资格已确认，实际可用视频轨 {len(self.source.videos)} 条。", source="pgc", target=f"ep{episode.ep_id}")
            if auto_play:self.play()
        def failed(exc):
            self.auto_play_pending = False;self.rights.setText(str(exc));self.window.runtime.error(exc, scope="pgc.tracks")
        self.window.runtime.jobs.submit("pgc.tracks", work, done, failed,
            valid=lambda: not self.window.closing and generation == self.generation and self.episode is episode and route == self.window.config["network_mode"])

    def progress(self):
        if self.episode is None:return {}
        return self.window.config.get("video_progress", {}).get(f"pgc:{self.episode.ep_id}:{self.episode.cid}", {})

    def play(self):
        if self.source is None:
            self.load_tracks(auto_play=True);return
        window, source, ep = self.window, self.source, self.source.episode
        window.runtime.save_video_progress();window.vod_page.cancel_lookup();self.cancel_lookup()
        window.update_options()
        progress = self.progress()
        request = {"kind": "vod", "content_type": "pgc", "platform": "bilibili", "room_input": f"ep{ep.ep_id}",
            "room_name": saved_name(window.config, ep.season_id, source.season_title) + f" · {ep.number} {ep.title}",
            "ep_id": ep.ep_id, "season_id": ep.season_id, "cid": ep.cid, "page": 1,
            "video_key": self.videos.currentData(), "audio_key": self.audios.currentData(), "subtitle_key": self.subtitle_box.currentData(),
            "requested": next(t.quality for t in source.videos if t.key == self.videos.currentData()),
            "resume_position": progress.get("position", 0) if self.resume_box.isChecked() and not progress.get("completed") else 0,
            "session": self.mode.currentData(), "backend": "potplayer", "network_mode": window.config["network_mode"],
            "low_latency": False, "replay_mode": False, "vsr": window.config["vsr"]}
        if window.runtime.play(request):window.confirmed.setChecked(False);window.note.clear()
        window.tick()

    def save_resume_option(self):
        self.window.config["vod_resume"] = self.resume_box.isChecked();self.window.save_settings()

    def load_subtitles(self):
        if self.source is None:
            self.window.runtime.error(ViewerError("请先检查本集整集资格，再查询 CC 字幕。", code="pgc.metadata_needed", level="WARN"), scope="pgc.subtitles");return
        generation, source, route = self.generation, self.source, self.window.config["network_mode"]
        jar = self.window.cookies.get("bilibili") if self.mode.currentData() == "导入 Cookie" else None
        if self.mode.currentData() == "导入 Cookie" and jar is None:
            self.window.runtime.error(ViewerError("登录已清除，请先重新登录再查询字幕。", code="account.missing", level="WARN"), scope="pgc.subtitles");return
        def work():
            client = BangumiClient(jar, network_mode=route)
            try:return client.subtitle_catalog(source.episode)
            finally:client.close()
        def done(result):
            tracks, required = result
            self.subtitle_box.clear();self.subtitle_box.addItem("不加载 CC 字幕", None)
            for track in tracks:self.subtitle_box.addItem(track.name, track.key)
            index = self.subtitle_box.findData(self.progress().get("subtitle_key"))
            if index >= 0:self.subtitle_box.setCurrentIndex(index)
            self.subtitle_status.setText(f"可用 CC 字幕 {len(tracks)} 条，下次播放生效。" if tracks else "本集未返回额外 CC 字幕；画面内嵌字幕不受影响。" if not required else "平台要求登录后查询 CC 字幕。")
            self.window.activity.clear("pgc.subtitles")
            self.window.activity.emit("pgc.subtitles_loaded", self.subtitle_status.text(), source="pgc", target=f"ep{source.episode.ep_id}")
        self.window.runtime.jobs.submit("pgc.subtitles", work, done,
            lambda exc: self.window.runtime.error(exc, scope="pgc.subtitles"),
            valid=lambda: not self.window.closing and generation == self.generation and self.source is source and route == self.window.config["network_mode"])

    def save(self):
        if self.season is None:
            self.window.runtime.error(ViewerError("请先获取作品后收藏。", code="pgc.metadata_needed", level="WARN"), scope="pgc.lookup");return
        value = record({"season_id": self.season.season_id, "season_title": self.season.title,
                        "name": self.name.text(), "last_ep_id": self.episode.ep_id if self.episode else None})
        values = self.window.config["bangumi_favorites"]
        existing = next((i for i, r in enumerate(values) if r["season_id"] == value["season_id"]), None)
        if existing is not None:values[existing] = value
        elif len(values) < 500:values.append(value)
        else:
            self.window.runtime.error(ViewerError("作品收藏已达 500 项。", code="pgc.favorite_limit", level="WARN"), scope="pgc.lookup");return
        self.sync_name(value["season_id"], value["name"])
        self.window.save_settings();self.refresh_saved()
        self.window.activity.emit("pgc.favorite_saved", "已收藏 " + value["name"] + "。", source="pgc", target=f"ss{value['season_id']}")

    def sync_name(self, sid, name):
        for r in self.window.config["bangumi_favorites"] + self.window.config["bangumi_history"]:
            if r["season_id"] == sid:r["name"] = name
        last = self.window.config.get("last_bangumi")
        if last and last["season_id"] == sid:last["name"] = name

    def refresh_saved(self):
        field = "bangumi_history" if self.saved_mode.currentIndex() else "bangumi_favorites"
        values = self.window.config[field]
        state = (field, self.search.text(), tuple(tuple(sorted(r.items())) for r in values))
        if state == self.last_saved:return
        self.last_saved = state;self.saved.blockSignals(True);self.saved.clear()
        for value in values:
            label = value["name"] + (f" · {value['number']} {value['title']}\nEP{value['ep_id']}" if field == "bangumi_history" else f"\nSS{value['season_id']}")
            if self.search.text().strip().lower() not in label.lower():continue
            item = QListWidgetItem(label);item.setData(Qt.ItemDataRole.UserRole, value);self.saved.addItem(item)
        self.saved.blockSignals(False)

    def select_record(self, value):
        self.input.setText(f"ep{value['ep_id']}" if value.get("ep_id") else f"ss{value['season_id']}")
        self.selected_ep = value.get("ep_id") or value.get("last_ep_id")
        self.expected_cid = value.get("cid");self.name.setText(value["name"])

    def select_saved(self, item, previous=None):
        if item:self.select_record(item.data(Qt.ItemDataRole.UserRole))

    def rename(self):
        item = self.saved.currentItem()
        if item is None:return
        value = item.data(Qt.ItemDataRole.UserRole)
        name, ok = QInputDialog.getText(self, "作品改名", "自定义名称", text=value["name"])
        if ok and name.strip():
            self.sync_name(value["season_id"], name.strip()[:80]);self.window.save_settings();self.refresh_saved()
            self.window.activity.emit("pgc.favorite_renamed", "作品名称已更新。", source="pgc", target=f"ss{value['season_id']}")

    def remove(self):
        item = self.saved.currentItem()
        if item is None:return
        value = item.data(Qt.ItemDataRole.UserRole)
        field = "bangumi_history" if self.saved_mode.currentIndex() else "bangumi_favorites"
        self.window.config[field] = [r for r in self.window.config[field] if
            (r["ep_id"], r["cid"]) != (value["ep_id"], value["cid"])] if field == "bangumi_history" else [r for r in self.window.config[field] if r["season_id"] != value["season_id"]]
        self.window.save_settings();self.refresh_saved()
        self.window.activity.emit("pgc.favorite_removed", "已从列表移除。", source="pgc")

    def tick(self):
        runtime = self.window.runtime
        jar = self.window.cookies.get("bilibili")
        if jar is not self.last_jar:
            self.last_jar = jar
            if self.mode.currentData() == "导入 Cookie":self.invalidate_tracks()
        active = (runtime.active or {}).get("content_type") == "pgc"
        pending = any(k in runtime.jobs.pending for k in ("pgc.lookup", "pgc.tracks"))
        self.fetch.setText("正在获取…" if "pgc.lookup" in runtime.jobs.pending else "获取番剧")
        self.play_button.setEnabled(not runtime.busy and not pending)
        self.pause_button.setEnabled(active and not runtime.busy and runtime.controller.state in ("playing", "paused"))
        self.pause_button.setText("继续" if runtime.paused else "暂停")
        self.stop_button.setEnabled(active or self.auto_play_pending)
        self.retry_button.setEnabled(not runtime.busy and (runtime.last_request or {}).get("content_type") == "pgc")
        self.subtitle_button.setEnabled(self.source is not None and "pgc.subtitles" not in runtime.jobs.pending)
        if active:
            self.status.setText({"resolving": "正在获取番剧", "connecting": "正在连接播放器", "reconnecting": "正在恢复本集",
                "playing": "正在播放番剧", "paused": "本集已暂停", "ended": "本集播放完毕", "error": "本集无法继续播放"}.get(runtime.controller.state, "准备播放番剧"))
            native = runtime.player.snapshot() if runtime.player else {}
            self.status_detail.setText(safe_text(runtime.active["room_name"], 70) +
                f"\n{native.get('position_seconds', 0):.1f} / {native.get('duration_seconds', 0):.1f} 秒" + (" · " + runtime.stream.actual_name if runtime.stream else ""))
        elif (runtime.last_request or {}).get("content_type") == "pgc":self.status.setText(runtime.controller.message)
        else:self.status.setText("准备播放番剧");self.status_detail.clear()
        for i in range(self.episodes.count()):
            item = self.episodes.item(i)
            value = self.episode_label(item.data(Qt.ItemDataRole.UserRole))
            if item.text() != value:item.setText(value)
        self.refresh_saved()
