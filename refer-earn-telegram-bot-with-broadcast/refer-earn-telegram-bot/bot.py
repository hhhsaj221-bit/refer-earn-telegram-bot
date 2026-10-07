"""Telegram Refer & Earn bot with forced subscription and manual UPI payouts."""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
LOGGER = logging.getLogger(__name__)

UPI_STATE, WITHDRAW_STATE, BROADCAST_STATE = range(3)
DB_PATH = Path(__file__).with_name("bot.db")
UPI_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9.-]{1,63}$")


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    channels: tuple[str, ...]
    referral_reward: int
    min_withdrawal: int
    currency: str
    support_username: str


def get_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    admins = frozenset(
        int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()
    )
    channels = tuple(
        value.strip() for value in os.getenv("REQUIRED_CHANNELS", "").split(",") if value.strip()
    )
    try:
        reward = int(os.getenv("REFERRAL_REWARD", "5"))
        minimum = int(os.getenv("MIN_WITHDRAWAL", "100"))
    except ValueError as error:
        raise RuntimeError("REFERRAL_REWARD and MIN_WITHDRAWAL must be whole numbers.") from error
    if not token or not admins or not channels or reward < 0 or minimum < 1:
        raise RuntimeError("Check BOT_TOKEN, ADMIN_IDS, REQUIRED_CHANNELS, and the amount settings in .env.")
    return Settings(
        token=token,
        admin_ids=admins,
        channels=channels,
        referral_reward=reward,
        min_withdrawal=minimum,
        currency=os.getenv("CURRENCY", "INR").strip() or "INR",
        support_username=os.getenv("SUPPORT_USERNAME", "").strip(),
    )


SETTINGS = get_settings()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def setup_database() -> None:
    with closing(connect()) as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                referrer_id INTEGER REFERENCES users(telegram_id),
                is_verified INTEGER NOT NULL DEFAULT 0,
                referral_rewarded INTEGER NOT NULL DEFAULT 0,
                balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                upi_id TEXT,
                created_at TEXT NOT NULL,
                verified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                amount INTEGER NOT NULL CHECK(amount > 0),
                upi_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, created_at);
            """
        )
        db.commit()


def upsert_user(user, referral_id: int | None) -> None:
    """Create user once; referral cannot be overwritten after first /start."""
    if referral_id == user.id:
        referral_id = None
    with closing(connect()) as db:
        if referral_id is not None:
            referrer_exists = db.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?", (referral_id,)
            ).fetchone()
            if not referrer_exists:
                referral_id = None
        db.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, referrer_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              username=excluded.username, first_name=excluded.first_name
            """,
            (user.id, user.username, user.first_name or "User", referral_id, now()),
        )
        db.commit()


def user_row(user_id: int) -> sqlite3.Row | None:
    with closing(connect()) as db:
        return db.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,)).fetchone()


def set_upi(user_id: int, upi_id: str) -> None:
    with closing(connect()) as db:
        db.execute("UPDATE users SET upi_id = ? WHERE telegram_id = ?", (upi_id, user_id))
        db.commit()


def set_verified(user_id: int, verified: bool) -> None:
    with closing(connect()) as db:
        db.execute("UPDATE users SET is_verified = ? WHERE telegram_id = ?", (int(verified), user_id))
        db.commit()


def credit_user(user_id: int, amount: int) -> bool:
    with closing(connect()) as db:
        result = db.execute(
            "UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, user_id)
        )
        db.commit()
        return result.rowcount == 1


def register_verified_referral(user_id: int) -> tuple[bool, int | None]:
    """Unlock user, award referrer exactly once, and return award status/referrer."""
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        if user is None:
            db.rollback()
            return False, None
        already_verified = bool(user["is_verified"])
        db.execute(
            "UPDATE users SET is_verified = 1, verified_at = COALESCE(verified_at, ?) WHERE telegram_id = ?",
            (now(), user_id),
        )
        awarded_referrer = None
        if user["referrer_id"] and not user["referral_rewarded"]:
            referrer = db.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?", (user["referrer_id"],)
            ).fetchone()
            if referrer:
                db.execute(
                    "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                    (SETTINGS.referral_reward, user["referrer_id"]),
                )
                db.execute(
                    "UPDATE users SET referral_rewarded = 1 WHERE telegram_id = ?", (user_id,)
                )
                awarded_referrer = user["referrer_id"]
        db.commit()
        return not already_verified, awarded_referrer


