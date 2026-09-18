"""
Manages Telethon clients: one per logged-in user.
Uses StringSession so the session can be stored (encrypted) in the DB
instead of as a loose .session file on disk.
"""
import os
from telethon import TelegramClient, types, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import crypto_utils

# Whichever phone number logs in matching this becomes admin (that's you).
# Set this env var to your own phone number, in the same format you log in with.
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")

# In-memory cache of live clients, keyed by user_id, so the worker
# doesn't reconnect on every message.
_live_clients = {}

# Temporary storage for in-progress logins, keyed by phone number.
_pending_logins = {}


async def start_login(phone: str, api_id: int, api_hash: str):
    """Step 1: send the login code to the user's Telegram app.
    api_id/api_hash are supplied by the user themselves (their own,
    from my.telegram.org) rather than shared app-wide credentials —
    this keeps one user's activity from ever affecting another's."""
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    _pending_logins[phone] = {
        "client": client,
        "phone_code_hash": sent.phone_code_hash,
        "api_id": api_id,
        "api_hash": api_hash,
    }
    return {"status": "code_sent"}


async def verify_login(phone: str, code: str, password: str = None):
    """Step 2: submit the code (and 2FA password if needed), persist session."""
    pending = _pending_logins.get(phone)
    if not pending:
        return {"status": "error", "detail": "No pending login for this phone. Call start_login first."}

    client = pending["client"]
    try:
        await client.sign_in(phone, code, phone_code_hash=pending["phone_code_hash"])
    except SessionPasswordNeededError:
        if not password:
            return {"status": "needs_password"}
        await client.sign_in(password=password)

    me = await client.get_me()
    session_str = client.session.save()
    encrypted_session = crypto_utils.encrypt(session_str)
    encrypted_api_hash = crypto_utils.encrypt(pending["api_hash"])

    user_id = db.get_or_create_user(
        phone=phone,
        telegram_user_id=me.id,
        display_name=me.first_name or "",
        api_id=pending["api_id"],
        encrypted_api_hash=encrypted_api_hash,
    )
    db.save_session(user_id, encrypted_session)

    _live_clients[user_id] = client
    del _pending_logins[phone]

    is_admin = bool(ADMIN_PHONE) and phone.strip() == ADMIN_PHONE.strip()

    return {"status": "ok", "user_id": user_id, "display_name": me.first_name, "is_admin": is_admin}


async def get_client(user_id: int) -> TelegramClient:
    """Get (or reconnect) the live Telethon client for a user, using
    that user's own saved api_id/api_hash — never a shared/global one."""
    if user_id in _live_clients and _live_clients[user_id].is_connected():
        return _live_clients[user_id]

    session_row = db.get_session(user_id)
    user_row = db.get_user(user_id)
    if not session_row or not user_row or not user_row.get("api_id") or not user_row.get("encrypted_api_hash"):
        raise RuntimeError(f"No saved Telegram session for user {user_id}. They must log in again.")

    session_str = crypto_utils.decrypt(session_row["encrypted_session"])
    api_hash = crypto_utils.decrypt(user_row["encrypted_api_hash"])
    client = TelegramClient(StringSession(session_str), user_row["api_id"], api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        _live_clients.pop(user_id, None)
        raise RuntimeError(f"Saved Telegram session for user {user_id} is not authorized or has expired.")
    await client.get_me()
    _live_clients[user_id] = client
    return client


async def disconnect_client(user_id: int):
    client = _live_clients.pop(user_id, None)
    if client and client.is_connected():
        try:
            await client.disconnect()
        except Exception:
            pass


async def list_dialogs(user_id: int):
    """Return the user's groups/chats/channels so the UI can show a picker."""
    client = await get_client(user_id)
    # Fetch up to 300 dialogs with high speed to avoid flood wait
    dialogs = await client.get_dialogs(limit=300)
    res = []
    seen = set()
    for d in dialogs:
        if d.id in seen:
            continue
        seen.add(d.id)
        name = d.name or getattr(d.entity, 'title', None) or getattr(d.entity, 'first_name', '') or f"Chat {d.id}"
        username = getattr(d.entity, 'username', None)
        is_group = bool(d.is_group)
        is_channel = bool(d.is_channel and not d.is_group)
        is_user = bool(d.is_user)
        res.append({
            "chat_id": d.id,
            "name": name,
            "username": username,
            "is_group": is_group,
            "is_channel": is_channel,
            "is_user": is_user,
        })
    return res


async def resolve_chat(user_id: int, query: str):
    """Find any chat, channel, or group by ID, @username, or search query."""
    client = await get_client(user_id)
    q = query.strip()
    if q.startswith("@"):
        target = q[1:]
    elif q.lstrip("-").isdigit():
        target = int(q)
    else:
        target = q

    entity = await client.get_entity(target)
    chat_id = utils.get_peer_id(entity)
    name = getattr(entity, 'title', None) or getattr(entity, 'first_name', '') or str(chat_id)
    if getattr(entity, 'last_name', None):
        name = f"{name} {entity.last_name}"

    is_user = isinstance(entity, types.User)
    is_group = (
        isinstance(entity, (types.Chat, types.ChatForbidden)) or
        (isinstance(entity, types.Channel) and getattr(entity, 'megagroup', False))
    )
    is_channel = isinstance(entity, types.Channel) and not getattr(entity, 'megagroup', False)

    return {
        "chat_id": chat_id,
        "name": name,
        "username": getattr(entity, 'username', None),
        "is_group": is_group,
        "is_channel": is_channel,
        "is_user": is_user,
    }


async def list_members(user_id: int, chat_id: int, limit: int = 50):
    """Return members of a group so the UI can let the user pick 'boss'."""
    client = await get_client(user_id)
    participants = await client.get_participants(chat_id, limit=limit)
    return [
        {
            "id": p.id,
            "name": (p.first_name or "") + (" " + p.last_name if p.last_name else ""),
            "username": p.username,
        }
        for p in participants
    ]
