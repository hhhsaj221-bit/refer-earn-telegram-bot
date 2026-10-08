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
from urllib.parse import quote

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

(
    UPI_STATE,
    WITHDRAW_STATE,
    BROADCAST_STATE,
    ADMIN_WELCOME_STATE,
    ADMIN_HOME_STATE,
    ADMIN_PUBLIC_CHANNEL_STATE,
    ADMIN_PRIVATE_CHANNEL_STATE,
) = range(7)
DATA_DIR = os.getenv("DATA_DIR", "").strip()
DB_PATH = Path(DATA_DIR) / "bot.db" if DATA_DIR else Path(__file__).with_name("bot.db")
UPI_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9.-]{1,63}$")
PUBLIC_CHANNEL_PATTERN = re.compile(r"^@[A-Za-z0-9_]{5,32}$")

DEFAULT_WELCOME_TEXT = (
    "👋 Welcome to Refer & Earn!\n\n"
    "🔗 Join every required channel to unlock the bot.\n\n"
    "✅ After joining, tap I Joined — Verify."
)
DEFAULT_HOME_TEXT = (
    "🎉 Welcome to Refer & Earn!\n\n"
    "👤 User: {name}\n"
    "🆔 ID: {id}\n\n"
    "💎 Balance: {balance} {currency}\n"
    "👥 Referrals: {verified_referrals}/{total_referrals}\n\n"
    "🎁 Earn {reward} {currency} for each verified referral.\n"
    "Choose an option below."
)


@dataclass(frozen=True)
class RequiredChannel:
    """A join link shown to a user and the chat identifier used for verification."""

    label: str
    chat_id: str | int
    join_url: str
    channel_id: int | None = None


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    channels: tuple[RequiredChannel, ...]
    referral_reward: int
    min_withdrawal: int
    currency: str
    support_username: str


def parse_private_channels(value: str) -> tuple[RequiredChannel, ...]:
    """Parse `Name|-1001234567890|https://t.me/+invite;...` configuration."""
    channels = []
    for item in (entry.strip() for entry in value.split(";") if entry.strip()):
        parts = [part.strip() for part in item.split("|", maxsplit=2)]
        if len(parts) != 3 or not all(parts):
            raise RuntimeError(
                "Each REQUIRED_CHANNELS_PRIVATE entry must be Name|chat_id|invite_link."
            )
        label, raw_chat_id, invite_url = parts
        if not raw_chat_id.startswith("-") or not raw_chat_id[1:].isdigit():
            raise RuntimeError("A private channel chat_id must be its numeric -100... ID.")
        if not invite_url.startswith("https://t.me/"):
            raise RuntimeError("A private channel invite link must start with https://t.me/.")
        channels.append(
            RequiredChannel(label=label, chat_id=int(raw_chat_id), join_url=invite_url)
        )
    return tuple(channels)


def get_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    admins = frozenset(
        int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()
    )
    public_channels = tuple(
        RequiredChannel(
            label=value.strip(),
            chat_id=value.strip(),
            join_url=f"https://t.me/{value.strip().lstrip('@')}",
        )
        for value in os.getenv("REQUIRED_CHANNELS", "").split(",")
        if value.strip()
    )
    private_channels = parse_private_channels(os.getenv("REQUIRED_CHANNELS_PRIVATE", ""))
    channels = public_channels + private_channels
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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                chat_id TEXT NOT NULL UNIQUE,
                join_url TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO app_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?)",
            ("welcome_text", DEFAULT_WELCOME_TEXT, now()),
        )
        db.execute(
            "INSERT OR IGNORE INTO app_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?)",
            ("home_text", DEFAULT_HOME_TEXT, now()),
        )
        existing_channels = db.execute("SELECT COUNT(*) FROM required_channels").fetchone()[0]
        if existing_channels == 0:
            for channel in SETTINGS.channels:
                db.execute(
                    """
                    INSERT INTO required_channels (label, chat_id, join_url, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (channel.label, str(channel.chat_id), channel.join_url, now()),
                )
        db.commit()


def app_setting(key: str, default: str) -> str:
    with closing(connect()) as db:
        row = db.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?", (key,)
        ).fetchone()
    return row["setting_value"] if row else default


def set_app_setting(key: str, value: str) -> None:
    with closing(connect()) as db:
        db.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
              setting_value = excluded.setting_value,
              updated_at = excluded.updated_at
            """,
            (key, value, now()),
        )
        db.commit()


