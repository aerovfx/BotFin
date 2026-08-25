import logging
import re
import time
from datetime import datetime
from pathlib import Path

import requests

import fetch_news as fn
import personalization as pers
import ranking as rk

log = logging.getLogger("newsbot")

BASE = Path(__file__).parent
NEWS_FILE = BASE / "news.json"
MARKET_FILE = BASE / "market.json"
STATS_FILE = BASE / "stats.json"

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 25
MAX_TEXT = 3900


def _api(token, method, **params):
    resp = requests.get(API.format(token=token, method=method), params=params, timeout=POLL_TIMEOUT + 10)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', resp.status_code)}")
    return data["result"]


def send_text(token, chat_id, text):
    text = text.strip()
    for start in range(0, len(text), MAX_TEXT):
        try:
            _api(token, "sendMessage", chat_id=chat_id, text=text[start:start + MAX_TEXT], disable_web_page_preview=True)
        except Exception as exc:
            log.warning("telegram reply %s: %s", chat_id, exc)
            return
        time.sleep(0.4)


def load_news():
    return fn.load_json(NEWS_FILE, [])


def fmt_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M %d/%m")
    except ValueError:
        return ""


def cmd_help(_args):
    return (
        "BotFintech — lệnh hỗ trợ:\n"
        "/latest [số] — tin mới nhất (mặc định 5)\n"
        "/top [số] — tin nóng nhất theo điểm xếp hạng\n"
        "/search <từ khóa> — tìm trong tiêu đề và tóm tắt\n"
        "/market — bảng chỉ số thị trường\n"
        "/status — trạng thái hoạt động của bot\n"
        "/sources — danh sách nguồn RSS\n"
        "/fetch — lấy tin mới ngay lập tức"
    )


def _score_prefix(item):
    score = item.get("score")
    if score is None:
        return ""
    return f"[{'🔥' if score >= 70 else ''}{score}] "


def cmd_latest(args):
    news = load_news()
    if not news:
        return "Chưa có tin nào trong kho."
    try:
        limit = max(1, min(int(args[0]), 10)) if args else 5
    except ValueError:
        limit = 5
    items = []
    for item in reversed(news[-50:]):
        stamp = fmt_time(item.get("fetched_at", ""))
        prefix = f"[{stamp}] " if stamp else ""
        cat = item.get("category", "")
        cat_tag = f"[{cat}] " if cat else ""
        items.append(f"{prefix}{_score_prefix(item)}{cat_tag}[{item['source']}] {item['title']}\n{item['link']}")
    chunk = "\n\n".join(items[-limit:])
    return f"{min(limit, len(items))} tin mới nhất:\n\n{chunk}"


def cmd_top(args):
    news = [i for i in load_news() if i.get("score") is not None]
    if not news:
        return "Chưa có tin nào được chấm điểm. Chờ chu kỳ lấy tin kế tiếp."
    try:
        limit = max(1, min(int(args[0]), 10)) if args else 5
    except ValueError:
        limit = 5
    top = sorted(news, key=lambda i: i.get("score", 0), reverse=True)[:limit]
    body = "\n\n".join(
        f"[🔥{i['score']}] [{i.get('category', 'Khác')}] [{i['source']}] {i['title']}\n{i['link']}"
        for i in top
    )
    return f"Top {len(top)} tin nóng:\n\n{body}"


def cmd_search(args):
    if not args:
        return "Dùng: /search <từ khóa> — ví dụ /search lãi suất"
    keyword = " ".join(args).lower()
    hits = [
        i for i in load_news()
        if keyword in i.get("title", "").lower() or keyword in i.get("summary", "").lower()
    ]
    if not hits:
        return f"Không tìm thấy tin nào chứa \"{keyword}\"."
    shown = hits[-5:]
    body = "\n\n".join(
        f"[{i['source']}] {i['title']}\n{i['link']}" for i in shown
    )
    extra = f"\n\n(và {len(hits) - 5} tin khác)" if len(hits) > 5 else ""
    return f"Tìm được {len(hits)} tin cho \"{keyword}\":\n\n{body}{extra}"


def cmd_market(_args):
    snap = fn.load_json(MARKET_FILE, {})
    if not snap:
        return "Chưa có dữ liệu thị trường, thử /fetch để cập nhật."
    lines = [f"Bảng giá {fmt_time(snap.get('updated_at', ''))}:"]
    rows = [(i.get("symbol"), i.get("value"), i.get("pct", 0)) for i in snap.get("indices", [])]
    rows += [(i.get("symbol"), i.get("value"), i.get("pct", 0)) for i in snap.get("extras", [])]
    for symbol, value, pct in rows:
        arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "•")
        sign = "+" if pct > 0 else ""
        lines.append(f"{arrow} {symbol}: {value} ({sign}{pct}%)")
    return "\n".join(lines)


