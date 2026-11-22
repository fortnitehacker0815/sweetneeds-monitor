#!/usr/bin/env python3
"""
Sweetneeds Shop-Monitor
--------------------
Lädt den kompletten Produktkatalog von sweetneeds.de (Shopify /products.json),
vergleicht ihn mit dem letzten Stand und erkennt:

  - neue Produkte / Varianten
  - Preissenkungen und Preiserhöhungen
  - neue / geänderte Streichpreise (Rabatte)
  - ausverkauft / wieder verfügbar
  - entfernte Produkte

Ergebnisse landen in docs/data/ (wird vom Dashboard gelesen).
Benachrichtigungen optional über ntfy.sh, Telegram und/oder E-Mail (SMTP),
gesteuert über Umgebungsvariablen (siehe README).

Nur Python-Standardbibliothek – keine Installation nötig.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

SHOP = os.environ.get("SHOP_URL", "https://sweetneeds.de").rstrip("/")
SHOP_NAME = os.environ.get("SHOP_NAME") or "Sweetneeds"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
STATE_FILE = DATA / "state.json"        # interner Stand inkl. Preisverlauf
CATALOG_FILE = DATA / "catalog.json"    # aktueller Katalog fürs Dashboard
EVENTS_FILE = DATA / "events.json"      # Änderungs-Feed fürs Dashboard
META_FILE = DATA / "meta.json"          # Laufinfos

MAX_EVENTS = 5000
MAX_HISTORY = 100
USER_AGENT = "Mozilla/5.0 (compatible; shop-price-monitor/1.0)"


# --------------------------------------------------------------------------- #
# Laden
# --------------------------------------------------------------------------- #
def http_get_json(url: str, retries: int = 4) -> dict:
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 ** attempt * 3)
    raise RuntimeError(f"Abruf fehlgeschlagen: {url}: {last_err}")


def fetch_catalog() -> list[dict]:
    products, page = [], 1
    while True:
        data = http_get_json(f"{SHOP}/products.json?limit=250&page={page}")
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
        if page > 200:  # Sicherheitsbremse
            break
        time.sleep(1)
    return products


def to_cents(v) -> int | None:
    if v in (None, "", "0.00", "0"):
        return None
    return round(float(v) * 100)


def normalize(products: list[dict]) -> dict[str, dict]:
    """Eine Zeile pro Variante, Schlüssel = Varianten-ID."""
    out: dict[str, dict] = {}
    for p in products:
        img = p["images"][0]["src"] if p.get("images") else None
        multi = len(p.get("variants", [])) > 1
        for v in p.get("variants", []):
            title = p["title"]
            if multi and v.get("title") and v["title"] != "Default Title":
                title = f'{p["title"]} – {v["title"]}'
            price = to_cents(v.get("price"))
            compare = to_cents(v.get("compare_at_price"))
            if compare is not None and price is not None and compare <= price:
                compare = None
            vimg = (v.get("featured_image") or {}).get("src") or img
            out[str(v["id"])] = {
                "id": str(v["id"]),
                "product_id": str(p["id"]),
                "title": title,
                "vendor": p.get("vendor") or "",
                "type": p.get("product_type") or "",
                "url": f'{SHOP}/products/{p["handle"]}' + (f'?variant={v["id"]}' if multi else ""),
                "image": vimg,
                "price": price,
                "compare_at": compare,
                "available": bool(v.get("available")),
                "published_at": p.get("published_at"),
                "sku": v.get("sku") or "",
            }
    return out


# --------------------------------------------------------------------------- #
# Vergleich
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def eur(cents: int | None) -> str:
    if cents is None:
        return "–"
    return f"{cents / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def discount_pct(price, compare) -> int | None:
    if price and compare and compare > price:
        return round((1 - price / compare) * 100)
    return None


def diff(old: dict[str, dict], new: dict[str, dict], now: str) -> list[dict]:
    events: list[dict] = []

    def ev(kind: str, item: dict, **extra):
        e = {
            "ts": now,
            "type": kind,
            "id": item["id"],
            "title": item["title"],
            "url": item["url"],
            "image": item.get("image"),
            "price": item.get("price"),
            "compare_at": item.get("compare_at"),
        }
        e.update(extra)
        events.append(e)

    for vid, n in new.items():
        o = old.get(vid)
        if o is None:
            ev("new", n)
            continue
        if n["price"] != o["price"] and n["price"] is not None and o["price"] is not None:
            kind = "price_drop" if n["price"] < o["price"] else "price_up"
            ev(kind, n, old_price=o["price"],
               change_pct=round((n["price"] / o["price"] - 1) * 100, 1))
        elif n["compare_at"] != o["compare_at"] and n["compare_at"]:
            # Preis gleich, aber neuer/anderer Streichpreis -> neuer Rabatt sichtbar
            ev("discount", n, old_compare_at=o["compare_at"])
        if n["available"] != o["available"]:
            ev("back_in_stock" if n["available"] else "sold_out", n)

    for vid, o in old.items():
        if vid not in new:
            ev("removed", o)

    return events


# --------------------------------------------------------------------------- #
# Benachrichtigungen
# --------------------------------------------------------------------------- #
LABELS = {
    "new": "🆕 Neu",
    "price_drop": "📉 Preis gesenkt",
    "price_up": "📈 Preis erhöht",
    "discount": "🏷️ Neuer Rabatt",
    "back_in_stock": "✅ Wieder da",
    "sold_out": "⛔ Ausverkauft",
    "removed": "🗑️ Entfernt",
}


def notify_types() -> set[str]:
    # GitHub übergibt nicht gesetzte Variablen als "" → dann Standardwert nehmen
    raw = os.environ.get("NOTIFY_TYPES") or "new,price_drop,price_up,discount,back_in_stock"
    return {t.strip() for t in raw.split(",") if t.strip()}


def event_line(e: dict) -> str:
    label = LABELS.get(e["type"], e["type"])
    if e["type"] in ("price_drop", "price_up"):
        price = f'{eur(e["old_price"])} → {eur(e["price"])} ({e["change_pct"]:+.1f} %)'
    else:
        price = eur(e["price"])
        d = discount_pct(e["price"], e["compare_at"])
        if d:
            price += f" (−{d} % ggü. {eur(e['compare_at'])})"
    return f"{label}: {e['title']} – {price}"


def rank_events(events: list[dict]) -> list[dict]:
    """Wichtigstes zuerst: größte Preissenkung, dann höchster Rabatt."""
    def rank(e):
        if e.get("old_price") is not None and e.get("change_pct") is not None and e["change_pct"] < 0:
            return (0, e["change_pct"])
        return (1, -(discount_pct(e["price"], e["compare_at"]) or 0))
    return sorted(events, key=rank)


def push_title(e: dict) -> str:
    label = LABELS.get(e["type"], e["type"])
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        return f'{SHOP_NAME} · {label} {e["change_pct"]:+.0f} %'
    d = discount_pct(e["price"], e["compare_at"])
    return f"{SHOP_NAME} · {label}" + (f" −{d} %" if d else "")


def push_body(e: dict) -> str:
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        price = f'{eur(e["old_price"])} → {eur(e["price"])}'
    else:
        price = eur(e["price"])
        if e.get("compare_at") and discount_pct(e["price"], e["compare_at"]):
            price += f" (statt {eur(e['compare_at'])})"
    return f"{e['title']}\n{price}"


def ntfy_publish(payload: dict) -> bool:
    """Veröffentlicht per JSON (UTF-8-sicher, mit Klick-Link und Bild).
    Bei Drosselung durch ntfy (HTTP 429) oder kurzen Störungen: warten und erneut versuchen."""
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f'Bearer {os.environ["NTFY_TOKEN"]}'
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(6):
        try:
            req = urllib.request.Request(server + "/", data=data, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            time.sleep(5 * (attempt + 1))
    return False


def send_ntfy(events: list[dict]):
    """Jede Änderung als eigene Nachricht – niemals gesammelt.
    Tippen öffnet direkt die Produktseite. Versand in ruhigem Tempo,
    damit auch viele Änderungen auf einmal vollständig ankommen."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    failed = 0
    for i, e in enumerate(events):
        payload = {"topic": topic, "title": push_title(e), "message": push_body(e),
                   "click": e["url"], "tags": ["shopping_cart"]}
        if e.get("image"):
            payload["attach"] = e["image"]
        if not ntfy_publish(payload):
            failed += 1
        time.sleep(1.0 if i < 50 else 5.0)   # ntfy erlaubt ca. 60 auf einmal, danach etwa 1 pro 5 s
    print(f"  ntfy: {len(events) - failed} von {len(events)} Nachrichten zugestellt")
    if failed:
        print(f"Warnung: {failed} Nachrichten konnten nicht zugestellt werden", file=sys.stderr)