def configured_channels() -> list[RequiredChannel]:
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT id, label, chat_id, join_url FROM required_channels ORDER BY id"
        ).fetchall()
    channels = []
    for row in rows:
        raw_chat_id = row["chat_id"]
        chat_id: str | int = int(raw_chat_id) if raw_chat_id.startswith("-") else raw_chat_id
        channels.append(
            RequiredChannel(
                label=row["label"], chat_id=chat_id, join_url=row["join_url"], channel_id=row["id"]
            )
        )
    return channels


def add_required_channel(channel: RequiredChannel) -> tuple[bool, str]:
    with closing(connect()) as db:
        try:
            db.execute(
                """
                INSERT INTO required_channels (label, chat_id, join_url, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (channel.label, str(channel.chat_id), channel.join_url, now()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return False, "This channel is already in the required-channel list."
    return True, "Channel added. Make sure the bot is an administrator there."


def remove_required_channel(channel_id: int) -> tuple[bool, str]:
    with closing(connect()) as db:
        total = db.execute("SELECT COUNT(*) FROM required_channels").fetchone()[0]
        if total <= 1:
            return False, "Keep at least one required channel. Add another channel before removing this one."
        result = db.execute("DELETE FROM required_channels WHERE id = ?", (channel_id,))
        db.commit()
    if result.rowcount != 1:
        return False, "Channel was not found."
    return True, "Required channel removed."


def render_text(template: str, replacements: dict[str, object]) -> str:
    """Replace supported placeholders without treating admin text as a format string."""
    for key, value in replacements.items():
        template = template.replace("{" + key + "}", str(value))
    return template


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


def referral_stats(user_id: int) -> tuple[int, int, int]:
    """Return total, pending, and verified referrals for one inviter."""
    with closing(connect()) as db:
        row = db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN is_verified = 0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN is_verified = 1 THEN 1 ELSE 0 END) AS verified
            FROM users WHERE referrer_id = ?
            """,
            (user_id,),
        ).fetchone()
    return row["total"], row["pending"] or 0, row["verified"] or 0


def withdrawal_history(user_id: int) -> list[sqlite3.Row]:
    with closing(connect()) as db:
        return db.execute(
            """
            SELECT id, amount, status, created_at, decided_at
            FROM withdrawals WHERE user_id = ?
            ORDER BY id DESC LIMIT 10
            """,
            (user_id,),
        ).fetchall()


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


def required_join_keyboard(channels: Iterable[RequiredChannel]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, channel in enumerate(channels, start=1):
        row.append(InlineKeyboardButton(f"📣 Join {index}", url=channel.join_url))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✅ I Joined — Verify", callback_data="verify")])
    return InlineKeyboardMarkup(buttons)


def main_menu(user_id: int | None = None) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("👤 Profile", callback_data="menu:profile"),
            InlineKeyboardButton("💸 Withdraw", callback_data="menu:withdraw"),
        ],
        [
            InlineKeyboardButton("🔗 Invite", callback_data="menu:invite"),
            InlineKeyboardButton("🧾 History", callback_data="menu:history"),
        ],
    ]
    if user_id in SETTINGS.admin_ids:
        buttons.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(buttons)


def back_to_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu:home")]])


