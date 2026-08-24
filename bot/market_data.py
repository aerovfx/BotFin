import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

BASE = Path(__file__).parent
MARKET_FILE = BASE / "market.json"
URL = "https://banggia.cafef.vn/stockhandler.ashx?center=1&index=true&curFloorCode=HOSE"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://s.cafef.vn/"}
HISTORY_KEEP = 90
NAME_VI = {
    "VNINDEX": "VN-Index",
    "VN30": "VN30",
    "HNXINDEX": "HNX-Index",
    "HNXUPCOMINDEX": "UPCOM",
    "HNX30": "HNX30",
}
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
EXTRA_SYMBOLS = [
    ("USDVND=X", "Tỷ giá USD/VND", ""),
    ("GC=F", "Vàng quốc tế", "USD/oz"),
    ("XAUUSD=X", "Vàng quốc tế", "USD/oz"),
    ("CL=F", "Dầu WTI", "USD/thùng"),
    ("BZ=F", "Dầu Brent", "USD/thùng"),
]


def fetch_indices():
    r = requests.get(URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    items = []
    for row in r.json():
        name = row.get("name") or ""
        if not name:
            continue
        try:
            pct = float(row.get("percent") or 0)
            change = float(str(row.get("change") or "0").replace(",", ""))
        except ValueError:
            pct, change = 0.0, 0.0
        items.append({
            "symbol": NAME_VI.get(name, name),
            "value": row.get("index") or "",
            "change": change,
            "pct": pct,
            "volume": row.get("volume") or "",
        })
    return items


def fetch_extras():
    logger = logging.getLogger("newsbot")
    out = []
    for sym, name, unit in EXTRA_SYMBOLS:
        try:
            r = requests.get(YAHOO_URL.format(quote(sym)), headers=YAHOO_HEADERS, timeout=15)
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            pct = (price / float(prev) - 1) * 100 if prev else 0.0
            if name in {o["symbol"] for o in out}:
                continue
            out.append({
                "symbol": name,
                "value": round(price, 2),
                "pct": round(pct, 2),
                "unit": unit,
            })
        except Exception as exc:
            logger.warning("Khong lay duoc %s: %s", sym, exc)
    return out


def update_market():
    logger = logging.getLogger("newsbot")
    try:
        items = fetch_indices()
    except Exception as exc:
        logger.warning("Khong lay duoc so lieu chi so: %s", exc)
        items = []
    extras = fetch_extras()
    if not items and not extras:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    try:
        old = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
        history = old.get("history", [])
    except Exception:
        history = []
    history.append({"at": now, "indices": items})
    snap = {"updated_at": now, "indices": items, "extras": extras, "history": history[-HISTORY_KEEP:]}
    MARKET_FILE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    logger.info("Cap nhat so lieu thi truong (%d chi so, %d loai ngoai te)", len(items), len(extras))
    return snap


def load_market():
    try:
        return json.loads(MARKET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
