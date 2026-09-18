"""
One event listener per active user. When a new message arrives in a chat
that has active rules, evaluate specific rules first (sender, keyword), then
catch-all rules, and execute all matching rules with natural delays and rotating replies.
If no active rule matches in a 1-on-1 private chat, the Away / Default reply is fired.
"""
import asyncio
import random
import re
import time
from telethon import events, functions, types

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import telegram_manager

# Cooldown cache: (chat_id, rule_id) -> last_replied_timestamp
_last_reply_times = {}

# Cooldown cache for Away reply: (user_id, chat_id) -> last_replied_timestamp
_last_away_times = {}

# Track which user_ids already have a listener attached on a live client instance
_watching_clients = {}


async def send_reaction(client, chat_id, message_id, emoji, input_chat=None):
    """Telethon has no high-level send_reaction() method — this uses the
    raw API call directly. Uses input_chat when available to prevent entity lookup errors."""
    peer = input_chat if input_chat is not None else chat_id
    try:
        await client(functions.messages.SendReactionRequest(
            peer=peer,
            msg_id=message_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
        ))
    except Exception as e:
        print(f"Reaction failed for chat {chat_id}: {e}")


def check_keyword_match(trigger_value: str, text: str, match_mode: str = "any") -> bool:
    """Checks if text matches keyword(s). Supports multiple keywords separated by commas or newlines."""
    if not trigger_value or text is None:
        return False
    keywords = [k.strip().lower() for k in re.split(r'[\r\n,]+', str(trigger_value)) if k.strip()]
    if not keywords:
        return False

    text_lower = text.lower()
    if match_mode == "all":
        return all(k in text_lower for k in keywords)
    else:  # default 'any' (OR)
        return any(k in text_lower for k in keywords)


def check_sender_match(trigger_value: str, sender_id: int, sender_username: str = None) -> bool:
    """Checks if sender_id or sender_username matches trigger_value."""
    if not trigger_value or sender_id is None:
        return False
    senders = [s.strip() for s in re.split(r'[\r\n,]+', str(trigger_value)) if s.strip()]
    for s in senders:
        try:
            if int(s) == sender_id:
                return True
        except (TypeError, ValueError):
            pass
        if sender_username and s.lstrip('@').lower() == sender_username.lower():
            return True
    return False


def pick_reply_text(raw_reply_text: str) -> str:
    """Supports rotating variations separated by '|'. Randomly picks one variation."""
    if not raw_reply_text:
        return ""
    if "|" in raw_reply_text:
        options = [opt.strip() for opt in raw_reply_text.split("|") if opt.strip()]
        if options:
            return random.choice(options)
    return raw_reply_text


def evaluate_rules(rules: list, text: str, sender_id: int, sender_username: str = None) -> list:
    """Pure rule evaluation function: prioritizes specific rules (sender, keyword)
    over generic catch-all ('all') rules. Returns all matching rules."""
    specific_rules = [r for r in rules if r.get("trigger_type") in ("sender", "keyword")]
    catch_all_rules = [r for r in rules if r.get("trigger_type") == "all"]

    matched = []

    # 1. Check specific rules first
    for rule in specific_rules:
        t_type = rule.get("trigger_type")
        t_val = rule.get("trigger_value")
        if t_type == "sender" and check_sender_match(t_val, sender_id, sender_username):
            matched.append(rule)
        elif t_type == "keyword":
            mode = rule.get("match_mode") or "any"
            if check_keyword_match(t_val, text, mode):
                matched.append(rule)

    # 2. If NO specific rules matched, fall back to catch-all ('all') rules
    if not matched and catch_all_rules:
        chat_specific = [r for r in catch_all_rules if r.get("chat_id") != 0]
        if chat_specific:
            matched.extend(chat_specific)
        else:
            matched.extend(catch_all_rules)

    return matched


