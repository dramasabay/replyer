"""
One event listener per active user. When a new message arrives in a chat
that has active rules, evaluate specific rules first (sender, keyword), then
catch-all rules, and execute all matching rules with natural delays and rotating replies.
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

# Track which user_ids already have a listener attached, so we don't
# register duplicate handlers if start_watching() is called more than once.
_watching = set()


async def send_reaction(client, chat_id, message_id, emoji):
    """Telethon has no high-level send_reaction() method — this uses the
    raw API call directly, which is the actual supported way to do it."""
    await client(functions.messages.SendReactionRequest(
        peer=chat_id,
        msg_id=message_id,
        reaction=[types.ReactionEmoji(emoticon=emoji)],
    ))


def check_keyword_match(trigger_value: str, text: str, match_mode: str = "any") -> bool:
    """Checks if text matches keyword(s). Supports multiple keywords separated by commas or newlines."""
    if not trigger_value or text is None:
        return False
    # Split by comma or newline
    keywords = [k.strip().lower() for k in re.split(r'[\r\n,]+', str(trigger_value)) if k.strip()]
    if not keywords:
        return False

    text_lower = text.lower()
    if match_mode == "all":
        return all(k in text_lower for k in keywords)
    else:  # default 'any' (OR)
        return any(k in text_lower for k in keywords)


def check_sender_match(trigger_value: str, sender_id: int) -> bool:
    """Checks if sender_id matches trigger_value. Supports multiple sender IDs separated by commas or newlines."""
    if not trigger_value or sender_id is None:
        return False
    senders = [s.strip() for s in re.split(r'[\r\n,]+', str(trigger_value)) if s.strip()]
    for s in senders:
        try:
            if int(s) == sender_id:
                return True
        except (TypeError, ValueError):
            continue
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


def evaluate_rules(rules: list, text: str, sender_id: int) -> list:
    """Pure rule evaluation function: prioritizes specific rules (sender, keyword)
    over generic catch-all ('all') rules. Returns all matching rules."""
    specific_rules = [r for r in rules if r.get("trigger_type") in ("sender", "keyword")]
    catch_all_rules = [r for r in rules if r.get("trigger_type") == "all"]

    matched = []

    # 1. Check specific rules first
    for rule in specific_rules:
        t_type = rule.get("trigger_type")
        t_val = rule.get("trigger_value")
        if t_type == "sender" and check_sender_match(t_val, sender_id):
            matched.append(rule)
        elif t_type == "keyword":
            mode = rule.get("match_mode") or "any"
            if check_keyword_match(t_val, text, mode):
                matched.append(rule)

    # 2. If NO specific rules matched, fall back to catch-all ('all') rules
    if not matched and catch_all_rules:
        matched.extend(catch_all_rules)

    return matched


async def start_watching(user_id: int):
    if user_id in _watching:
        return  # already listening

    try:
        client = await telegram_manager.get_client(user_id)
    except RuntimeError:
        return  # no saved session yet, nothing to watch

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        # Never reply to outgoing messages sent by the account owner
        if getattr(event, 'out', False):
            return

        chat_id = event.chat_id
        sender_id = event.sender_id
        raw_text = event.raw_text or ""

        # Never reply to self or Saved Messages
        if sender_id == user_id or chat_id == user_id:
            return

        # Check if user is approved by admin
        user_row = db.get_user(user_id)
        if user_row and user_row.get("is_approved") == 0:
            return

        # Track user active timestamp
        db.update_user_active(user_id)

        # Dedicated helper for firing Away / Default Reply
        async def try_fire_away_reply():
            # 1. STRICT ACCOUNT-TO-ACCOUNT (DM ONLY):
            # Must be a 1-on-1 private chat. NEVER groups, supergroups, broadcast channels.
            if not event.is_private or getattr(event, 'is_group', False) or getattr(event, 'is_channel', False):
                return

            # 2. Never reply to Telegram official service messages (777000, 42777)
            if sender_id in (777000, 42777):
                return

            # 3. Never reply to Telegram bots — strictly account-to-account (real human user)
            try:
                sender = await event.get_sender()
                if sender and getattr(sender, 'bot', False):
                    return
            except Exception:
                pass

            default = db.get_default_rule(user_id)
            if not (default and default.get("active")):
                return

            # 4. Realtime fast reply: 0 delay means immediate dispatch without sleeping
            delay = default.get("delay_seconds") or 0
            if delay > 0:
                await asyncio.sleep(delay)

            # Send reply message immediately in realtime
            if default.get("reply_text"):
                reply_text = pick_reply_text(default["reply_text"])
                if reply_text:
                    try:
                        await event.reply(reply_text)
                    except Exception as e:
                        print(f"Default reply failed: {e}")

            # Send reaction if set
            if default.get("reaction_emoji"):
                try:
                    await send_reaction(client, chat_id, event.message.id, default["reaction_emoji"])
                except Exception as e:
                    print(f"Default reaction failed: {e}")

        rules = db.get_rules_for_chat(user_id, chat_id)

        # Fallback check if no rules exist at all for this chat
        if not rules:
            if not db.chat_has_any_rule(user_id, chat_id):
                await try_fire_away_reply()
            return

        # Evaluate rules with multi-rule matching
        matched_rules = evaluate_rules(rules, raw_text, sender_id)

        if not matched_rules:
            # If rules exist for chat but none matched this message,
            # only fall back to default if chat doesn't have any specific rules configured
            if not db.chat_has_any_rule(user_id, chat_id):
                await try_fire_away_reply()
            return

        # Execute all matching rules in realtime
        now = time.time()
        for rule in matched_rules:
            rule_id = rule["id"]
            cooldown = rule.get("cooldown_seconds") or 0
            if cooldown > 0:
                last_time = _last_reply_times.get((chat_id, rule_id), 0)
                if now - last_time < cooldown:
                    continue  # skip during cooldown
                _last_reply_times[(chat_id, rule_id)] = now

            # Realtime delay: if 0, reply immediately with no artificial jitter
            base_delay = rule.get("delay_seconds") or 0
            if base_delay > 0:
                await asyncio.sleep(base_delay)

            # Send text reply first for fastest realtime response
            if rule.get("reply_text"):
                reply_text = pick_reply_text(rule["reply_text"])
                if reply_text:
                    try:
                        await event.reply(reply_text)
                    except Exception as e:
                        print(f"Reply failed for rule {rule_id}: {e}")

            # Send reaction
            if rule.get("reaction_emoji"):
                try:
                    await send_reaction(client, chat_id, event.message.id, rule["reaction_emoji"])
                except Exception as e:
                    print(f"Reaction failed for rule {rule_id}: {e}")

            db.add_log(rule_id, raw_text)

    _watching.add(user_id)