def create_withdrawal(user_id: int, amount: int) -> tuple[int | None, str]:
    """Atomically reserve balance and create exactly one pending payout request."""
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT balance, upi_id FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        if user is None or not user["upi_id"]:
            db.rollback()
            return None, "Add a UPI ID first with /upi."
        if amount < SETTINGS.min_withdrawal:
            db.rollback()
            return None, f"Minimum withdrawal is {SETTINGS.min_withdrawal} {SETTINGS.currency}."
        existing = db.execute(
            "SELECT id FROM withdrawals WHERE user_id = ? AND status = 'pending'", (user_id,)
        ).fetchone()
        if existing:
            db.rollback()
            return None, "You already have a pending withdrawal request."
        if user["balance"] < amount:
            db.rollback()
            return None, "Your available balance is too low for that amount."
        db.execute("UPDATE users SET balance = balance - ? WHERE telegram_id = ?", (amount, user_id))
        result = db.execute(
            "INSERT INTO withdrawals (user_id, amount, upi_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (user_id, amount, user["upi_id"], now()),
        )
        db.commit()
        return result.lastrowid, ""


def pending_withdrawals() -> list[sqlite3.Row]:
    with closing(connect()) as db:
        return db.execute(
            """
            SELECT w.*, u.username, u.first_name
            FROM withdrawals w JOIN users u ON u.telegram_id = w.user_id
            WHERE w.status = 'pending' ORDER BY w.created_at ASC
            """
        ).fetchall()


def verified_user_ids() -> list[int]:
    """Recipients who have completed the mandatory-channel verification."""
    with closing(connect()) as db:
        rows = db.execute("SELECT telegram_id FROM users WHERE is_verified = 1").fetchall()
    return [row["telegram_id"] for row in rows]


def decide_withdrawal(withdrawal_id: int, approve: bool, admin_id: int) -> sqlite3.Row | None:
    """Approve or reject a pending request. Rejections refund reserved funds."""
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        request = db.execute(
            """
            SELECT w.*, u.username, u.first_name
            FROM withdrawals w JOIN users u ON u.telegram_id = w.user_id
            WHERE w.id = ?
            """,
            (withdrawal_id,),
        ).fetchone()
        if request is None or request["status"] != "pending":
            db.rollback()
            return None
        status = "approved" if approve else "rejected"
        db.execute(
            "UPDATE withdrawals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (status, now(), admin_id, withdrawal_id),
        )
        if not approve:
            db.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (request["amount"], request["user_id"]),
            )
        db.commit()
        return request


def required_join_keyboard(channels: Iterable[str]) -> InlineKeyboardMarkup:
    buttons = []
    for channel in channels:
        username = channel.lstrip("@")
        buttons.append([InlineKeyboardButton(f"Join {channel}", url=f"https://t.me/{username}")])
    buttons.append([InlineKeyboardButton("✅ I have joined — Verify", callback_data="verify")])
    return InlineKeyboardMarkup(buttons)


async def missing_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    missing = []
    valid_statuses = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
    for channel in SETTINGS.channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in valid_statuses:
                missing.append(channel)
        except Exception:
            LOGGER.exception("Could not check membership for %s", channel)
            # Do not unlock a user when membership cannot be verified.
            missing.append(channel)
    return missing


def referral_link(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    username = context.bot.username
    return f"https://t.me/{username}?start=ref_{user_id}" if username else "Your bot link is loading; try /balance again."


async def send_locked_message(update: Update) -> None:
    text = "🔒 Please join every required channel, then tap **Verify** to unlock the bot."
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=required_join_keyboard(SETTINGS.channels)
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=required_join_keyboard(SETTINGS.channels)
        )


