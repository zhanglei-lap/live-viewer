"""Portable season favorites and episode history; never persist media addresses."""
from datetime import datetime


def record(value, *, episode=False):
    if not isinstance(value, dict):return None
    keys = ("season_id", "ep_id", "cid") if episode else ("season_id",)
    if any(type(value.get(k)) is not int or not 0 < value[k] < 10**16 for k in keys):return None
    result = {k: value[k] for k in keys}
    for key, limit in (("name", 80), ("season_title", 300), ("title", 300), ("number", 80)):
        text = value.get(key, "")
        result[key] = text.strip()[:limit] if isinstance(text, str) else ""
    result["name"] = result["name"] or result["season_title"] or f"番剧 SS{result['season_id']}"
    if not episode and type(value.get("last_ep_id")) is int and 0 < value["last_ep_id"] < 10**16:
        result["last_ep_id"] = value["last_ep_id"]
    if episode:
        stamp = value.get("played_at", "")
        result["played_at"] = stamp[:40] if isinstance(stamp, str) else ""
    return result


def records(values, *, episode=False):
    output, seen = [], set()
    for value in values[:1000] if isinstance(values, list) else []:
        item = record(value, episode=episode)
        if item is None:continue
        key = (item["ep_id"], item["cid"]) if episode else item["season_id"]
        if key in seen:continue
        seen.add(key);output.append(item)
        if len(output) >= (50 if episode else 500):break
    return output


def remember(history, value):
    item = record(value, episode=True)
    if item is None:return history
    item["played_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return [item] + [r for r in history if (r["ep_id"], r["cid"]) != (item["ep_id"], item["cid"])][:49]


def saved_name(config, season_id, default=""):
    return next((r["name"] for r in config.get("bangumi_favorites", []) if r["season_id"] == season_id), default)
