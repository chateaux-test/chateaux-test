"""chateaux shop: one process = the web server for the Mini App + the Telegram bot."""
import asyncio
import html
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import core

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
if not ADMIN_IDS:
    raise SystemExit("Set ADMIN_IDS (your numeric Telegram ID), otherwise nobody receives the orders")
CORS_ORIGIN = os.environ.get("CORS_ORIGIN") or "{0.scheme}://{0.netloc}".format(urlparse(WEBAPP_URL))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

STATUS_TEXT = {"new": "🕓 awaiting payment confirmation", "paid": "✅ paid, preparing",
               "delivered": "📦 delivered", "cancelled": "❌ cancelled"}


# ---------- message texts ----------
def mention(o):
    name = html.escape(o.get("first_name") or "no name")
    tag = f" (@{html.escape(o['username'])})" if o.get("username") else ""
    return f'<a href="tg://user?id={o["tg_id"]}">{name}</a>{tag}'


def card_text(o):
    items = ", ".join(f"{core.CATALOG.get(i, (i,))[0]} ×{q}" for i, q in o["items"])
    pct = f" (−{o['pct']}%)" if o["pct"] else ""
    return (f"<b>Order №{o['id']}</b>\nBuyer: {mention(o)}\nRoom №{o['room']} · TG ID <code>{o['tg_id']}</code>\n"
            f"{html.escape(items)}\nTotal: <b>{o['total']} ₽</b>{pct} · {o['method']}\n"
            f"Status: {STATUS_TEXT[o['status']]}")


def admin_kb(order_id):
    row = [InlineKeyboardButton(text=label, callback_data=f"st:{status}:{order_id}")
           for label, status in (("✅ Paid", "paid"), ("📦 Delivered", "delivered"), ("❌ Cancel", "cancelled"))]
    return InlineKeyboardMarkup(inline_keyboard=[row])


# ---------- bot ----------
@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍 Open shop", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Welcome to chateaux shop. Tap the button to open the shop.\nYour orders: /orders", reply_markup=kb)


@dp.message(Command("orders"))
async def my_orders(m: Message):
    rows = core.list_orders(m.from_user.id, limit=10)
    if not rows:
        await m.answer("You have no orders yet.")
        return
    await m.answer("\n".join(f"№{o['id']} · {o['total']} ₽ · {STATUS_TEXT[o['status']]}" for o in rows))


@dp.message(Command("all"))
async def all_orders(m: Message):                       # founder only
    if m.from_user.id not in ADMIN_IDS:
        return
    rows = core.recent_orders(20)
    if not rows:
        await m.answer("No orders yet.")
        return
    await m.answer("\n".join(
        f"№{o['id']} · {('@' + o['username']) if o.get('username') else html.escape(o.get('first_name') or str(o['tg_id']))}"
        f" · {o['total']} ₽ · {STATUS_TEXT[o['status']]}" for o in rows) + "\n\nDetails: /order 12")


@dp.message(Command("order"))
async def one_order(m: Message):                        # founder only: /order 12
    if m.from_user.id not in ADMIN_IDS:
        return
    parts = (m.text or "").split()
    o = core.get_order(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
    if not o:
        await m.answer("Usage: /order 12")
        return
    await m.answer(card_text(o), reply_markup=admin_kb(o["id"]))


@dp.callback_query(F.data.startswith("st:"))
async def change_status(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Not allowed", show_alert=True)
        return
    _, status, oid = cb.data.split(":")
    o = core.set_status(int(oid), status)
    if not o:
        await cb.answer("Order not found", show_alert=True)
        return
    await cb.answer("Updated")
    await cb.message.edit_text(card_text(o), reply_markup=admin_kb(o["id"]))
    try:
        await bot.send_message(o["tg_id"], f"Order №{o['id']}: {STATUS_TEXT[status]}")
    except Exception:
        pass                                            # the buyer has not started the bot yet


# ---------- web API for the Mini App ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    core.init_db()
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Shop", web_app=WebAppInfo(url=WEBAPP_URL)))
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[CORS_ORIGIN], allow_methods=["POST"], allow_headers=["Content-Type"])


class AuthIn(BaseModel):
    initData: str


class PromoIn(AuthIn):
    code: str


class OrderIn(AuthIn):
    items: list[list[Any]]
    method: str = "stars"
    promo: str = ""


def who(init_data: str, max_age: int = 86400) -> dict:
    try:
        return core.verify_init_data(init_data, BOT_TOKEN, max_age)
    except core.AuthError as e:
        raise HTTPException(401, str(e))


@app.post("/api/auth")
async def api_auth(body: AuthIn):
    u = core.upsert_user(who(body.initData))
    return {"user": {"id": u["tg_id"], "username": u["username"], "first_name": u["first_name"], "room": u["room"]},
            "loyalty": core.loyalty_pct(u["tg_id"]), "orders": core.list_orders(u["tg_id"])}


@app.post("/api/promo")
async def api_promo(body: PromoIn):
    u = who(body.initData)
    try:
        pct = core.promo_check(u["id"], body.code)
    except core.OrderError as e:
        raise HTTPException(429, str(e))
    return {"ok": pct > 0, "pct": pct}


@app.post("/api/orders")
async def api_order(body: OrderIn):
    u = core.upsert_user(who(body.initData, max_age=3600))      # ordering needs a fresh launch of the Mini App
    try:
        order = core.create_order(u["tg_id"], body.items, body.method, body.promo)
    except core.OrderError as e:
        raise HTTPException(429 if "too many" in str(e) else 400, str(e))
    if not order.get("dup"):                                      # a double tap must not notify twice
        for admin in ADMIN_IDS:
            try:
                await bot.send_message(admin, card_text(order), reply_markup=admin_kb(order["id"]))
            except Exception:
                pass
        try:
            await bot.send_message(u["tg_id"], f"Order №{order['id']} registered. We will confirm your payment here.")
        except Exception:
            pass
    return {"order": core.public(order)}


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