def text_digest(events: list[dict]) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    title = f"{SHOP_NAME}: " + ", ".join(f"{n}× {LABELS[k].split(' ', 1)[1]}" for k, n in counts.items())
    lines = [f"{event_line(e)}\n{e['url']}" for e in events[:40]]
    if len(events) > 40:
        lines.append(f"… und {len(events) - 40} weitere")
    return title, "\n\n".join(lines)


def send_telegram(title: str, body: str):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n\n{body}"[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()


def send_email(title: str, body: str):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not (host and to):
        return
    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("MAIL_FROM") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = title, sender, to
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or "587"), timeout=30) as s:
        s.starttls()
        if user:
            s.login(user, os.environ.get("SMTP_PASSWORD", ""))
        s.sendmail(sender, [a.strip() for a in to.split(",")], msg.as_string())


def select_for_notify(events: list[dict]) -> list[dict]:
    wanted = notify_types()
    events = [e for e in events if e["type"] in wanted]
    min_change = float(os.environ.get("MIN_PRICE_CHANGE_PCT") or "0")
    events = [e for e in events
              if e["type"] not in ("price_drop", "price_up") or abs(e["change_pct"]) >= min_change]
    min_disc = float(os.environ.get("MIN_DISCOUNT_PCT") or "0")
    if min_disc:
        events = [e for e in events if e["type"] != "new"
                  or (discount_pct(e["price"], e["compare_at"]) or 0) >= min_disc]
    return rank_events(events)


