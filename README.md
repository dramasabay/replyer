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
pip install fastapi uvicorn telethon python-multipart cryptography

export ADMIN_PHONE=+855xxxxxxxx   # your own phone number — logging in with this unlocks the Admin tab
```

Generate the encryption key once (used to encrypt saved sessions at rest):
```bash
python -c "from crypto_utils import generate_key; generate_key()"
```
This creates `backend/secret.key`. **Back it up. Never commit it to git.**
If you lose it, every saved session becomes unreadable and users must re-login.

## 3. Run the backend
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For production, run this behind a process manager (systemd, pm2, or
`supervisord`) so it restarts automatically, and put it behind HTTPS
(e.g. Caddy or nginx + Let's Encrypt) since it's carrying login codes and
session data — never serve this over plain HTTP on the open internet.

## 4. Serve the frontend
The `frontend/index.html` is a static file. Serve it with anything:
```bash
cd frontend
python -m http.server 8080
```
Or drop it behind your existing nginx/Caddy setup. If your backend isn't on
`localhost:8000`, edit the `API_BASE` constant near the top of the `<script>`
tag in `index.html`.

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
- **Rule Simulator**: Built-in interactive simulator to test messages and see matched rules in real time.
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
