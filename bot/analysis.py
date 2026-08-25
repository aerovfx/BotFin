"""Phần 5 — Phân tích & cảnh báo.

- Sentiment tin tức: từ điển tiếng Việt tài chính (+ xử lý phủ định "không/chưa"),
  chấm điểm -1..+1 cho từng tin, gắn nhãn Tích cực / Tiêu cực / Trung tính.
- Phát hiện xu hướng: gom cụm tiêu đề tương tự trong kho tin gần đây,
  cụm có nhiều nguồn cùng đăng = chủ đề đang nóng.
- Alert thị trường: so snapshot chỉ số với ngưỡng trong alerts.json
  (tự tạo mặc định lần đầu), có cooldown chống spam.
"""
import json
import logging
import re
import sys
import time

import fetch_news as fn

log = logging.getLogger("newsbot")

ALERTS_FILE = fn.BASE / "alerts.json"
ALERT_STATE_FILE = fn.BASE / "alert_state.json"
TREND_WINDOW_ITEMS = 250

POSITIVE = [
    "tăng trưởng", "hồi phục", "phục hồi", "kỷ lục", "lập đỉnh", "đỉnh mới",
    "vượt kỳ vọng", "vượt mục tiêu", "khởi sắc", "bùng nổ", "bứt phá",
    "mua ròng", "thắng thầu", "cổ tức", "tích cực", "đột phá", "khả quan",
    "ký kết", "khánh thành", "xuất khẩu tăng", "lợi nhuận tăng", "giải ngân",
    "góp phần", "thúc đẩy", "cải thiện", "nâng hạng", "rút ngắn",
]
NEGATIVE = [
    "suy giảm", "giảm sâu", "lao dốc", "trượt dài", "rớt", "thủng đáy",
    "phá sản", "mặc nợ", "khủng hoảng", "suy thoái", "thua lỗ", "lỗ nặng",
    "cắt giảm", "sa thải", "nợ xấu", "bong bóng", "đình trệ", "tê liệt",
    "lo ngại", "rủi ro", "trừng phạt", "cấm vận", "bán ròng", "vỡ", "sụp đổ",
    "chìm", "hoãn", "tạm dừng", "đìu hiu", "lạm phát cao", "kéo dài",
]
NEGATORS = ["không", "chưa", "ngưng", "ngừng", "dừng", "mất"]


def config(env=None):
    env = env or fn.load_env(fn.ENV_FILE)

    def _int(key, default):
        try:
            return max(0, int(env.get(key, str(default))))
        except (TypeError, ValueError):
            return default

    return {
        "sentiment": env.get("SENTIMENT", "1") != "0",
        "trends": env.get("TRENDS", "1") != "0",
        "alerts": env.get("ALERTS", "1") != "0",
        "alert_cooldown_min": _int("ALERT_COOLDOWN_MINUTES", 90),
    }


# ---------------------------------------------------------------- sentiment

def _negated(text_lower, pos):
    """True nếu cụm tại vị trí pos bị phủ định bởi từ đứng ngay trước."""
    prefix = text_lower[max(0, pos - 12):pos]
    return any(neg in prefix for neg in NEGATORS)


def analyze_sentiment(item):
    """Gán item['sentiment'] = {'label', 'score', 'hits'} và trả về item."""
    text = f"{item.get('title', '')} {item.get('ai_summary') or item.get('summary', '')}".lower()
    pos_hits, neg_hits = [], []
    for word in POSITIVE:
        for match in re.finditer(re.escape(word), text):
            if not _negated(text, match.start()):
                pos_hits.append(word)
                break
    for word in NEGATIVE:
        for match in re.finditer(re.escape(word), text):
            if _negated(text, match.start()):
                pos_hits.append(word)
            else:
                neg_hits.append(word)
                break

    total = len(pos_hits) + len(neg_hits)
    score = round((len(pos_hits) - len(neg_hits)) / total, 2) if total else 0.0
    if score > 0.2:
        label = "pos"
    elif score < -0.2:
        label = "neg"
    else:
        label = "neu"
    item["sentiment"] = {"label": label, "score": score}
    return item


def enrich_sentiment(items):
    if not config()["sentiment"]:
        return items
    for item in items:
        analyze_sentiment(item)
    return items


def sentiment_summary(news_pool, limit=100):
    """Tỷ lệ cảm xúc trong ~limit tin gần nhất — dùng cho dashboard/Telegram."""
    recent = [i for i in news_pool[-limit:] if i.get("sentiment")]
    if not recent:
        return None
    counts = {"pos": 0, "neg": 0, "neu": 0}
    for i in recent:
        counts[i["sentiment"]["label"]] += 1
    total = len(recent)
    avg = round(sum(i["sentiment"]["score"] for i in recent) / total, 2)
    return {"sampled": total, **counts, "avg_score": avg}


# ---------------------------------------------------------------- trends

def _tokens(title):
    words = re.sub(r"[^\w\sÀ-ỹ]", " ", (title or "").lower()).split()
    return {w for w in words if len(w) >= 4 and not w.isdigit()}


