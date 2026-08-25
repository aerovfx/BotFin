import argparse
import json
import re
import sys
import time
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
SOURCES_FILE = BASE / "sources.json"
STATE_FILE = BASE / "seen.json"
NEWS_FILE = BASE / "news.txt"
ENV_FILE = BASE / ".env"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
STATE_MAX_AGE = 7 * 86400


def load_env(path):
    if not path.exists():
        return {}
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[warn] {path.name} khong hop le JSON, dung gia tri mac dinh", file=sys.stderr)
        return default


def clean_html(text):
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def fetch_feed(source):
    resp = requests.get(source["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    category = source.get("category", "Khác")
    items = []
    for entry in parsed.entries:
        link = getattr(entry, "link", "").strip()
        title = clean_html(getattr(entry, "title", ""))
        summary = clean_html(getattr(entry, "summary", ""))[:300]
        published = getattr(entry, "published", "")
        if link and title:
            items.append({
                "source": source["name"],
                "category": category,
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
            })
    return items


def format_item(item):
    cat = item.get("category", "")
    ai_tag = "[AI] " if item.get("ai_summary") else ""
    score = item.get("score")
    score_tag = f"[{'🔥' if score >= 70 else ''}{score}] " if score is not None else ""
    cat_tag = f"[{cat}] " if cat else ""
    lines = [f"{score_tag}{ai_tag}{cat_tag}[{item['source']}] {item['title']}", item["link"]]
    summary = item.get("ai_summary") or item["summary"]
    if summary:
        lines.append(summary)
    return "\n".join(lines)


def send_telegram(env, messages):
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or "your_" in token:
        print("[skip] Telegram chua cau hinh trong .env")
        return 0
    ok = 0
    for msg in messages:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.ok:
            ok += 1
        else:
            print(f"[telegram] loi {r.status_code}: {r.text[:200]}", file=sys.stderr)
        time.sleep(0.5)
    print(f"[telegram] da gui {ok}/{len(messages)} tin")
    return ok


def send_discord(env, messages):
    token = env.get("DISCORD_BOT_TOKEN", "")
    channel_id = env.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel_id or "your_" in token:
        print("[skip] Discord chua cau hinh trong .env")
        return 0
    ok = 0
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    for msg in messages:
        r = requests.post(url, headers=headers, json={"content": msg}, timeout=15)
        if r.ok:
            ok += 1
        else:
            print(f"[discord] loi {r.status_code}: {r.text[:200]}", file=sys.stderr)
        time.sleep(0.5)
    print(f"[discord] da gui {ok}/{len(messages)} tin")
    return ok


def save_news_file(items):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"== Tin tuc tong hop {now} =="]
    for item in items:
        lines.append("")
        lines.append(format_item(item))
    NEWS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Bot lay tin kinh te tu RSS")
    parser.add_argument("--dry-run", action="store_true", help="Chi lay va hien thi, khong gui")
    parser.add_argument("--limit", type=int, default=10, help="So tin moi toi da moi nguon")
    args = parser.parse_args()

    env = load_env(ENV_FILE)
    sources = load_json(SOURCES_FILE, [])
    state = load_json(STATE_FILE, {})

    new_items = []
    for source in sources:
        try:
            fetched = fetch_feed(source)
        except Exception as exc:
            print(f"[loi] {source['name']}: {exc}", file=sys.stderr)
            continue
        fresh = [i for i in fetched if i["link"] not in state][: args.limit]
        print(f"{source['name']}: {len(fetched)} tin RSS, {len(fresh)} tin moi")
        new_items.extend(fresh)

    if not new_items:
        print("Khong co tin moi.")
        return

    for item in new_items:
        state[item["link"]] = time.time()
    cutoff = time.time() - STATE_MAX_AGE
    state = {k: v for k, v in state.items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    messages = [format_item(i) for i in new_items]
    save_news_file(new_items)
    print(f"\nTong cong {len(messages)} tin moi. Xem chi tiet trong {NEWS_FILE.name}:\n")
    for msg in messages[:15]:
        print(msg)
        print("-" * 60)

    if args.dry_run:
        print("[dry-run] Khong gui di bat ky kenh nao.")
        return
    send_telegram(env, messages)
    send_discord(env, messages)


if __name__ == "__main__":
    main()