async def ensure_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    db_user = user_row(user.id)
    if db_user and db_user["is_verified"]:
        # Membership is checked again to prevent users leaving required channels
        # after receiving access.
        if not await missing_channels(user.id, context):
            return True
        set_verified(user.id, False)
    await send_locked_message(update)
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    referral_id = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referral_id = int(context.args[0][4:])
        except ValueError:
            pass
    upsert_user(user, referral_id)
    if user_row(user.id)["is_verified"]:
        await show_home(update, context)
    else:
        await send_locked_message(update)


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    query = update.callback_query
    if not user:
        return
    if query:
        await query.answer()
    # Ensure a user who presses an old button still has a database account.
    upsert_user(user, None)
    missing = await missing_channels(user.id, context)
    if missing:
        message = "You still need to join: " + ", ".join(missing) + ". Then verify again."
        if query:
            await query.message.reply_text(message, reply_markup=required_join_keyboard(missing))
        else:
            await update.effective_message.reply_text(message, reply_markup=required_join_keyboard(missing))
        return
    first_unlock, rewarded_referrer = register_verified_referral(user.id)
    if rewarded_referrer:
        try:
            await context.bot.send_message(
                rewarded_referrer,
                f"🎉 You received {SETTINGS.referral_reward} {SETTINGS.currency} for a verified referral!",
            )
        except Exception:
            LOGGER.info("Could not notify referrer %s", rewarded_referrer)
    if first_unlock:
        message = "✅ All channels verified. Your account is now unlocked!"
    else:
        message = "✅ Your channel membership is verified."
    if query:
        await query.message.reply_text(message)
    else:
        await update.effective_message.reply_text(message)
    await show_home(update, context)


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message:
        return
    row = user_row(user.id)
    text = (
        "👋 *Welcome to Refer & Earn!*\n\n"
        f"Share your link and earn *{SETTINGS.referral_reward} {SETTINGS.currency}* when a new user joins all required channels:\n"
        f"`{referral_link(context, user.id)}`\n\n"
        "Use /balance to see your wallet, /upi to save your UPI ID, and /withdraw to request a payout."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_unlocked(update, context):
        return
    user = update.effective_user
    row = user_row(user.id)
    upi = row["upi_id"] or "Not added"
    await update.effective_message.reply_text(
        f"💰 *Wallet balance:* {row['balance']} {SETTINGS.currency}\n"
        f"🏦 *UPI ID:* `{upi}`\n\n"
        "Your referral link:\n"
        f"`{referral_link(context, user.id)}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def upi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await ensure_unlocked(update, context):
        return ConversationHandler.END
    await update.effective_message.reply_text("Send your UPI ID (example: `name@bank`). Send /cancel to stop.", parse_mode=ParseMode.MARKDOWN)
    return UPI_STATE


async def receive_upi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.effective_message.text or "").strip().lower()
    if not UPI_PATTERN.fullmatch(value):
        await update.effective_message.reply_text("That does not look like a valid UPI ID. Try again, e.g. `name@bank`.", parse_mode=ParseMode.MARKDOWN)
        return UPI_STATE
    set_upi(update.effective_user.id, value)
    await update.effective_message.reply_text(f"✅ UPI ID saved as `{value}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await ensure_unlocked(update, context):
        return ConversationHandler.END
    row = user_row(update.effective_user.id)
    if not row["upi_id"]:
        await update.effective_message.reply_text("Please add your UPI ID first using /upi.")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"Your available balance is {row['balance']} {SETTINGS.currency}.\n"
        f"Send the withdrawal amount (minimum {SETTINGS.min_withdrawal}). Send /cancel to stop."
    )
    return WITHDRAW_STATE


def withdrawal_buttons(withdrawal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve", callback_data=f"wd:approve:{withdrawal_id}"),
            InlineKeyboardButton("❌ Reject + refund", callback_data=f"wd:reject:{withdrawal_id}"),
        ]]
    )


def request_summary(request: sqlite3.Row) -> str:
    name = f"@{request['username']}" if request["username"] else request["first_name"]
    name = escape_markdown(name, version=1)
    return (
        f"*Withdrawal #{request['id']}*\n"
        f"User: {name} (`{request['user_id']}`)\n"
        f"Amount: *{request['amount']} {SETTINGS.currency}*\n"
        f"UPI: `{request['upi_id']}`\n"
        f"Requested: {request['created_at']} UTC"
    )


async def receive_withdrawal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip()
    try:
        amount = int(raw)
    except ValueError:
        await update.effective_message.reply_text("Please send a whole-number amount, for example `100`.", parse_mode=ParseMode.MARKDOWN)
        return WITHDRAW_STATE
    request_id, error = create_withdrawal(update.effective_user.id, amount)
    if error:
        await update.effective_message.reply_text(f"❌ {error}")
        return ConversationHandler.END
    request = next(row for row in pending_withdrawals() if row["id"] == request_id)
    await update.effective_message.reply_text(
        f"✅ Withdrawal request #{request_id} sent for admin review. {amount} {SETTINGS.currency} has been reserved from your wallet."
    )
    for admin_id in SETTINGS.admin_ids:
        try:
            await context.bot.send_message(
                admin_id,
                request_summary(request),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=withdrawal_buttons(request_id),
            )
        except Exception:
            LOGGER.exception("Could not send withdrawal #%s to admin %s", request_id, admin_id)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in SETTINGS.admin_ids)


async def admin_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    requests = pending_withdrawals()
    if not requests:
        await update.effective_message.reply_text("No pending withdrawal requests.")
        return
    for request in requests:
        await update.effective_message.reply_text(
            request_summary(request), parse_mode=ParseMode.MARKDOWN, reply_markup=withdrawal_buttons(request["id"])
        )