async def start_watching(user_id: int):
    client = _watching_clients.get(user_id)
    if client is not None and client.is_connected():
        return  # already listening on a live connected client

    try:
        client = await telegram_manager.get_client(user_id)
    except Exception as e:
        print(f"Cannot start watching user {user_id}: {e}")
        return

    try:
        me = await client.get_me()
        owner_id = me.id if me else None
    except Exception:
        owner_id = None

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        # Never reply to outgoing messages sent by the account owner
        if getattr(event, 'out', False):
            return

        chat_id = event.chat_id
        sender_id = event.sender_id
        raw_text = event.raw_text or ""

        # Never reply to self or Saved Messages
        if owner_id and (sender_id == owner_id or chat_id == owner_id):
            return

        # Check if user is approved by admin
        user_row = db.get_user(user_id)
        if user_row and user_row.get("is_approved") == 0:
            return

        # Track user active timestamp
        db.update_user_active(user_id)

        # Resolve input_chat and sender details safely
        try:
            input_chat = await event.get_input_chat()
        except Exception:
            input_chat = chat_id

        sender_username = None
        sender_is_bot = False
        try:
            sender = await event.get_sender()
            if sender:
                sender_username = getattr(sender, 'username', None)
                sender_is_bot = bool(getattr(sender, 'bot', False))
        except Exception:
            pass

        # Helper for Away / Default Reply
        async def try_fire_away_reply():
            # 1. STRICT ACCOUNT-TO-ACCOUNT (DM ONLY):
            if not event.is_private or getattr(event, 'is_group', False) or getattr(event, 'is_channel', False):
                return

            # 2. Never reply to Telegram official service messages (777000, 42777)
            if sender_id in (777000, 42777):
                return

            # 3. Never reply to Telegram bots
            if sender_is_bot:
                return

            default = db.get_default_rule(user_id)
            if not (default and default.get("active")):
                return

            # 10s cooldown per DM sender to prevent rapid-fire duplicates
            now_ts = time.time()
            last_away = _last_away_times.get((user_id, chat_id), 0)
            if now_ts - last_away < 10:
                return
            _last_away_times[(user_id, chat_id)] = now_ts

            delay = default.get("delay_seconds") or 0
            if delay > 0:
                await asyncio.sleep(delay)

            # Send reply message
            if default.get("reply_text"):
                reply_text = pick_reply_text(default["reply_text"])
                if reply_text:
                    try:
                        await event.reply(reply_text)
                    except Exception as e:
                        print(f"Default event.reply failed: {e}, falling back to send_message")
                        try:
                            await client.send_message(chat_id, reply_text)
                        except Exception as e2:
                            print(f"Default send_message failed: {e2}")

            # Send reaction if set
            if default.get("reaction_emoji"):
                await send_reaction(client, chat_id, event.message.id, default["reaction_emoji"], input_chat=input_chat)

        # 1. Fetch active rules for this chat
        rules = db.get_rules_for_chat(user_id, chat_id)
        matched_rules = evaluate_rules(rules, raw_text, sender_id, sender_username)

        # 2. If no active rule matched this message, fire Away / Default reply
        if not matched_rules:
            await try_fire_away_reply()
            return

        # 3. Execute all matching rules
        now = time.time()
        for rule in matched_rules:
            rule_id = rule["id"]
            cooldown = rule.get("cooldown_seconds") or 0
            if cooldown > 0:
                last_time = _last_reply_times.get((chat_id, rule_id), 0)
                if now - last_time < cooldown:
                    continue  # skip during cooldown
                _last_reply_times[(chat_id, rule_id)] = now

            base_delay = rule.get("delay_seconds") or 0
            if base_delay > 0:
                await asyncio.sleep(base_delay)

            # Send text reply
            if rule.get("reply_text"):
                reply_text = pick_reply_text(rule["reply_text"])
                if reply_text:
                    try:
                        await event.reply(reply_text)
                    except Exception as e:
                        print(f"Reply failed for rule {rule_id}: {e}, falling back to send_message")
                        try:
                            await client.send_message(chat_id, reply_text)
                        except Exception as e2:
                            print(f"send_message also failed for rule {rule_id}: {e2}")

            # Send reaction
            if rule.get("reaction_emoji"):
                await send_reaction(client, chat_id, event.message.id, rule["reaction_emoji"], input_chat=input_chat)

            db.add_log(rule_id, raw_text)

    _watching_clients[user_id] = client

