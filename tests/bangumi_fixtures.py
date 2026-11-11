from live_viewer.bangumi import Episode, Season, BangumiSource, Inspection
from live_viewer.vod import Video, Track


def season():
    episodes = (Episode(693247, 43164, 875212290, 944573356, "BV1yW4y1j7Ft", "1", "孤独的转机", 1415),
                Episode(706287, 43164, 932704646, 476092951, "BV1hK41167rG", "7", "去你家", 1425, status=13, badge="会员"),
                Episode(999999, 43164, 0, 0, "", "未上线", "预留", 0))
    return Season(43164, "孤独摇滚！", episodes, ({"season_id": 43164, "title": "第一季"}, {"season_id": 50001, "title": "其他版本"}))


def source(ep_id=693247):
    ep = season().episode(ep_id)
    page = {"page": 1, "cid": ep.cid, "title": ep.title, "duration": ep.duration}
    video = Video(ep.bvid, ep.aid, "孤独摇滚！ · " + ep.number, (page,))
    high = Track("video:112:avc1:1800000", "video", 112, "1080P 高码率", "avc1", 1800000, ("https://test.bilivideo.com/a?signature=fixture-secret",), 1920, 1080, "24")
    low = Track("video:80:avc1:1000000", "video", 80, "1080P 高清", "avc1", 1000000, high.urls, 1920, 1080, "24")
    audio = Track("audio:30280:mp4a:192000", "audio", 30280, "AAC", "mp4a.40.2", 192000, high.urls)
    return BangumiSource(video, page, (high, low), (audio,), high, audio, episode=ep, season_title="孤独摇滚！",
                         account={"login": "authenticated", "vip": "active"}, media_duration=ep.duration, play_detail="PLAY_WHOLE")


def inspection(ep_id=693247, *, preview=False):
    value = source(ep_id)
    return Inspection(season(), value.episode, value.account, "pgc.preview_only" if preview else "pgc.whole",
                      "当前会话仅获试看。" if preview else "整集资格已确认。", {}, None if preview else value)


def request(ep_id=693247):
    s = source(ep_id)
    return {"kind": "vod", "content_type": "pgc", "platform": "bilibili", "room_input": f"ep{ep_id}",
            "room_name": s.video.title, "ep_id": ep_id, "season_id": s.episode.season_id, "cid": s.episode.cid, "page": 1,
            "session": "匿名", "backend": "potplayer", "network_mode": "direct", "requested": 127, "low_latency": False}
