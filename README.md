# Telegram Auto-Reply — Setup

## 1. Get your admin phone number ready
Each user (including you) now gets their **own** Telegram API credentials at
login time — there's no shared `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` for the
whole app anymore. This is safer for a public app: one user's activity can
never get another user's credentials flagged or rate-limited.

The login screen walks users through getting their own from
https://my.telegram.org/apps — nothing to set up here for that part.

## 2. Install & configure the backend
```bash
cd backend
pip install -r requirements.txt

export ADMIN_PHONE=+855xxxxxxxx   # your own phone number — logging in with this unlocks the Admin tab
```

Generate the encryption key once (used to encrypt saved sessions at rest):
```bash
python -c "from crypto_utils import generate_key; generate_key()"
```
This creates `backend/secret.key`. **Back it up. Never commit it to git.**
If you lose it, every saved session becomes unreadable and users must re-login.

## 3. Run the backend & Web UI
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
Open **http://localhost:8000** in your browser to access the complete application!
FastAPI now serves the full frontend directly from port 8000 without needing any extra web server.

*(Optional)* If you prefer serving `frontend/index.html` via a separate static dev server:
```bash
cd frontend
python -m http.server 8080
```
Then open **http://localhost:8080** (API calls will automatically route to http://localhost:8000).

## 5. Try it end to end
1. Open the frontend in a browser
2. Enter a phone number → get the Telegram login code → enter it (+ 2FA
   password if that account has one)
3. Pick a chat from the list
4. Set a rule (anyone / one specific person / keyword) and a reply message
5. Send a test message from another account into that chat — the reply
   should appear after the configured delay

## Features & Multi-Rule Capabilities
- **Multi-rule smart execution**: Specific rules (sender, keyword) are prioritized over generic catch-all (`all`) rules so generic rules never block specific keyword/sender rules.
- **Multi-rule co-execution**: Multiple matching rules (e.g. reaction rule + reply text rule) can both execute for the same message.
- **Multi-keyword triggers**: Separate keywords with commas (e.g. `ok, urgent, price`) with configurable match mode (`any` [OR] or `all` [AND]).
- **Multi-chat selection & Global Rules**: Apply rules to multiple selected chats at once, or use `All Chats (Global)` to cover every conversation.
- **Rotating reply variations**: Separate text with `|` to randomly pick a reply variant each time, keeping replies natural and anti-spam friendly.
- **In-place Rule Editing**: Edit rules directly without deleting and recreating them.
- **Bulk rule controls**: Bulk toggle (Turn all ON/OFF) and bulk cleanup.

## What's not built yet (from the project plan)
- Rebuilding listeners after a crash mid-message (currently resumes on clean restart, via the FastAPI startup hook)
- Per-chat mute hours / quiet periods
- AI-generated replies (rules table already has room to add a `mode` field for this later)

## Important reminder
This automates personal Telegram accounts using the client API (Telethon),
which is outside Telegram's official terms for bot automation. Keep reply
volume low, keep the random delay in place, and expect that heavy or
obviously bot-like use on any given account risks that account being
flagged by Telegram's own systems — hosting it on your server doesn't change
that exposure, it just centralizes it across however many users connect.
