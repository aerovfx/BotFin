import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

import ai_enrich
import fetch_news as fn
import market_data as md
import ranking
import telegram_bot as tb

BASE = Path(__file__).parent
NEWS_STORE = BASE / "news.json"
STATS_FILE = BASE / "stats.json"
LOG_FILE = BASE / "bot.log"
NEWS_KEEP = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("newsbot")

app = Flask(__name__)
fetch_lock = threading.Lock()


def load_store(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_store(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def get_env():
    return fn.load_env(fn.ENV_FILE)


def env_value(key, default):
    value = get_env().get(key, "").strip()
    return value if value else default


def get_interval():
    try:
        return max(5, int(env_value("FETCH_INTERVAL_MINUTES", "30")))
    except ValueError:
        return 30


def get_auto_send():
    return env_value("AUTO_SEND", "0") == "1"


def channel_ready(kind):
    env = get_env()
    if kind == "telegram":
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        chat = env.get("TELEGRAM_CHAT_ID", "")
    else:
        token = env.get("DISCORD_BOT_TOKEN", "")
        chat = env.get("DISCORD_CHANNEL_ID", "")
    return bool(token) and bool(chat) and "your_" not in token


def update_env(updates):
    lines = fn.ENV_FILE.read_text(encoding="utf-8").splitlines() if fn.ENV_FILE.exists() else []
    seen_keys = set()
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key and key in updates:
            seen_keys.add(key)
            value = updates[key]
            if value != "":
                out.append(f"{key}={value}")
            else:
                out.append(line)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen_keys and value != "":
            out.append(f"{key}={value}")
    fn.ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def append_news(items):
    store = load_store(NEWS_STORE, [])
    known = {i["link"] for i in store}
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for item in items:
        if item["link"] in known:
            continue
        entry = dict(item)
        entry["fetched_at"] = now
        store.append(entry)
        known.add(item["link"])
        added += 1
    save_store(NEWS_STORE, store[-NEWS_KEEP:])
    return added


def backfill_display():
    sources = fn.load_json(fn.SOURCES_FILE, [])
    collected = []
    for source in sources[:8]:
        try:
            collected.extend(fn.fetch_feed(source)[:10])
        except Exception as exc:
            log.warning("backfill %s: %s", source["name"], exc)
    append_news(collected)
    log.info("Backfill hien thi %d tin", len(collected))


def run_fetch(limit_per_source=10):
    state = fn.load_json(fn.STATE_FILE, {})
    sources = fn.load_json(fn.SOURCES_FILE, [])
    new_items, errors, ok_sources = [], [], 0

    md.update_market()

    for source in sources:
        try:
            fetched = fn.fetch_feed(source)
            fresh = [i for i in fetched if i["link"] not in state][:limit_per_source]
            new_items.extend(fresh)
            ok_sources += 1
            log.info("%s: %d tin moi", source["name"], len(fresh))
        except Exception as exc:
            errors.append(f"{source['name']}: {exc}")
            log.error("%s: %s", source["name"], exc)

    stats = load_store(STATS_FILE, {"runs": 0, "total_fetched": 0})
    stats["runs"] = stats.get("runs", 0) + 1
    stats["last_run"] = datetime.now().isoformat(timespec="seconds")
    stats["last_new"] = len(new_items)
    result = {"new": len(new_items), "sources_ok": ok_sources, "errors": errors}

    if not new_items:
        save_store(STATS_FILE, stats)
        result["telegram"] = result["discord"] = "-"
        return result

    for item in new_items:
        state[item["link"]] = time.time()
    cutoff = time.time() - fn.STATE_MAX_AGE
    state = {k: v for k, v in state.items() if v >= cutoff}
    fn.STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    new_items, ai_stats = ai_enrich.enrich(new_items)
    if ai_stats.get("enabled"):
        log.info("AI enrich: %s", ai_enrich.status_line(ai_stats))
    ranking.score_items(new_items)
    hot = [i for i in new_items if i.get("score", 0) >= ranking.config()["send_min"]]
    stats["last_push"] = len(hot)
    stats["last_held"] = len(new_items) - len(hot)
    stats["last_top_score"] = max((i.get("score", 0) for i in new_items), default=0)
    fn.save_news_file(new_items)
    append_news(new_items)
    stats["total_fetched"] = stats.get("total_fetched", 0) + len(new_items)
    save_store(STATS_FILE, stats)

    to_push, held_back = ranking.split_for_send(new_items)
    result["push"] = len(to_push)
    result["held"] = len(held_back)
    messages = [fn.format_item(i) for i in to_push]
    held_note = f" (giữ lại {len(held_back)} tin ít quan trọng)" if held_back else ""
    for kind in ("telegram", "discord"):
        if get_auto_send() and channel_ready(kind):
            sent = (fn.send_telegram if kind == "telegram" else fn.send_discord)(get_env(), messages)
            result[kind] = f"đã gửi {sent}/{len(messages)}{held_note}"
        elif get_auto_send():
            result[kind] = "chưa cấu hình kênh"
        else:
            result[kind] = "tắt tự động gửi"
    return result


def worker():
    while True:
        try:
            run_fetch()
        except Exception:
            log.exception("Vong lap tu dong loi")
        wait_s = get_interval() * 60
        end = time.time() + wait_s
        while time.time() < end:
            time.sleep(min(10, end - time.time()))


def mask(value):
    if not value or "your_" in value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    env = get_env()
    stats = load_store(STATS_FILE, {})
    news = load_store(NEWS_STORE, [])
    interval = get_interval()
    last_run = stats.get("last_run")
    next_run = ""
    if last_run:
        try:
            next_run = (datetime.fromisoformat(last_run) + timedelta(minutes=interval)).isoformat(timespec="minutes")
        except ValueError:
            pass
    return jsonify({
        "running": fetch_lock.locked(),
        "interval": interval,
        "auto_send": get_auto_send(),
        "telegram_ready": channel_ready("telegram"),
        "discord_ready": channel_ready("discord"),
        "sources_count": len(fn.load_json(fn.SOURCES_FILE, [])),
        "news_total": len(news),
        "last_run": last_run or "",
        "next_run": next_run,
        "last_new": stats.get("last_new", "-"),
        "today_fetched": sum(1 for i in news if i.get("fetched_at", "").startswith(datetime.now().strftime("%Y-%m-%d"))),
    })


def trigger_fetch():
    if not fetch_lock.acquire(blocking=False):
        return {"error": "Đang có phiên lấy tin chạy, thử lại sau"}
    try:
        return run_fetch()
    finally:
        fetch_lock.release()


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    result = trigger_fetch()
    if "error" in result:
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/market")
def api_market():
    return jsonify(md.load_market())


@app.route("/api/news")
def api_news():
    news = load_store(NEWS_STORE, [])
    source = request.args.get("source", "")
    category = request.args.get("category", "")
    q = request.args.get("q", "").lower()
    try:
        limit = min(int(request.args.get("limit", "60")), NEWS_KEEP)
    except ValueError:
        limit = 60
    try:
        min_score = max(0, int(request.args.get("min_score", "0")))
    except ValueError:
        min_score = 0
    sort = request.args.get("sort", "")
    items = []
    for item in reversed(news):
        if source and item.get("source") != source:
            continue
        if category and item.get("category", "Khác") != category:
            continue
        if q and q not in item.get("title", "").lower() and q not in item.get("summary", "").lower():
            continue
        if min_score and item.get("score", 0) < min_score:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    if sort == "top":
        items.sort(key=lambda i: i.get("score", 0), reverse=True)
    return jsonify({"items": items})


@app.route("/api/categories")
def api_categories():
    news = load_store(NEWS_STORE, [])
    sources = fn.load_json(fn.SOURCES_FILE, [])
    cats = sorted({s.get("category", "Khác") for s in sources if s.get("category")}
                  | {i.get("category", "Khác") for i in news if i.get("category")})
    counts = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for item in news:
        cat = item.get("category", "Khác")
        counts[cat] = counts.get(cat, 0) + 1
    return jsonify({"categories": cats, "counts": counts})


@app.route("/api/sources", methods=["GET", "POST"])
def api_sources():
    sources = fn.load_json(fn.SOURCES_FILE, [])
    if request.method == "GET":
        return jsonify({"sources": sources})
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    if not name or not url.startswith(("http://", "https://")):
        return jsonify({"error": "Cần tên nguồn và URL bắt đầu bằng http(s)://"}), 400
    if any(s.get("url") == url for s in sources):
        return jsonify({"error": "Nguồn này đã có trong danh sách"}), 400
    sources.append({"name": name, "url": url})
    fn.SOURCES_FILE.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Them nguon %s (%s)", name, url)
    return jsonify({"ok": True, "sources": sources})


@app.route("/api/sources/<int:idx>", methods=["DELETE"])
def api_delete_source(idx):
    sources = fn.load_json(fn.SOURCES_FILE, [])
    if 0 <= idx < len(sources):
        removed = sources.pop(idx)
        fn.SOURCES_FILE.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Xoa nguon %s", removed.get("name"))
        return jsonify({"ok": True, "sources": sources})
    return jsonify({"error": "Không tìm thấy nguồn"}), 404


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        env = get_env()
        tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
        dc_token = env.get("DISCORD_BOT_TOKEN", "")
        return jsonify({
            "telegram_token_hint": mask(tg_token),
            "telegram_chat_id": "" if "your_" in env.get("TELEGRAM_CHAT_ID", "") else env.get("TELEGRAM_CHAT_ID", ""),
            "discord_token_hint": mask(dc_token),
            "discord_channel_id": "" if "your_" in env.get("DISCORD_CHANNEL_ID", "") else env.get("DISCORD_CHANNEL_ID", ""),
            "interval": get_interval(),
            "auto_send": get_auto_send(),
        })
    data = request.get_json(silent=True) or {}
    updates = {}
    mapping = {
        "telegram_token": "TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
        "discord_token": "DISCORD_BOT_TOKEN",
        "discord_channel_id": "DISCORD_CHANNEL_ID",
    }
    for field, key in mapping.items():
        value = (data.get(field) or "").strip()
        if value:
            updates[key] = value
    if "interval" in data:
        try:
            updates["FETCH_INTERVAL_MINUTES"] = str(max(5, int(data["interval"])))
        except (TypeError, ValueError):
            pass
    if "auto_send" in data:
        updates["AUTO_SEND"] = "1" if str(data["auto_send"]) in ("1", "true", "on") else "0"
    if updates:
        update_env(updates)
        log.info("Cap nhat cau hinh: %s", ", ".join(sorted(updates)))
    return jsonify({"ok": True})


@app.route("/api/test/<kind>", methods=["POST"])
def api_test(kind):
    if kind not in ("telegram", "discord"):
        return jsonify({"error": "Kênh không hợp lệ"}), 404
    if not channel_ready(kind):
        return jsonify({"ok": False, "detail": "Chưa cấu hình token/ID cho kênh này"}), 400
    text = f"[Kiểm tra] Bot tin tức hoạt động bình thường - {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    env = get_env()
    if kind == "telegram":
        r = requests.post(
            f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage",
            json={"chat_id": env["TELEGRAM_CHAT_ID"], "text": text},
            timeout=15,
        )
        ok = r.ok and r.json().get("ok", False)
        detail = "" if ok else r.text[:200]
    else:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{env['DISCORD_CHANNEL_ID']}/messages",
            headers={"Authorization": f"Bot {env['DISCORD_BOT_TOKEN']}"},
            json={"content": text},
            timeout=15,
        )
        ok = r.status_code in (200, 204)
        detail = "" if ok else r.text[:200]
    level = logging.INFO if ok else logging.ERROR
    log.log(level, "Test %s: %s", kind, "OK" if ok else detail)
    return jsonify({"ok": ok, "detail": detail}), (200 if ok else 502)


@app.route("/api/logs")
def api_logs():
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        content = ""
    lines = content.splitlines()[-200:]
    return jsonify({"logs": "\n".join(lines)})


if __name__ == "__main__":
    if not load_store(NEWS_STORE, []):
        backfill_display()
    if not md.load_market():
        md.update_market()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=tb.polling_worker, kwargs={"fetch_callback": trigger_fetch}, daemon=True).start()
    print("\n  Bot Tin Tức Tổng Hợp đang chạy.")
    print("  Mở trình duyệt và truy cập:  http://127.0.0.1:8787")
    print("  Lệnh Telegram: /latest /search /market /status /sources /fetch\n")
    app.run(host="127.0.0.1", port=8787, debug=False)