async def missing_channels(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> list[RequiredChannel]:
    missing = []
    valid_statuses = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
    for channel in configured_channels():
        try:
            member = await context.bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
            if member.status not in valid_statuses:
                missing.append(channel)
        except Exception:
            LOGGER.exception("Could not check membership for %s", channel.label)
            # Do not unlock a user when membership cannot be verified.
            missing.append(channel)
    return missing


def referral_link(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    username = context.bot.username
    return f"https://t.me/{username}?start=ref_{user_id}" if username else "Your bot link is loading; try /balance again."


async def send_locked_message(update: Update) -> None:
    text = app_setting("welcome_text", DEFAULT_WELCOME_TEXT)
    keyboard = required_join_keyboard(configured_channels())
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=keyboard
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard
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
    if not user_row(user.id)["is_verified"]:
        await send_locked_message(update)
        return
    if await missing_channels(user.id, context):
        set_verified(user.id, False)
        await send_locked_message(update)
        return
    await show_home(update, context)


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
        message = "You still need to join: " + ", ".join(channel.label for channel in missing) + ". Then verify again."
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
    total, _, verified = referral_stats(user.id)
    text = render_text(
        app_setting("home_text", DEFAULT_HOME_TEXT),
        {
            "name": user.full_name or user.first_name or "User",
            "id": user.id,
            "balance": row["balance"],
            "currency": SETTINGS.currency,
            "total_referrals": total,
            "verified_referrals": verified,
            "reward": SETTINGS.referral_reward,
        },
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu(user.id))


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_unlocked(update, context):
        return
    await show_profile(update, context)


def masked_upi(upi_id: str | None) -> str:
    if not upi_id:
        return "Not added"
    local, handle = upi_id.split("@", maxsplit=1)
    if len(local) <= 2:
        return f"{local[0]}***@{handle}"
    return f"{local[:2]}***@{handle}"


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = user_row(user.id)
    total, pending, verified = referral_stats(user.id)
    upi = masked_upi(row["upi_id"])
    await update.effective_message.reply_text(
        "👤 *Your Profile*\n\n"
        f"🆔 *User ID:* `{user.id}`\n"
        f"💎 *Available balance:* {row['balance']} {SETTINGS.currency}\n"
        f"👥 *Referrals:* {verified}/{total}\n"
        f"⏳ *Pending joins:* {pending}\n"
        f"🏦 *UPI ID:* `{upi}`\n\n"
        "Use the button below to add or update your payout UPI ID.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🏦 Set / Change UPI", callback_data="menu:upi")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu:home")],
            ]
        ),
    )


async def show_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    link = referral_link(context, user.id)
    total, pending, verified = referral_stats(user.id)
    share_text = quote(f"Join using my link and earn rewards! {link}")
    share_url = f"https://t.me/share/url?url={quote(link)}&text={share_text}"
    await update.effective_message.reply_text(
        "🔗 *Invite Friends & Earn*\n\n"
        f"🎁 Reward per verified referral: *{SETTINGS.referral_reward} {SETTINGS.currency}*\n"
        f"👥 Your referrals: *{verified}/{total} verified*"
        + (f" · {pending} pending" if pending else "")
        + "\n\n*Your referral link:*\n"
        f"`{link}`\n\n"
        "Share it with friends. The reward is credited only after they join every required channel.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📤 Share Link", url=share_url)],
                [InlineKeyboardButton("👥 My Invites", callback_data="menu:analytics")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu:home")],
            ]
        ),
    )


async def show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    total, pending, verified = referral_stats(user.id)
    await update.effective_message.reply_text(
        "📊 *Your Referral Analytics*\n\n"
        f"👥 *Total invited:* {total} user(s)\n"
        f"⏳ *Pending join:* {pending} user(s)\n"
        f"✅ *Verified & credited:* {verified} user(s)\n\n"
        "Invite more friends to increase your rewards!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_to_home(),
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    records = withdrawal_history(user.id)
    if not records:
        text = (
            "🧾 *No Withdrawal Records*\n\n"
            "You have not requested any withdrawals yet.\n\n"
            "Keep earning and unlock your first reward!"
        )
    else:
        icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
        lines = ["🧾 *Withdrawal History*\n"]
        for record in records:
            status = record["status"].title()
            lines.append(
                f"{icons[record['status']]} #{record['id']} — *{record['amount']} {SETTINGS.currency}* — {status}"
            )
        text = "\n".join(lines)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_home()
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the selected user-menu screen without relying on typed commands."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not await ensure_unlocked(update, context):
        return
    action = query.data.removeprefix("menu:")
    if action == "home":
        await show_home(update, context)
    elif action == "profile":
        await show_profile(update, context)
    elif action == "invite":
        await show_invite(update, context)
    elif action == "analytics":
        await show_analytics(update, context)
    elif action == "history":
        await show_history(update, context)


async def upi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    if not await ensure_unlocked(update, context):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🏦 Send your UPI ID (example: `name@bank`).\n\nSend /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return UPI_STATE


async def receive_upi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.effective_message.text or "").strip().lower()
    if not UPI_PATTERN.fullmatch(value):
        await update.effective_message.reply_text("That does not look like a valid UPI ID. Try again, e.g. `name@bank`.", parse_mode=ParseMode.MARKDOWN)
        return UPI_STATE
    set_upi(update.effective_user.id, value)
    await update.effective_message.reply_text(
        f"✅ UPI ID saved as `{value}`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
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
        f"✅ Withdrawal request #{request_id} sent for admin review. {amount} {SETTINGS.currency} has been reserved from your wallet.",
        reply_markup=main_menu(update.effective_user.id),
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


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📝 Welcome Text", callback_data="admin:edit_welcome"),
                InlineKeyboardButton("🏠 Home Text", callback_data="admin:edit_home"),
            ],
            [InlineKeyboardButton("📣 Required Channels", callback_data="admin:channels")],
            [InlineKeyboardButton("📢 Broadcast Help", callback_data="admin:broadcast_help")],
        ]
    )


