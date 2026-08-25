import json
import logging
import re
import time

import requests

import fetch_news as fn

log = logging.getLogger("newsbot")

CACHE_FILE = fn.BASE / "ai_cache.json"
CACHE_KEEP = 600
BATCH_SIZE = 8
REQUEST_TIMEOUT = 60

HINT_CATEGORIES = [
    "Kinh tế vĩ mô",
    "Chứng khoán",
    "Ngân hàng - Tài chính",
    "Bất động sản",
    "Doanh nghiệp",
    "Kinh tế quốc tế",
    "Hàng hóa - Năng lượng",
    "Công nghệ",
    "Giáo dục",
    "Đời sống",
    "Giải trí - Thể thao",
]

PROMPT_TEMPLATE = """Bạn là trợ lý biên tập tin tức. Với mỗi bài báo dưới đây, hãy trả về một mảng JSON, mỗi phần tử gồm:
- "id": số thứ tự bài viết
- "summary": tóm tắt 1-2 câu tiếng Việt, khách quan, không lặp lại nguyên văn tiêu đề
- "category": nhãn chủ đề NGẮN GỌN (2-4 từ). Ưu tiên chọn một nhãn trong danh sách gợi ý nếu phù hợp: {hints}. Nếu không phù hợp thì tự đặt nhãn mới ngắn gọn.

Bài viết:
{articles}

Chỉ trả về mảng JSON thuần, không giải thích thêm."""


def config(env=None):
    env = env or fn.load_env(fn.ENV_FILE)
    enabled = env.get("AI_ENRICH", "0") == "1"
    url = env.get("LLM_API_URL", "").strip()
    model = env.get("LLM_MODEL", "").strip()
    return {
        "enabled": enabled and bool(url) and bool(model),
        "url": url,
        "model": model,
        "key": env.get("LLM_API_KEY", "").strip(),
        "max_per_run": _to_int(env.get("AI_MAX_PER_RUN", "20"), 20),
    }


def _to_int(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def load_cache():
    return fn.load_json(CACHE_FILE, {})


def save_cache(cache):
    trimmed = dict(sorted(cache.items(), key=lambda kv: kv[1].get("at", 0))[-CACHE_KEEP:])
    CACHE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def call_llm(cfg, prompt):
    headers = {"Content-Type": "application/json"}
    if cfg["key"]:
        headers["Authorization"] = f"Bearer {cfg['key']}"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    resp = requests.post(cfg["url"], headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def parse_reply(text):
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    out = {}
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        summary = str(entry.get("summary", "")).strip()
        category = str(entry.get("category", "")).strip()[:40]
        if summary or category:
            out[entry["id"]] = {"summary": summary, "category": category}
    return out or None


def enrich_batch(cfg, batch):
    articles = "\n".join(
        f"{idx}. Tiêu đề: {item['title']}\n   Nội dung gốc: {(item.get('summary') or '(không có)')[:300]}"
        for idx, item in enumerate(batch, 1)
    )
    prompt = PROMPT_TEMPLATE.format(articles=articles, hints=", ".join(HINT_CATEGORIES))
    reply = call_llm(cfg, prompt)
    parsed = parse_reply(reply)
    if not parsed:
        raise ValueError(f"không đọc được JSON từ LLM: {str(reply)[:120]}")
    results = []
    for idx, item in enumerate(batch, 1):
        hit = parsed.get(idx)
        enriched = dict(item)
        if hit:
            if hit["summary"]:
                enriched["ai_summary"] = hit["summary"]
            if hit["category"]:
                enriched["source_category"] = item.get("category", "")
                enriched["category"] = hit["category"]
        results.append(enriched)
    return results


def enrich(items):
    """Bổ sung ai_summary + category theo nội dung cho danh sách tin mới.

    Trả về (items, stats). Khi chưa cấu hình hoặc lỗi, trả nguyên items ban đầu.
    """
    stats = {"enabled": False, "enriched": 0, "cached": 0, "failed_batches": 0}
    cfg = config()
    if not cfg["enabled"] or not items:
        return items, stats
    stats["enabled"] = True
    cache = load_cache()

    pending, result = [], []
    for item in items:
        hit = cache.get(item["link"])
        if hit:
            enriched = dict(item)
            enriched.setdefault("source_category", item.get("category", ""))
            if hit.get("summary"):
                enriched["ai_summary"] = hit["summary"]
            if hit.get("category"):
                enriched["category"] = hit["category"]
            result.append(enriched)
            stats["cached"] += 1
        else:
            pending.append(item)

    overflow = pending[cfg["max_per_run"]:]
    pending = pending[: cfg["max_per_run"]]
    result.extend(overflow)

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        try:
            enriched_batch = enrich_batch(cfg, batch)
            for src, enr in zip(batch, enriched_batch):
                cache[src["link"]] = {
                    "summary": enr.get("ai_summary", ""),
                    "category": enr.get("category", ""),
                    "at": time.time(),
                }
                if enr.get("ai_summary") or enr.get("category") != src.get("category"):
                    stats["enriched"] += 1
                result.append(enr)
            log.info("AI làm giàu %d/%d tin trong lô", sum(1 for e in enriched_batch if e.get("ai_summary")), len(batch))
        except Exception as exc:
            stats["failed_batches"] += 1
            log.warning("AI enrich lô lỗi: %s — giữ tin gốc", exc)
            result.extend(batch)
        time.sleep(1)

    try:
        save_cache(cache)
    except Exception as exc:
        log.warning("Không lưu được AI cache: %s", exc)
    return result, stats


def status_line(stats):
    if not stats.get("enabled"):
        return ""
    parts = [f"AI: làm giàu {stats['enriched']}"]
    if stats.get("cached"):
        parts.append(f"dùng cache {stats['cached']}")
    if stats.get("failed_batches"):
        parts.append(f"{stats['failed_batches']} lô lỗi")
    return " | ".join(parts)
