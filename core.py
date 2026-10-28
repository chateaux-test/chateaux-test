"""Shop logic that does not depend on FastAPI or aiogram, so it is easy to test."""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import closing
from urllib.parse import parse_qsl

DB_PATH = os.environ.get("DB_PATH", "shop.db")

# Prices live on the server, so nobody can change them from the page.
CATALOG = {  # id: (name, price in RUB)
    "cz": ("Czechia", 400), "pl": ("Poland", 280), "gb": ("United Kingdom", 170),
    "fr": ("France", 260), "de": ("Germany", 250), "nl": ("Netherlands", 260),
    "es": ("Spain", 270), "ee": ("Estonia", 250), "jp": ("Japan", 300),
    "bd": ("Bangladesh", 100), "co": ("Colombia", 130), "us": ("United States", 70),
    "us2": ("United States (spam block)", 40),
}
TIERS = [(7, 10), (3, 5)]      # (paid orders, discount %), biggest first
PROMOS = {"WELCOME5": 5}       # promo codes, percent. They stay on the server, nobody can read them
STATUSES = ("new", "paid", "delivered", "cancelled")
METHODS = ("stars", "card")
MAX_PENDING = 5      # unpaid orders one customer may have at the same time (stops order spam)
PROMO_TRIES = 10     # wrong promo codes allowed per customer per hour (stops code guessing)
_promo_fails = {}    # tg_id -> times of wrong guesses (kept in memory)


class AuthError(Exception):
    pass


class OrderError(Exception):
    pass


# ---------- proving who the user is ----------
def _check_string(pairs, skip):
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()) if k not in skip)


def verify_init_data(init_data, bot_token, max_age=86400, now=None):
    """Mini App: check the signature Telegram puts on initData. Returns the user dict."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise AuthError("bad initData")
    got = pairs.get("hash")
    if not got:
        raise AuthError("no hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    ok = False
    # Telegram added an extra "signature" field at some point; accept the check string with or without it.
    # Both need the bot token, so this does not make forging any easier.
    for skip in ({"hash"}, {"hash", "signature"}):
        calc = hmac.new(secret, _check_string(pairs, skip).encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calc, got):
            ok = True
            break
    if not ok:
        raise AuthError("bad signature")
    try:
        age = (now or time.time()) - int(pairs.get("auth_date", "0"))
    except ValueError:
        raise AuthError("bad auth_date")
    if age > max_age:
        raise AuthError("expired")
    try:
        user = json.loads(pairs["user"])
    except (KeyError, ValueError):
        raise AuthError("no user")
    if not isinstance(user, dict) or "id" not in user:
        raise AuthError("no user id")
    return user


def verify_login_widget(data, bot_token, max_age=86400, now=None):
    """Plain website: check the data from the Telegram Login Widget. Returns the user dict."""
    data = {k: str(v) for k, v in dict(data).items() if v is not None}
    got = data.pop("hash", None)
    if not got:
        raise AuthError("no hash")
    secret = hashlib.sha256(bot_token.encode()).digest()
    calc = hmac.new(secret, _check_string(data, set()).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        raise AuthError("bad signature")
    try:
        age = (now or time.time()) - int(data.get("auth_date", "0"))
        uid = int(data["id"])
    except (KeyError, ValueError):
        raise AuthError("bad data")
    if age > max_age:
        raise AuthError("expired")
    return {"id": uid, "first_name": data.get("first_name", ""), "username": data.get("username")}


# ---------- database ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  room INTEGER PRIMARY KEY AUTOINCREMENT,          -- Room No.1, 2, 3 ... one per customer
  tg_id INTEGER UNIQUE NOT NULL, username TEXT, first_name TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,            -- order No.1, 2, 3 ...
  tg_id INTEGER NOT NULL, items TEXT NOT NULL, total INTEGER NOT NULL, pct INTEGER NOT NULL,
  method TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at INTEGER NOT NULL);
"""


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with closing(db()) as c:
        c.executescript(SCHEMA)