def detect_trends(news_pool, top=6, min_items=3, min_sources=2):
    """Trả về các chủ đề đang nóng: cụm tin tương tự, nhiều nguồn, mới nhất trên cùng."""
    if not config()["trends"]:
        return []
    pool = news_pool[-TREND_WINDOW_ITEMS:]
    tokens = [_tokens(i.get("title")) for i in pool]
    parent = list(range(len(pool)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(pool)):
        for b in range(a + 1, len(pool)):
            if not tokens[a] or not tokens[b]:
                continue
            inter = len(tokens[a] & tokens[b])
            union = len(tokens[a] | tokens[b])
            if union and inter / union >= 0.45:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    clusters = {}
    for idx in range(len(pool)):
        clusters.setdefault(find(idx), []).append(idx)

    trends = []
    for members in clusters.values():
        sources = {pool[m].get("source") for m in members}
        if len(members) < min_items or len(sources) < min_sources:
            continue
        newest_idx = max(members, key=lambda m: pool[m].get("fetched_at", ""))
        representative = pool[newest_idx]
        sentiments = [pool[m]["sentiment"]["label"] for m in members if pool[m].get("sentiment")]
        mood = max(set(sentiments), key=sentiments.count) if sentiments else "neu"
        trends.append({
            "title": representative.get("title"),
            "link": representative.get("link"),
            "count": len(members),
            "sources": sorted(sources)[:5],
            "category": representative.get("category", "Khác"),
            "last_at": representative.get("fetched_at", ""),
            "mood": mood,
        })
    trends.sort(key=lambda t: t["last_at"], reverse=True)
    trends.sort(key=lambda t: -t["count"])
    return trends[:top]


# ---------------------------------------------------------------- alerts

DEFAULT_RULES = {
    "rules": [
        {"name": "VN-Index biến động mạnh", "symbol": "VN-Index", "metric": "pct", "op": "abs_gte", "value": 1.5},
        {"name": "VN30 biến động mạnh", "symbol": "VN30", "metric": "pct", "op": "abs_gte", "value": 1.5},
        {"name": "HNX-Index biến động mạnh", "symbol": "HNX-Index", "metric": "pct", "op": "abs_gte", "value": 2.0},
        {"name": "USD/VND dịch chuyển mạnh", "symbol": "USD/VND", "metric": "pct", "op": "abs_gte", "value": 0.8},
        {"name": "Vàng quốc tế biến động mạnh", "symbol": "Vàng quốc tế", "metric": "pct", "op": "abs_gte", "value": 2.0},
    ],
    "cooldown_minutes": 90,
}


def load_rules():
    data = fn.load_json(ALERTS_FILE, {})
    if not data.get("rules"):
        try:
            ALERTS_FILE.write_text(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"[warn] khong tao duoc {ALERTS_FILE.name}: {exc}", file=sys.stderr)
        return DEFAULT_RULES
    return data


def load_alert_state():
    return fn.load_json(ALERT_STATE_FILE, {})


def save_alert_state(state):
    ALERT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


_OPS = {
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "abs_gte": lambda v, t: abs(v) >= t,
}


def check_alerts(snapshot=None):
    """So snapshot thị trường với rules; trả về list alert vừa kích hoạt."""
    cfg = config()
    if not cfg["alerts"] or not snapshot:
        return []
    rules = load_rules().get("rules", [])
    state = load_alert_state()
    cooldown_s = cfg["alert_cooldown_min"] * 60
    now = time.time()

    rows = list(snapshot.get("indices", [])) + list(snapshot.get("extras", []))
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(str(row.get("symbol", "")).lower(), row)

    fired = []
    for rule in rules:
        symbol = str(rule.get("symbol", "")).lower()
        row = next((by_symbol[s] for s in by_symbol if s.startswith(symbol) or symbol in s), None)
        if row is None:
            continue
        try:
            value = float(row.get(rule.get("metric", "pct"), 0) or 0)
        except (TypeError, ValueError):
            continue
        op = _OPS.get(rule.get("op", "abs_gte"))
        threshold = float(rule.get("value", 10**9))
        if not op(value, threshold):
            continue

        key = rule.get("name") or f"{row.get('symbol')}:{rule.get('metric')}"
        last = state.get(key, {})
        if now - last.get("at", 0) < cooldown_s and last.get("value") == value:
            continue

        pct = row.get("pct", 0)
        arrow = "▲" if isinstance(pct, (int, float)) and pct > 0 else ("▼" if isinstance(pct, (int, float)) and pct < 0 else "•")
        sign = "+" if isinstance(pct, (int, float)) and pct > 0 else ""
        fired.append({
            "rule": key,
            "message": (
                f"🚨 CẢNH BÁO THỊ TRƯỜNG\n{arrow} {row.get('symbol')}: {row.get('value')} "
                f"({sign}{pct}%)\n— {key} (ngưỡng {threshold})"
            ),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        state[key] = {"at": now, "value": value}

    if fired:
        try:
            save_alert_state(state)
        except OSError as exc:
            log.warning("Không lưu được alert state: %s", exc)
        log.warning("Kích hoạt %d cảnh báo thị trường", len(fired))
    return fired


def append_history(events, keep=50):
    hist_file = fn.BASE / "alert_history.json"
    history = fn.load_json(hist_file, [])
    history.extend({"at": e["at"], "rule": e["rule"], "message": e["message"]} for e in events)
    hist_file.write_text(json.dumps(history[-keep:], ensure_ascii=False, indent=1), encoding="utf-8")
