from live_viewer.vod import Video, Track, VideoSource


def source(page=1):
    video = Video("BV17x411w7KC", 170001, "普通视频测试", (
        {"page": 1, "cid": 279786, "title": "第一集", "duration": 199},
        {"page": 2, "cid": 279787, "title": "第二集", "duration": 120}))
    videos = tuple(Track(f"video:{q}:avc1.64001E:1000", "video", q, f"画质 {q}", "avc1.64001E", 1000,
                         (f"https://example.bilivideo.com/video-{q}.m4s?secret=private-test",), 512, 288, "30") for q in (64, 16))
    audio = Track("audio:30216:mp4a.40.2:51000", "audio", 30216, "AAC 51k", "mp4a.40.2", 51000,
                  ("https://example.bilivideo.com/audio.m4s?secret=private-test",))
    return VideoSource(video, video.page(page), videos, (audio,), videos[0], audio)


def api_records():
    return [
        {"bvid": "BV17x411w7KC", "aid": 170001, "title": "普通视频测试", "owner": {"name": "测试作者"},
         "pages": [{"page": 1, "cid": 279786, "part": "第一集", "duration": 199},
                   {"page": 2, "cid": 279787, "part": "第二集", "duration": 120}]},
        {"isLogin": False, "wbi_img": {"img_url": "https://i.example/" + "a" * 32 + ".png",
                                       "sub_url": "https://i.example/" + "b" * 32 + ".png"}},
        {"quality": 16, "support_formats": [{"quality": 16, "new_description": "360P"},
                                           {"quality": 120, "new_description": "4K 需权限"}],
         "dash": {"video": [{"id": 16, "codecs": "avc1.64001E", "bandwidth": 1000,
                             "width": 512, "height": 288, "baseUrl": "https://example.bilivideo.com/video.m4s"}],
                  "audio": [{"id": 30216, "codecs": "mp4a.40.2", "bandwidth": 51000,
                             "baseUrl": "https://example.bilivideo.com/audio.m4s"}]}}]
