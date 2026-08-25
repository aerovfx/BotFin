"""Phần 4 — Cá nhân hóa: học từ hành vi của người dùng để tinh chỉnh điểm đẩy tin.

Tín hiệu:
- "open": mở bài đọc trên dashboard, hoặc bấm 👍 trên Telegram
- "down": bấm nút ẩn trên dashboard, hoặc 👎 trên Telegram

Profile lưu trong profile.json: hệ số nhân cho từng chuyên mục / nguồn,
trọng số -1..1 cho từ khóa tiêu đề. ranking.py cộng thêm phần điều chỉnh
vào điểm nóng; mọi thứ đều có biên nên một tín hiệu không lật ngược hệ thống.
"""
import hashlib
import json
import re
import time

import fetch_news as fn

PROFILE_FILE = fn.BASE / "profile.json"
NEWS_STORE = fn.BASE / "news.json"

MULT_UP = 1.08
MULT_DOWN = 0.92
MULT_MIN = 0.6
MULT_MAX = 1.8
KW_DELTA = 0.25
KW_LIMIT = 1.0

EVENTS_KEEP = 100
VOTED_KEEP = 1500
HASHES_KEEP = 800

STOPWORDS = {"và", "của", "có", "cho", "với", "trong", "ra", "một", "các", "những", "được", "là", "đã", "sẽ", "bị", "the", "này", "kia"}


def config(env=None):
    env = env or fn.load_env(fn.ENV_FILE)
    return {"enabled": env.get("PERSONALIZE", "1") != "0"}


def load_profile():
    profile = fn.load_json(PROFILE_FILE, {})
    profile.setdefault("categories", {})
    profile.setdefault("sources", {})
    profile.setdefault("keywords", {})
    profile.setdefault("counts", {"open": 0, "down": 0})
    profile.setdefault("events", [])
    profile.setdefault("voted", {})
    profile.setdefault("hashes", {})
    return profile


def save_profile(profile):
    PROFILE_FILE.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")


def _clamp_mult(value):
    return max(MULT_MIN, min(MULT_MAX, value))


def link_hash(link):
    return hashlib.md5(link.encode("utf-8")).hexdigest()[:10]


def register_link(link):
    """Ghi nhận link chuẩn bị đẩy kênh để nút 👍/👎 trên Telegram tra ngược."""
    profile = load_profile()
    h = link_hash(link)
    if profile["hashes"].get(h) != link:
        profile["hashes"][h] = link
        profile["hashes"] = dict(list(profile["hashes"].items())[-HASHES_KEEP:])
        save_profile(profile)
    return h


def _tokens(title):
    words = re.sub(r"[^\w\sÀ-ỹ]", " ", (title or "").lower()).split()
    return {w for w in words if len(w) > 2 and w not in STOPWORDS and not w.isdigit()}


def find_item(link):
    for item in fn.load_json(NEWS_STORE, []):
        if item.get("link") == link:
            return item
    return None


def record(link, action):
    """Học một tín hiệu. Trả về True nếu tín hiệu được áp dụng (không trùng)."""
    if action not in ("open", "down"):
        return False
    delta = 1 if action == "open" else -1
    profile = load_profile()

    vote_key = f"{link_hash(link)}:{action}"
    if profile["voted"].get(vote_key):
        return False
    profile["voted"][vote_key] = True
    if len(profile["voted"]) > VOTED_KEEP:
        profile["voted"] = dict(list(profile["voted"].items())[-VOTED_KEEP:])

    item = find_item(link) or {}
    category = item.get("category")
    source = item.get("source")
    title = item.get("title", "")

    if category:
        profile["categories"][category] = round(_clamp_mult(profile["categories"].get(category, 1.0) * (MULT_UP if delta > 0 else MULT_DOWN)), 3)
    if source:
        profile["sources"][source] = round(_clamp_mult(profile["sources"].get(source, 1.0) * (MULT_UP if delta > 0 else MULT_DOWN)), 3)
    for token in _tokens(title):
        current = profile["keywords"].get(token, 0.0)
        updated = max(-KW_LIMIT, min(KW_LIMIT, current + delta * KW_DELTA))
        if abs(updated) < 0.05:
            updated = 0.0
        profile["keywords"][token] = round(updated, 2)

    profile["counts"][action] = profile["counts"].get(action, 0) + 1
    profile["events"].append({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "category": category or "?",
        "source": source or "?",
        "title": title[:60],
    })
    profile["events"] = profile["events"][-EVENTS_KEEP:]
    save_profile(profile)
    return True


def record_hash(h, action):
    link = load_profile()["hashes"].get(h)
    if not link:
        return False
    applied = record(link, action)
    register_link(link)
    return applied


def boost(item):
    """Phần điều chỉnh điểm (có thể âm) dựa trên profile hiện tại."""
    if not config()["enabled"]:
        return 0
    profile = load_profile()

    def mult(mapping, key):
        try:
            return float(mapping.get(key, 1.0))
        except (TypeError, ValueError):
            return 1.0

    adjustment = max(-7, min(7, (mult(profile["categories"], item.get("category")) - 1) * 14))
    adjustment += max(-5, min(5, (mult(profile["sources"], item.get("source")) - 1) * 10))

    keywords = profile["keywords"]
    liked = sum(1 for t in _tokens(item.get("title")) if keywords.get(t, 0) > 0.3)
    disliked = sum(1 for t in _tokens(item.get("title")) if keywords.get(t, 0) < -0.3)
    adjustment += min(liked * 3, 9) - min(disliked * 3, 9)
    return int(round(adjustment))


def summary():
    profile = load_profile()
    top_cats = sorted(profile["categories"].items(), key=lambda kv: kv[1], reverse=True)
    kw_pos = sorted(((k, v) for k, v in profile["keywords"].items() if v > 0.3), key=lambda kv: -kv[1])[:8]
    kw_neg = sorted(((k, v) for k, v in profile["keywords"].items() if v < -0.3), key=lambda kv: kv[1])[:8]
    return {
        "enabled": config()["enabled"],
        "counts": profile["counts"],
        "categories_liked": [c for c, m in top_cats if m > 1.05][:5],
        "categories_disliked": [c for c, m in reversed(top_cats) if m < 0.95][:5],
        "keywords_liked": [k for k, _ in kw_pos],
        "keywords_disliked": [k for k, _ in kw_neg],
    }


def vote_markup(link):
    """Bàn phím inline 👍/👎 cho tin đẩy lên Telegram (None khi tắt cá nhân hóa)."""
    if not config()["enabled"]:
        return None
    h = register_link(link)
    return {"inline_keyboard": [[
        {"text": "👍 Hợp ý", "callback_data": f"fb:u:{h}"},
        {"text": "👎 Kém", "callback_data": f"fb:d:{h}"},
    ]]}
