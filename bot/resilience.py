"""Phần 6 — Kháng lỗi: theo dõi sức khỏe nguồn, cảnh báo khi nguồn chết/hồi phục.

- Mỗi lần lấy tin (và mỗi vòng health-check định kỳ) ghi kết quả vào
  source_health.json: số lỗi liên tiếp, lỗi cuối, lần OK cuối.
- Lỗi liên tiếp đạt ngưỡng (mặc định 3) → nguồn được đánh dấu "chết",
  bắn sự kiện cảnh báo kênh đúng một lần.
- Nguồn chết nhận được phản hồi thành công → sự kiện "hồi phục".
- health_worker chạy nền giữa các chu kỳ lấy tin, probe nhẹ (stream GET).
"""
import json
import logging
import sys
import time

import requests

import fetch_news as fn

log = logging.getLogger("newsbot")

HEALTH_FILE = fn.BASE / "source_health.json"


def config(env=None):
    env = env or fn.load_env(fn.ENV_FILE)

    def _int(key, default):
        try:
            return max(1, int(env.get(key, str(default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": env.get("RESILIENCE", "1") != "0",
        "check_minutes": _int("HEALTH_CHECK_MINUTES", 20),
        "threshold": _int("SOURCE_FAILURE_THRESHOLD", 3),
    }


def load_health():
    data = fn.load_json(HEALTH_FILE, {})
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_health(health):
    HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=1), encoding="utf-8")


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def note(name, ok, error=""):
    """Ghi kết quả một lần chạm nguồn. Trả về list sự kiện dead/revived mới phát sinh."""
    cfg = config()
    if not cfg["enabled"]:
        return []
    health = load_health()
    rec = health.get(name, {})
    events = []
    now = time.time()

    if ok:
        if rec.get("was_dead"):
            events.append({"type": "revived", "name": name})
        rec["consecutive_failures"] = 0
        rec["was_dead"] = False
        rec["last_ok"] = _iso(now)
        rec.pop("last_error", None)
    else:
        rec["consecutive_failures"] = rec.get("consecutive_failures", 0) + 1
        rec["last_error"] = str(error)[:200]
        rec["last_fail"] = _iso(now)
        if rec["consecutive_failures"] >= cfg["threshold"] and not rec.get("was_dead"):
            rec["was_dead"] = True
            events.append({"type": "dead", "name": name, "error": rec["last_error"]})

    health[name] = rec
    try:
        save_health(health)
    except OSError as exc:
        print(f"[warn] khong luu duoc {HEALTH_FILE.name}: {exc}", file=sys.stderr)
    return events


def snapshot(sources=None):
    """Trạng thái mọi nguồn cho dashboard: ok / degraded / dead / unknown."""
    sources = sources if sources is not None else fn.load_json(fn.SOURCES_FILE, [])
    health = load_health()
    out = []
    for source in sources:
        rec = health.get(source.get("name"), {})
        if not rec:
            out.append({"name": source.get("name"), "state": "unknown", "failures": 0,
                        "last_error": "", "last_ok": ""})
            continue
        failures = rec.get("consecutive_failures", 0)
        state = "dead" if rec.get("was_dead") else ("degraded" if failures > 0 else "ok")
        out.append({
            "name": source.get("name"),
            "state": state,
            "failures": failures,
            "last_error": rec.get("last_error", ""),
            "last_ok": rec.get("last_ok", ""),
        })
    return out


def dead_names():
    return [rec["name"] for rec in snapshot() if rec["state"] == "dead"]


def format_events(events):
    messages = []
    for event in events:
        if event["type"] == "dead":
            messages.append(
                f"⚠️ NGUỒN CHẾT: {event['name']}\nLỗi: {event.get('error', '')[:150]}\nBot sẽ báo khi nguồn hồi phục."
            )
        elif event["type"] == "revived":
            messages.append(f"✅ NGUỒN HỒI PHỤC: {event['name']}")
    return messages


def probe_once():
    """Chạm nhẹ từng nguồn (GET stream, không parse RSS). Trả về sự kiện phát sinh."""
    events = []
    for source in fn.load_json(fn.SOURCES_FILE, []):
        try:
            resp = requests.get(source["url"], headers=fn.HEADERS, timeout=(4, 8), stream=True)
            resp.close()
            ok = resp.status_code < 400
            error = f"HTTP {resp.status_code}" if not ok else ""
        except requests.RequestException as exc:
            ok, error = False, str(exc)[:120]
        log.debug("health %s: %s", source["name"], "ok" if ok else error)
        events += note(source["name"], ok, error)
        time.sleep(0.3)
    return events


def health_worker(sender=None):
    """Vòng lặp nền: probe định kỳ và đẩy cảnh báo qua sender(messages)."""
    announced_disabled = False
    while True:
        cfg = config()
        if cfg["enabled"]:
            announced_disabled = False
            try:
                handle_events(probe_once(), sender)
            except Exception:
                log.exception("Health check loi")
        elif not announced_disabled:
            log.info("Resilience tat (RESILIENCE=0), bo qua health check")
            announced_disabled = True
        time.sleep(cfg["check_minutes"] * 60)


def handle_events(events, sender=None):
    messages = format_events(events)
    for message in messages:
        log.warning(message.splitlines()[0])
    if messages and sender:
        try:
            sender(messages)
        except Exception:
            log.exception("Khong gui duoc canh bao nguon")