async def show_admin_panel(update: Update) -> None:
    await update.effective_message.reply_text(
        "🛠 *Admin Panel*\n\n"
        "Control the welcome text, home text, and required public/private channels from here.\n\n"
        "Use `/broadcast` to send a post to all verified users.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_panel_keyboard(),
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await show_admin_panel(update)


async def show_message_settings(update: Update) -> None:
    await update.effective_message.reply_text(
        "📝 *Message Settings*\n\n"
        "Edit the text shown before verification or the main home text shown after verification.\n\n"
        "Home-text placeholders: `{name}`, `{id}`, `{balance}`, `{currency}`, "
        "`{total_referrals}`, `{verified_referrals}`, `{reward}`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Edit Welcome Text", callback_data="admin:edit_welcome")],
                [InlineKeyboardButton("✏️ Edit Home Text", callback_data="admin:edit_home")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin:panel")],
            ]
        ),
    )


async def show_channel_settings(update: Update) -> None:
    channels = configured_channels()
    await update.effective_message.reply_text(
        f"📣 *Required Channels*\n\nCurrently configured: {len(channels)}\n\n"
        "Public and private channels added here are checked before a user can use the bot.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("➕ Add Public", callback_data="admin:add_public"),
                    InlineKeyboardButton("➕ Add Private", callback_data="admin:add_private"),
                ],
                [InlineKeyboardButton("📋 View Channels", callback_data="admin:list_channels")],
                [InlineKeyboardButton("🗑 Remove Channel", callback_data="admin:remove_menu")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin:panel")],
            ]
        ),
    )


async def show_channel_list(update: Update, for_removal: bool = False) -> None:
    channels = configured_channels()
    if not channels:
        await update.effective_message.reply_text("No required channels are configured.")
        return
    lines = ["📋 *Required Channels*\n"]
    for index, channel in enumerate(channels, start=1):
        kind = "Private" if isinstance(channel.chat_id, int) else "Public"
        lines.append(f"{index}. {escape_markdown(channel.label, version=1)} — {kind}")
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back", callback_data="admin:channels")]]
    )
    if for_removal:
        rows = [
            [
                InlineKeyboardButton(
                    f"🗑 {channel.label[:40]}",
                    callback_data=f"admin:remove_channel:{channel.channel_id}",
                )
            ]
            for channel in channels
        ]
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin:channels")])
        markup = InlineKeyboardMarkup(rows)
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=markup
    )


async def edit_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        "Send the new welcome text now. It will appear above the Join buttons.\n\n"
        "Send /cancel to stop."
    )
    return ADMIN_WELCOME_STATE


async def edit_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        "Send the new home text now. You can use: \n"
        "`{name}`, `{id}`, `{balance}`, `{currency}`, `{total_referrals}`, "
        "`{verified_referrals}`, `{reward}`.\n\nSend /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_HOME_STATE


async def save_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    value = (update.effective_message.text or "").strip()
    if not value or len(value) > 3500:
        await update.effective_message.reply_text("Send a message between 1 and 3500 characters.")
        return ADMIN_WELCOME_STATE if key == "welcome_text" else ADMIN_HOME_STATE
    set_app_setting(key, value)
    await update.effective_message.reply_text("✅ Text saved.", reply_markup=admin_panel_keyboard())
    return ConversationHandler.END


async def receive_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await save_admin_text(update, context, "welcome_text")


async def receive_home_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await save_admin_text(update, context, "home_text")


