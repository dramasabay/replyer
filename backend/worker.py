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

# Cooldown cache for Mention reply: (user_id, chat_id) -> last_replied_timestamp
_last_mention_times = {}

# Track which user_ids already have a listener attached on a live client instance
_watching_clients = {}


async def send_reaction(client, chat_id, message_id, emoji, input_chat=None):
    """Telethon has no high-level send_reaction() method — this uses the
    raw API call directly. Resolves input entity safely to prevent entity lookup errors."""
    if not emoji:
        return
    target = input_chat if input_chat is not None else chat_id
    resolved_peer = target
    try:
        resolved_peer = await client.get_input_entity(target)
    except Exception:
        try:
            resolved_peer = await client.get_input_entity(chat_id)
        except Exception:
            pass

    try:
        await client(functions.messages.SendReactionRequest(
            peer=resolved_peer,
            msg_id=message_id,
            big=True,
            add_to_recent=True,
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


async def check_is_mention_or_reply(event, raw_text: str, owner_id: int, owner_username: str) -> bool:
    """Checks if incoming message in group or channel mentions or replies to the user."""
    # 0. Telegram native mentioned flag (set directly by Telegram servers)
    if getattr(event.message, 'mentioned', False):
        return True

    # 1. Text contains @username (case-insensitive)
    if owner_username and f"@{owner_username}" in raw_text.lower():
        return True

    # 2. Telegram message entities (MessageEntityMention, MessageEntityMentionName)
    entities = getattr(event.message, 'entities', None) or []
    for ent in entities:
        if isinstance(ent, types.MessageEntityMention):
            try:
                part = raw_text[ent.offset:ent.offset + ent.length].lower()
                if owner_username and part == f"@{owner_username}":
                    return True
            except Exception:
                pass
        elif isinstance(ent, types.MessageEntityMentionName):
            if owner_id and getattr(ent, 'user_id', None) == owner_id:
                return True

    # 3. Message is a direct reply to the user's message
    if getattr(event, 'is_reply', False):
        try:
            reply_msg = await event.get_reply_message()
            if reply_msg and getattr(reply_msg, 'sender_id', None) == owner_id:
                return True
        except Exception:
            pass

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


def evaluate_rules(rules: list, text: str, sender_id: int, sender_username: str = None, is_mention: bool = False) -> list:
    """Pure rule evaluation function: prioritizes specific rules (sender, mention, keyword)
    over generic catch-all ('all') rules. Returns all matching rules."""
    specific_rules = [r for r in rules if r.get("trigger_type") in ("sender", "mention", "keyword")]
    catch_all_rules = [r for r in rules if r.get("trigger_type") == "all"]

    matched = []

    # 1. Check specific rules first
    for rule in specific_rules:
        t_type = rule.get("trigger_type")
        t_val = rule.get("trigger_value")
        if t_type == "sender" and check_sender_match(t_val, sender_id, sender_username):
            matched.append(rule)
        elif t_type == "mention":
            if is_mention:
                # If trigger_value is provided, also require matching keyword; otherwise any mention matches!
                if not t_val or not str(t_val).strip() or check_keyword_match(t_val, text, rule.get("match_mode") or "any"):
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
        owner_username = (me.username or "").lower() if (me and getattr(me, 'username', None)) else ""
    except Exception:
        owner_id = None
        owner_username = ""

    if not owner_id:
        u_row = db.get_user(user_id)
        if u_row and u_row.get("telegram_user_id"):
            owner_id = u_row["telegram_user_id"]

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

        # Detect if message is in a group or channel, and if it mentions the user
        is_group_or_channel = bool(getattr(event, 'is_group', False) or getattr(event, 'is_channel', False))
        is_mention = False
        if is_group_or_channel:
            is_mention = await check_is_mention_or_reply(event, raw_text, owner_id, owner_username)

        # Helper for Away / Default Reply (Private 1-on-1 DMs only)
        async def try_fire_away_reply():
            # 1. STRICT ACCOUNT-TO-ACCOUNT (DM ONLY):
            if not event.is_private or is_group_or_channel:
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
            replied_any = False
            if default.get("reply_text"):
                reply_text = pick_reply_text(default["reply_text"])
                if reply_text:
                    try:
                        await event.reply(reply_text)
                        replied_any = True
                    except Exception as e:
                        print(f"Default event.reply failed: {e}, falling back to send_message")
                        try:
                            await client.send_message(chat_id, reply_text)
                            replied_any = True
                        except Exception as e2:
                            print(f"Default send_message failed: {e2}")

            # Send reaction if set
            reacted_any = False
            if default.get("reaction_emoji"):
                await send_reaction(client, chat_id, event.message.id, default["reaction_emoji"], input_chat=input_chat)
                reacted_any = True

            # Log away reply activity
            action_parts = []
            if reacted_any and default.get("reaction_emoji"):
                action_parts.append(f"Reacted {default['reaction_emoji']}")
            if replied_any:
                action_parts.append("Replied text")
            action_desc = " + ".join(action_parts) if action_parts else "Away auto-replied"

            chat_title = None
            try:
                sender = await event.get_sender()
                if sender:
                    chat_title = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip() or getattr(sender, 'username', None)
            except Exception:
                pass
            if not chat_title:
                chat_title = f"DM ({chat_id})"

            db.add_log(rule_id=None, message_snippet=raw_text, user_id=user_id, chat_name=chat_title, action_desc=action_desc)

        # Helper for Group & Channel Mention Auto-Reply
        async def try_fire_mention_reply():
            if not is_group_or_channel or not is_mention:
                return

            if sender_id in (777000, 42777) or sender_is_bot:
                return

            mention_cfg = db.get_mention_rule(user_id)
            if not (mention_cfg and mention_cfg.get("active")):
                return

            # If user selected specific groups/channels, only fire if chat_id is selected
            target_chats = mention_cfg.get("target_chats")
            if target_chats and isinstance(target_chats, list) and len(target_chats) > 0:
                # Compare both raw chat_id and string / int representations
                if chat_id not in target_chats and int(chat_id) not in [int(c) for c in target_chats if str(c).replace('-','').isdigit()]:
                    return

            cooldown = mention_cfg.get("cooldown_seconds") or 30
            now_ts = time.time()
            last_time = _last_mention_times.get((user_id, chat_id), 0)
            if cooldown > 0 and (now_ts - last_time < cooldown):
                return
            _last_mention_times[(user_id, chat_id)] = now_ts

            delay = mention_cfg.get("delay_seconds") or 0
            if delay > 0:
                await asyncio.sleep(delay)

            # Send reply message
            replied_any = False
            if mention_cfg.get("reply_text"):
                reply_text = pick_reply_text(mention_cfg["reply_text"])
                if reply_text:
                    try:
                        await event.reply(reply_text)
                        replied_any = True
                    except Exception as e:
                        print(f"Mention event.reply failed: {e}, falling back to send_message")
                        try:
                            await client.send_message(chat_id, reply_text)
                            replied_any = True
                        except Exception as e2:
                            print(f"Mention send_message failed: {e2}")

            # Send reaction if set
            reacted_any = False
            if mention_cfg.get("reaction_emoji"):
                await send_reaction(client, chat_id, event.message.id, mention_cfg["reaction_emoji"], input_chat=input_chat)
                reacted_any = True

            # Log this mention auto-reply activity
            action_parts = []
            if reacted_any and mention_cfg.get("reaction_emoji"):
                action_parts.append(f"Reacted {mention_cfg['reaction_emoji']}")
            if replied_any:
                action_parts.append("Replied text")
            action_desc = " + ".join(action_parts) if action_parts else "Mention auto-replied"

            chat_title = None
            try:
                chat_obj = await event.get_chat()
                if chat_obj:
                    chat_title = getattr(chat_obj, 'title', None) or getattr(chat_obj, 'username', None)
            except Exception:
                pass
            if not chat_title:
                chat_title = f"Group/Channel ({chat_id})"

            db.add_log(rule_id=None, message_snippet=raw_text, user_id=user_id, chat_name=chat_title, action_desc=action_desc)

        # 1. Fetch active rules for this chat
        rules = db.get_rules_for_chat(user_id, chat_id)
        matched_rules = evaluate_rules(rules, raw_text, sender_id, sender_username, is_mention=is_mention)

        # 2. In groups/channels when mentioned, if no chat-specific rule (sender/mention/keyword) matched,
        # Section 05 Group Mention Reply takes priority over generic 'all' catch-all rules!
        has_specific_rule = any(r.get("trigger_type") in ("sender", "mention", "keyword") for r in matched_rules)
        if is_group_or_channel and is_mention and not has_specific_rule:
            mention_cfg = db.get_mention_rule(user_id)
            if mention_cfg and mention_cfg.get("active"):
                target_chats = mention_cfg.get("target_chats")
                target_match = True
                if target_chats and isinstance(target_chats, list) and len(target_chats) > 0:
                    if chat_id not in target_chats and int(chat_id) not in [int(c) for c in target_chats if str(c).replace('-','').isdigit()]:
                        target_match = False
                if target_match:
                    await try_fire_mention_reply()
                    return

        # 3. If no active rule matched this message, fire Away / Default or Mention reply
        if not matched_rules:
            if event.is_private:
                await try_fire_away_reply()
            elif is_group_or_channel and is_mention:
                await try_fire_mention_reply()
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

            db.add_log(rule_id, raw_text, user_id=user_id, chat_name=rule.get("chat_name"))

    _watching_clients[user_id] = client

