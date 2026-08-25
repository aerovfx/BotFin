"""Phần 3 — Xếp hạng thông minh: chấm điểm độ nóng 0-100 cho từng tin.

Tín hiệu (thuần cục bộ, không cần LLM):
- Từ khóa quan trọng theo nhóm trọng số (ranking_keywords.json, sửa được)
- Nhiều nguồn cùng đăng một chuyện (cụm tiêu đề tương tự) = tin nóng
- Tiêu đề/tóm tắt có số liệu cụ thể
- Độ ưu tiên nguồn (trường "priority" trong sources.json, 1-3)
- Trừ điểm tiêu đề câu view (clickbait)
"""
import json
import re
import sys

import fetch_news as fn

KEYWORDS_FILE = fn.BASE / "ranking_keywords.json"

DEFAULT_KEYWORDS = {
    "groups": [
        {"words": ["lãi suất", "lãi xuất", "tỷ giá", "lạm phát", "cpi", "gdp", "dự trữ ngoại hối", "nới lỏng", "thắt chặt", "tiền tệ"], "weight": 18},
        {"words": ["nghị quyết", "quốc hội", "chính phủ", "thủ tướng", "bộ trưởng", "luật", "nghị định", "thông tư", "pháp lệnh", "bầu cử", "tổng thống", "ngoại giao"], "weight": 14},
        {"words": ["fed", "ecb", "imf", "world bank", "wto", "opec", "khủng hoảng", "suy thoái", "phá sản", "mặc nợ", "sụp đổ", "cấm vận"], "weight": 16},
        {"words": ["vn-index", "vn30", "hnx", "phiên", "thanh khoản", "khớp lệnh", "khối ngoại", "cổ phiếu", "cổ tức", "ipo", "thoái vốn", "sáp nhập", "báo cáo tài chính", "lợi nhuận"], "weight": 10},
        {"words": ["vàng", "dầu thô", "xăng dầu", "giá điện", "than đá", "hàng hóa"], "weight": 8},
        {"words": ["tuyển sinh", "kỳ thi", "điểm chuẩn", "học phí", "goodwill", "du học", "giáo dục"], "weight": 10},
    ],
    "clickbait": ["sốc", "shock", "chấn động", "gây bão", "không thể tin", "đáng sợ", "hoảng loạn"],
    "stopwords": ["và", "của", "có", "cho", "với", "trong", "ra", "the", "một", "các", "những", "được", "là", "đã", "sẽ", "bị"],
}

BASE_SCORE = 22
KEYWORD_CAP = 30
CLUSTER_STEP = 9
CLUSTER_CAP = 27
NUMBER_BONUS = 6
PRIORITY_STEP = 4
PRIORITY_CAP = 8
CLICKBAIT_PENALTY = 9


def config(env=None):
    env = env or fn.load_env(fn.ENV_FILE)

    def _int(key, default):
        try:
            return int(env.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": env.get("RANKING", "1") != "0",
        "send_min": max(0, _int("SCORE_SEND_MIN", 38)),
        "top_fallback": max(0, _int("SEND_TOP_FALLBACK", 3)),
    }


def load_keywords():
    data = fn.load_json(KEYWORDS_FILE, {})
    if not data.get("groups"):
        try:
            KEYWORDS_FILE.write_text(json.dumps(DEFAULT_KEYWORDS, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"[warn] khong tao duoc {KEYWORDS_FILE.name}: {exc}", file=sys.stderr)
        return DEFAULT_KEYWORDS
    merged = dict(DEFAULT_KEYWORDS)
    merged.update({k: v for k, v in data.items() if v})
    return merged


def normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\sÀ-ỹ]", " ", (text or "").lower())).strip()


def token_set(text, stopwords):
    return {t for t in normalize(text).split() if len(t) > 1 and t not in stopwords}


def keyword_score(text, groups):
    text_l = (text or "").lower()
    total = sum(
        weight for group in groups
        for weight in [group.get("weight", 10)]
        if any(word.lower() in text_l for word in group.get("words", []))
    )
    return min(total, KEYWORD_CAP)


def has_numbers(text):
    if not text:
        return False
    return bool(re.search(r"\d+\s*%|\d+[.,]\d+|\b\d+\s*(tỷ|triệu|nghìn|ngàn|usd|đồng|tỉ)\b", text.lower()))


def clickbait_hit(text, phrases):
    text_l = (text or "").lower()
    return any(p.lower() in text_l for p in phrases)


def cluster_sizes(items, stopwords):
    """Gom tiêu đề tương tự lại; trả về map index -> số nguồn khác nhau trong cụm."""
    tokens = [token_set(i["title"], stopwords) for i in items]
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            if not tokens[a] or not tokens[b]:
                continue
            inter = len(tokens[a] & tokens[b])
            union = len(tokens[a] | tokens[b])
            if union and inter / union >= 0.55:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    clusters = {}
    for idx in range(len(items)):
        clusters.setdefault(find(idx), []).append(idx)
    sizes = {}
    for members in clusters.values():
        distinct_sources = len({items[m]["source"] for m in members})
        size = min(distinct_sources, 1 + CLUSTER_CAP // CLUSTER_STEP)
        for m in members:
            sizes[m] = size
    return sizes


def score_items(items):
    """Gán item['score'] (0-100) tại chỗ và trả về danh sách."""
    cfg = config()
    if not cfg["enabled"]:
        for item in items:
            item.setdefault("score", 0)
        return items

    rules = load_keywords()
    groups = rules.get("groups", [])
    clickbait = rules.get("clickbait", [])
    stopwords = set(rules.get("stopwords", []))
    priorities = {s.get("name"): s.get("priority", 1) for s in fn.load_json(fn.SOURCES_FILE, [])}
    sizes = cluster_sizes(items, stopwords)

    for idx, item in enumerate(items):
        searchable = f"{item['title']} {item.get('summary', '')} {item.get('ai_summary', '')}"
        score = BASE_SCORE
        score += keyword_score(searchable, groups)
        score += min(sizes.get(idx, 1) - 1, 3) * CLUSTER_STEP
        if has_numbers(item["title"]) or has_numbers(item.get("summary")):
            score += NUMBER_BONUS
        priority = priorities.get(item.get("source"), 1)
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 1
        score += min(max(priority - 1, 0), 2) * PRIORITY_STEP
        if clickbait_hit(f"{item['title']} {item.get('summary','')}", clickbait):
            score -= CLICKBAIT_PENALTY
        item["score"] = max(0, min(100, round(score)))
    return items


def split_for_send(items):
    """Chia tin thành (đẩy kênh, giữ lại) theo ngưỡng điểm.

    Không tin nào đạt ngưỡng → gửi tối đa top_fallback tin cao nhất để kênh không im lặng.
    """
    cfg = config()
    if not cfg["enabled"]:
        return list(items), []
    passed = [i for i in items if i.get("score", 0) >= cfg["send_min"]]
    if not passed and items and cfg["top_fallback"] > 0:
        ranked = sorted(items, key=lambda i: i.get("score", 0), reverse=True)
        passed = ranked[: cfg["top_fallback"]]
    passed_ids = {id(i) for i in passed}
    held = [i for i in items if id(i) not in passed_ids]
    return passed, held