def upsert_user(u):
    uid = int(u["id"])
    with closing(db()) as c, c:
        c.execute(
            "INSERT INTO users(tg_id, username, first_name, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (uid, u.get("username"), u.get("first_name"), int(time.time())))
        return dict(c.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone())


def loyalty_pct(tg_id):
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) FROM orders WHERE tg_id=? AND status IN ('paid','delivered')", (tg_id,)).fetchone()[0]
    return next((p for need, p in TIERS if n >= need), 0)


def promo_pct(code):
    return PROMOS.get((code or "").strip().upper(), 0)


def promo_check(tg_id, code, now=None):
    """Percent for a promo code, or 0. Too many wrong guesses lock the check for an hour."""
    now = now or time.time()
    fails = [t for t in _promo_fails.get(tg_id, []) if now - t < 3600]
    if len(fails) >= PROMO_TRIES:
        raise OrderError("too many attempts, try later")
    pct = promo_pct(code)
    if not pct:
        fails.append(now)
    _promo_fails[tg_id] = fails
    return pct


def price_cart(items, promo, loyalty):
    """Recalculate the price on the server. The page's numbers are never trusted."""
    if not isinstance(items, list) or not items or len(items) > 20:
        raise OrderError("empty cart")
    lines, subtotal = [], 0
    for it in items:
        try:
            pid, qty = it[0], int(it[1])
        except (TypeError, ValueError, IndexError):
            raise OrderError("bad item")
        if pid not in CATALOG or not 1 <= qty <= 20:
            raise OrderError("bad item")
        lines.append([pid, qty])
        subtotal += CATALOG[pid][1] * qty
    pct = max(loyalty, promo_pct(promo))
    return lines, round(subtotal * (100 - pct) / 100), pct


def _full(r):
    if not r:
        return None
    d = dict(r)
    d["items"] = json.loads(d["items"])
    return d


_JOIN = "SELECT o.*, u.username, u.first_name, u.room FROM orders o JOIN users u ON u.tg_id=o.tg_id"


def get_order(order_id):
    with closing(db()) as c:
        return _full(c.execute(_JOIN + " WHERE o.id=?", (order_id,)).fetchone())


def public(o):
    return {"id": o["id"], "items": o["items"], "total": o["total"], "pct": o["pct"],
            "method": o["method"], "status": o["status"], "ts": o["created_at"] * 1000}


def list_orders(tg_id, limit=50):
    with closing(db()) as c:
        rows = c.execute(_JOIN + " WHERE o.tg_id=? ORDER BY o.id DESC LIMIT ?", (tg_id, limit)).fetchall()
    return [public(_full(r)) for r in reversed(rows)]      # oldest first


def recent_orders(limit=20):
    with closing(db()) as c:
        return [_full(r) for r in c.execute(_JOIN + " ORDER BY o.id DESC LIMIT ?", (limit,)).fetchall()]


def create_order(tg_id, items, method, promo, now=None):
    lines, total, pct = price_cart(items, promo, loyalty_pct(tg_id))
    method = method if method in METHODS else "stars"
    now = int(now or time.time())
    dup = False
    with closing(db()) as c, c:
        same = c.execute("SELECT id FROM orders WHERE tg_id=? AND status='new' AND items=? AND method=? AND created_at>=? ORDER BY id DESC LIMIT 1",
                         (tg_id, json.dumps(lines), method, now - 15)).fetchone()
        pending = c.execute("SELECT COUNT(*) FROM orders WHERE tg_id=? AND status='new'", (tg_id,)).fetchone()[0]
        if same:                                   # double tap: give back the order that was just created
            oid, dup = same["id"], True
        elif pending >= MAX_PENDING:
            raise OrderError("too many unpaid orders, wait until they are confirmed")
        else:
            oid = c.execute("INSERT INTO orders(tg_id, items, total, pct, method, created_at) VALUES(?,?,?,?,?,?)",
                            (tg_id, json.dumps(lines), total, pct, method, now)).lastrowid
    order = get_order(oid)
    if order is None:
        raise OrderError("unknown customer")
    order["dup"] = dup
    return order


def set_status(order_id, status):
    if status not in STATUSES:
        return None
    with closing(db()) as c, c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    return get_order(order_id)