async def admin_credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /admin_credit <user_id> <amount>")
        return
    try:
        user_id, amount = map(int, context.args)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("User ID and amount must be positive whole numbers.")
        return
    if not credit_user(user_id, amount):
        await update.effective_message.reply_text("User not found. They must use /start first.")
        return
    await update.effective_message.reply_text(f"Credited {amount} {SETTINGS.currency} to `{user_id}`.", parse_mode=ParseMode.MARKDOWN)
    try:
        await context.bot.send_message(user_id, f"🎉 Your wallet was credited with {amount} {SETTINGS.currency}.")
    except Exception:
        LOGGER.info("Could not notify credited user %s", user_id)


async def send_broadcast(
    update: Update, context: ContextTypes.DEFAULT_TYPE, source_message
) -> None:
    """Copy an admin's message to every unlocked user, observing flood limits."""
    recipients = verified_user_ids()
    if not recipients:
        await update.effective_message.reply_text("No verified users are available for this broadcast.")
        return
    await update.effective_message.reply_text(
        f"📣 Broadcast started for {len(recipients)} verified user(s)."
    )
    delivered = 0
    failed = 0
    for user_id in recipients:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_message.chat_id,
                message_id=source_message.message_id,
            )
            delivered += 1
            # Stay below the normal per-bot broadcast rate.
            await asyncio.sleep(0.04)
        except RetryAfter as error:
            await asyncio.sleep(error.retry_after + 1)
            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_message.chat_id,
                    message_id=source_message.message_id,
                )
                delivered += 1
            except TelegramError:
                failed += 1
        except Forbidden:
            # A user may have blocked the bot; do not stop the entire broadcast.
            failed += 1
        except TelegramError:
            LOGGER.warning("Broadcast delivery failed for user %s", user_id)
            failed += 1
    await update.effective_message.reply_text(
        f"✅ Broadcast complete. Delivered: {delivered} | Failed/blocked: {failed}"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start an admin-only broadcast; replying to a post broadcasts it immediately."""
    if not is_admin(update):
        return ConversationHandler.END
    message = update.effective_message
    if message.reply_to_message:
        await send_broadcast(update, context, message.reply_to_message)
        return ConversationHandler.END
    await message.reply_text(
        "Send or forward the post you want to broadcast.\n\n"
        "Tip: reply to a post with /broadcast to send it immediately.\n"
        "Send /cancel to stop."
    )
    return BROADCAST_STATE


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    await send_broadcast(update, context, update.effective_message)
    return ConversationHandler.END


async def withdrawal_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not is_admin(update):
        await query.answer("Admins only.", show_alert=True)
        return
    _, action, raw_id = query.data.split(":")
    try:
        request_id = int(raw_id)
    except ValueError:
        await query.answer("Invalid request.", show_alert=True)
        return
    request = decide_withdrawal(request_id, action == "approve", update.effective_user.id)
    if not request:
        await query.answer("This request was already handled.", show_alert=True)
        return
    status = "APPROVED" if action == "approve" else "REJECTED — amount refunded"
    await query.answer(f"Withdrawal {status.lower()}.")
    await query.edit_message_text(
        f"{request_summary(request)}\n\n*{status}* by `{update.effective_user.id}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        user_message = (
            f"✅ Your withdrawal #{request_id} for {request['amount']} {SETTINGS.currency} was approved."
            if action == "approve"
            else f"❌ Your withdrawal #{request_id} was rejected. {request['amount']} {SETTINGS.currency} has been returned to your wallet."
        )
        await context.bot.send_message(request["user_id"], user_message)
    except Exception:
        LOGGER.info("Could not notify payout user %s", request["user_id"])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled exception while processing update", exc_info=context.error)


def main() -> None:
    setup_database()
    application = Application.builder().token(SETTINGS.token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("verify", verify))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("admin_withdrawals", admin_withdrawals))
    application.add_handler(CommandHandler("admin_credit", admin_credit))
    application.add_handler(CallbackQueryHandler(verify, pattern=r"^verify$"))
    application.add_handler(CallbackQueryHandler(withdrawal_decision, pattern=r"^wd:(approve|reject):\d+$"))
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("upi", upi)],
            states={UPI_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_upi)]},
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("withdraw", withdraw)],
            states={WITHDRAW_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_withdrawal_amount)]},
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("broadcast", broadcast)],
            states={BROADCAST_STATE: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_broadcast)]},
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    application.add_error_handler(error_handler)
    LOGGER.info("Bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
