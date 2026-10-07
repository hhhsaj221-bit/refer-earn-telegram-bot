# Refer & Earn Telegram Bot

A ready-to-configure Telegram bot with:

- compulsory membership checks for every configured channel;
- deep-link referral tracking and automatic referral reward on unlock;
- wallet balance, UPI ID storage, and withdrawal requests;
- admin-only approval/rejection buttons; and
- SQLite storage (no external database required).

## Before running

1. Create the Telegram channels and make them public (for example `@mychannel`).
2. Create a bot in [@BotFather](https://t.me/BotFather).
3. Add the bot as an **administrator** to every required channel. It needs permission to view members; it does not need posting rights.
4. Copy `.env.example` to `.env` and set the token, your numeric Telegram user ID, and the channel usernames.

> Never publish your `.env` file or bot token. The bot only checks channels it is configured for; users must be told why their UPI ID is collected and how payout requests are handled.

## Run locally

```powershell
cd C:\Users\Welcome\Documents\Codex\2026-10-07\hi\outputs\refer-earn-telegram-bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python bot.py
```

Use `/start` in the bot after it is running. The bot sends each user a personal link in the form `https://t.me/YourBot?start=ref_<id>`.

## User commands

| Command | Purpose |
| --- | --- |
| `/start` | Opens the bot and processes a referral link. |
| `/verify` | Re-checks all required channel joins. |
| `/balance` | Shows wallet balance and referral link. |
| `/upi` | Adds or changes the UPI ID used for withdrawal. |
| `/withdraw` | Starts a payout request. |
| `/cancel` | Cancels the current UPI/withdrawal input. |

## Admin commands

| Command | Purpose |
| --- | --- |
| `/admin_withdrawals` | Shows current pending requests with Approve / Reject buttons. |
| `/admin_credit <user_id> <amount>` | Manually adds a whole-number balance credit. |
| `/broadcast` | Sends or forwards the next post to every verified user. Reply to a post with this command to send it immediately. |

Approving a request **does not call a payment gateway**. It only records the administrative decision. Send the payment to the saved UPI ID separately, then tap **Approve**. Rejecting a request returns the reserved amount to the user's wallet.

Broadcasts copy the original content (text, photo, video, document, and forwarded messages are supported) without exposing the source chat. Telegram service messages, invoices, and protected/uncopyable posts may fail and are counted in the admin completion report.

## Important operational notes

- This starter uses whole currency units; it does not support paise/decimal balances.
- The required-channel membership check will fail unless the bot remains an administrator in every configured channel.
- Keep regular backups of `bot.db`, which contains user payout details and requests. Treat it as sensitive data.
- Add your terms, privacy notice, eligibility rules, and support process before inviting users or accepting personal payout details.