def notify(events: list[dict]):
    events = select_for_notify(events)
    if not events:
        return
    title, body = text_digest(events)
    for fn, args in ((send_ntfy, (events,)),
                     (send_telegram, (title, body)),
                     (send_email, (title, body))):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001  – Benachrichtigung darf Lauf nicht abbrechen
            print(f"Warnung: {fn.__name__} fehlgeschlagen: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Hauptablauf
# --------------------------------------------------------------------------- #
def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    raw = fetch_catalog()
    if not raw:
        print("Leerer Katalog erhalten – Abbruch, Stand bleibt unverändert.", file=sys.stderr)
        return 1
    current = normalize(raw)

    state = load_json(STATE_FILE, {"items": {}, "history": {}, "first_seen": {}})
    old_items: dict = state.get("items", {})
    history: dict = state.get("history", {})
    first_seen: dict = state.get("first_seen", {})
    baseline = not old_items

    # Schutz vor Fehlalarm: wenn plötzlich >50 % fehlen, ist eher der Abruf kaputt.
    if old_items and len(current) < 0.5 * len(old_items):
        print(f"Nur {len(current)} von {len(old_items)} Varianten geladen – Abbruch.", file=sys.stderr)
        return 1

    events = [] if baseline else diff(old_items, current, now)

    for vid, item in current.items():
        first_seen.setdefault(vid, now)
        h = history.setdefault(vid, [])
        if not h or h[-1][1] != item["price"] or h[-1][2] != item["compare_at"]:
            h.append([now, item["price"], item["compare_at"]])
            del h[:-MAX_HISTORY]

    all_events = load_json(EVENTS_FILE, [])
    if baseline:
        all_events.append({"ts": now, "type": "baseline", "count": len(current),
                           "title": f"Überwachung gestartet – {len(current)} Varianten erfasst"})
    all_events.extend(events)
    all_events = all_events[-MAX_EVENTS:]

    # Katalog fürs Dashboard
    catalog = []
    for vid, item in current.items():
        catalog.append({
            **item,
            "discount_pct": discount_pct(item["price"], item["compare_at"]),
            "first_seen": first_seen.get(vid),
            "history": history.get(vid, []),
        })
    catalog.sort(key=lambda x: x["title"].lower())

    state = {"items": current, "history": history, "first_seen": first_seen}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), "utf-8")
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), "utf-8")
    EVENTS_FILE.write_text(json.dumps(all_events, ensure_ascii=False, separators=(",", ":")), "utf-8")
    meta = load_json(META_FILE, {})
    meta.update({
        "shop": SHOP,
        "last_run": now,
        "last_change": now if (events or baseline) else meta.get("last_change"),
        "products": len({i["product_id"] for i in current.values()}),
        "variants": len(current),
        "last_run_events": len(events),
        "started": meta.get("started", now),
    })
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")

    print(f"{now}: {meta['products']} Produkte / {len(current)} Varianten, "
          f"{len(events)} Änderungen{' (Basislauf)' if baseline else ''}")
    for e in events[:50]:
        print("  " + event_line(e))

    if events:
        notify(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