def cmd_status(_args):
    stats = fn.load_json(STATS_FILE, {})
    env = fn.load_env(fn.ENV_FILE)
    try:
        interval = max(5, int(env.get("FETCH_INTERVAL_MINUTES", "30")))
    except ValueError:
        interval = 30
    auto = env.get("AUTO_SEND", "0") == "1"
    tg_ok = bool(env.get("TELEGRAM_BOT_TOKEN")) and "your_" not in env.get("TELEGRAM_BOT_TOKEN", "")
    dc_ok = bool(env.get("DISCORD_BOT_TOKEN")) and "your_" not in env.get("DISCORD_BOT_TOKEN", "")
    lines = [
        "Trạng thái BotFintech:",
        f"- Chu kỳ lấy tin: {interval} phút",
        f"- Tự động đẩy tin: {'bật' if auto else 'tắt (Safe Mode)'}",
        f"- Telegram: {'sẵn sàng' if tg_ok else 'chưa cấu hình'} | Discord: {'sẵn sàng' if dc_ok else 'chưa cấu hình'}",
        f"- Nguồn RSS: {len(fn.load_json(fn.SOURCES_FILE, []))}",
        f"- Tin trong kho: {len(load_news())}",
        f"- Số lần chạy: {stats.get('runs', 0)} | lần cuối: {fmt_time(stats.get('last_run', '')) or '-'}",
        f"- Xếp hạng lần cuối: đẩy {stats.get('last_push', '-')} / giữ {stats.get('last_held', '-')} | điểm cao nhất: {stats.get('last_top_score', '-')}",
    ]
    return "\n".join(lines)


def cmd_sources(_args):
    sources = fn.load_json(fn.SOURCES_FILE, [])
    if not sources:
        return "Danh sách nguồn trống."
    lines = [f"{idx + 1}. [{s.get('category', 'Khác')}] {s['name']}" for idx, s in enumerate(sources)]
    return "Nguồn RSS hiện có:\n" + "\n".join(lines)


def format_fetch_result(result):
    if "error" in result:
        return f"Không lấy được tin: {result['error']}"
    if result.get("new", 0) == 0:
        return f"Không có tin mới ({result.get('sources_ok', 0)} nguồn OK)."
    errors = "".join(f"\n- {e}" for e in result.get("errors", [])[:3])
    return (
        f"Đã lấy {result['new']} tin mới từ {result.get('sources_ok', 0)} nguồn.\n"
        f"Telegram: {result.get('telegram', '-')} | Discord: {result.get('discord', '-')}"
        f"{errors}"
    )


COMMANDS = {
    "start": cmd_help,
    "help": cmd_help,
    "latest": cmd_latest,
    "top": cmd_top,
    "search": cmd_search,
    "market": cmd_market,
    "status": cmd_status,
    "sources": cmd_sources,
}


def allowed_chat(env, chat_id):
    configured = env.get("TELEGRAM_CHAT_ID", "").strip()
    return not configured or str(chat_id) == configured


def bind_chat(env, chat_id):
    """Ghi nhận người nhắn đầu tiên làm kênh chính khi .env chưa có CHAT_ID."""
    lines = fn.ENV_FILE.read_text(encoding="utf-8").splitlines() if fn.ENV_FILE.exists() else ["TELEGRAM_BOT_TOKEN="]
    out = []
    found = False
    for line in lines:
        if line.strip().startswith("TELEGRAM_CHAT_ID"):
            out.append(f"TELEGRAM_CHAT_ID={chat_id}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"TELEGRAM_CHAT_ID={chat_id}")
    fn.ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    log.info("Telegram: gan chat ID moi (%s)", str(chat_id)[-4:])


def handle_callback(token, env, cb):
    chat_id = (cb.get("message") or {}).get("chat") or {}
    chat_id = chat_id.get("id")
    data = cb.get("data", "")
    if not chat_id or not data.startswith("fb:"):
        return
    if not allowed_chat(env, chat_id):
        return
    action = {"u": "open", "d": "down"}.get(data[3:4])
    applied = pers.record_hash(data[5:], action) if action else False
    try:
        _api(token, "answerCallbackQuery", callback_query_id=cb.get("id", ""),
             text=("Đã ghi nhận 👍" if action == "open" else "Đã ghi nhận 👎") if applied else "Đã phản hồi trước đó")
    except Exception as exc:
        log.warning("answerCallbackQuery: %s", exc)
    msg = cb.get("message") or {}
    try:
        _api(token, "editMessageReplyMarkup", chat_id=msg.get("chat", {}).get("id"),
             message_id=msg.get("message_id"), reply_markup="")
    except Exception as exc:
        log.warning("editMessageReplyMarkup: %s", exc)


def handle_update(token, env, update, fetch_callback):
    cb = update.get("callback_query")
    if cb:
        handle_callback(token, env, cb)
        return
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return
    if not allowed_chat(env, chat_id):
        log.warning("Telegram: bo qua tin tu chat khong hop le (%s)", str(chat_id)[-4:])
        return

    parts = text.split(maxsplit=1)
    command = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1].split() if len(parts) > 1 else []

    if command == "fetch":
        send_text(token, chat_id, "Đang lấy tin...")
        result = fetch_callback() if fetch_callback else {"error": "chức năng chưa sẵn sàng"}
        send_text(token, chat_id, format_fetch_result(result))
        return

    handler = COMMANDS.get(command)
    if handler:
        send_text(token, chat_id, handler(args))
    else:
        send_text(token, chat_id, f"Lệnh không rõ: /{command}\n\n" + cmd_help([]))


def polling_worker(fetch_callback=None):
    """Vòng lặp dài hạn: đợi lệnh từ Telegram. Chờ im lặng khi chưa có token."""
    announced = False
    offset = None
    while True:
        env = fn.load_env(fn.ENV_FILE)
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or "your_" in token or re.match(r"^https?://", token):
            if not announced:
                log.info("Telegram commands: chua cau hinh token, o che doi cho")
                announced = True
            time.sleep(30)
            continue
        try:
            updates = _api(token, "getUpdates", offset=offset or 0, timeout=POLL_TIMEOUT) or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(token, env, update, fetch_callback)
                except Exception:
                    log.exception("Xu ly lenh Telegram loi")
        except Exception as exc:
            log.warning("Telegram poll: %s", exc)
            time.sleep(10)