async def add_public_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        "Send the public channel username, for example: `@my_channel`\n\n"
        "The bot must already be an administrator in that channel. Send /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_PUBLIC_CHANNEL_STATE


async def receive_public_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.effective_message.text or "").strip()
    if not PUBLIC_CHANNEL_PATTERN.fullmatch(value):
        await update.effective_message.reply_text("Send a public username like `@my_channel`.", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_PUBLIC_CHANNEL_STATE
    success, message = add_required_channel(
        RequiredChannel(label=value, chat_id=value, join_url=f"https://t.me/{value[1:]}")
    )
    await update.effective_message.reply_text(
        ("✅ " if success else "❌ ") + message, reply_markup=admin_panel_keyboard()
    )
    return ConversationHandler.END


async def add_private_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        "Send the private channel in this exact format:\n\n"
        "`Display Name|-1001234567890|https://t.me/+inviteCode`\n\n"
        "The bot must already be an administrator in that channel. Send /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_PRIVATE_CHANNEL_STATE


async def receive_private_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    try:
        channels = parse_private_channels((update.effective_message.text or "").strip())
    except RuntimeError as error:
        await update.effective_message.reply_text(f"❌ {error}")
        return ADMIN_PRIVATE_CHANNEL_STATE
    if not channels:
        await update.effective_message.reply_text("❌ Send one private channel in the requested format.")
        return ADMIN_PRIVATE_CHANNEL_STATE
    results = []
    for channel in channels:
        success, message = add_required_channel(channel)
        results.append(("✅ " if success else "❌ ") + f"{channel.label}: {message}")
    await update.effective_message.reply_text(
        "\n".join(results), reply_markup=admin_panel_keyboard()
    )
    return ConversationHandler.END


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not is_admin(update):
        await query.answer("Admins only.", show_alert=True)
        return
    await query.answer()
    action = query.data.removeprefix("admin:")
    if action == "panel":
        await show_admin_panel(update)
    elif action == "messages":
        await show_message_settings(update)
    elif action == "channels":
        await show_channel_settings(update)
    elif action == "list_channels":
        await show_channel_list(update)
    elif action == "remove_menu":
        await show_channel_list(update, for_removal=True)
    elif action.startswith("remove_channel:"):
        try:
            channel_id = int(action.rsplit(":", maxsplit=1)[1])
        except ValueError:
            await update.effective_message.reply_text("Invalid channel selection.")
            return
        success, message = remove_required_channel(channel_id)
        await update.effective_message.reply_text(("✅ " if success else "❌ ") + message)
        await show_channel_settings(update)
    elif action == "broadcast_help":
        await update.effective_message.reply_text(
            "📢 Use `/broadcast`, then send or forward the message.\n\n"
            "You can also reply to any post with `/broadcast` to send it immediately.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_panel_keyboard(),
        )


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
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("admin_withdrawals", admin_withdrawals))
    application.add_handler(CommandHandler("admin_credit", admin_credit))
    application.add_handler(CallbackQueryHandler(verify, pattern=r"^verify$"))
    application.add_handler(CallbackQueryHandler(withdrawal_decision, pattern=r"^wd:(approve|reject):\d+$"))
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("upi", upi),
                CallbackQueryHandler(upi, pattern=r"^menu:upi$"),
            ],
            states={UPI_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_upi)]},
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("withdraw", withdraw),
                CallbackQueryHandler(withdraw, pattern=r"^menu:withdraw$"),
            ],
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
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(edit_welcome, pattern=r"^admin:edit_welcome$"),
                CallbackQueryHandler(edit_home, pattern=r"^admin:edit_home$"),
                CallbackQueryHandler(add_public_channel_prompt, pattern=r"^admin:add_public$"),
                CallbackQueryHandler(add_private_channel_prompt, pattern=r"^admin:add_private$"),
            ],
            states={
                ADMIN_WELCOME_STATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_welcome_text)
                ],
                ADMIN_HOME_STATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_home_text)
                ],
                ADMIN_PUBLIC_CHANNEL_STATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_public_channel)
                ],
                ADMIN_PRIVATE_CHANNEL_STATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_private_channel)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            menu_callback, pattern=r"^menu:(home|profile|invite|analytics|history)$"
        )
    )
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    application.add_error_handler(error_handler)
    LOGGER.info("Bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
