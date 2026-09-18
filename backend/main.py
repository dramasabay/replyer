import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import telegram_manager
import worker
import auth_utils

app = FastAPI(
    title="Telegram Auto-Reply",
    docs_url=None,       # Disable /docs to prevent API attack surface enumeration
    redoc_url=None,      # Disable /redoc
    openapi_url=None,    # Disable /openapi.json schema disclosure
)

# The phone number treated as admin.
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")

# Configurable CORS. No wildcard default: an unset ALLOWED_ORIGINS should fail
# closed (same-origin only via the nginx proxy) rather than fail open to "*".
# Set ALLOWED_ORIGINS to a comma-separated list of exact origins if you ever
# serve the frontend from a different origin than the API.
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# We authenticate with a Bearer token in the Authorization header, never
# cookies, so allow_credentials (which governs cookie/credentialed requests)
# should stay off — it also has no effect combined with a "*" origin, so
# leaving it on gave no real protection and just added confusion.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_client_ip(request: Request) -> str:
    """Extract real client IP behind reverse proxy (NPM/Nginx), falling back to peer host."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return get_remote_address(request)


# Rate limiting: keyed by real client IP, applied to the auth endpoints below so
# the Telegram login flow can't be used to brute-force codes or to spam
# send_code_request at arbitrary phone numbers.
limiter = Limiter(key_func=get_client_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

security = HTTPBearer(auto_error=False)


# ---------- Security Dependencies ----------

async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Security(security)) -> dict:
    """Validate Bearer access token and return current authenticated user."""
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authentication required. Please log in.")
    
    token = credentials.credentials
    try:
        payload = auth_utils.verify_access_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user_id = payload.get("user_id")
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User account not found.")

    if user.get("is_approved") == 0:
        raise HTTPException(status_code=403, detail="Account is blocked or pending approval by admin.")

    return user


async def require_admin_user(current_user: dict = Depends(get_current_user)) -> dict:
    """Ensure current authenticated user is the designated admin."""
    if not telegram_manager.is_admin_phone(current_user.get("phone", "")):
        raise HTTPException(status_code=403, detail="Access denied: Admin privileges required.")
    return current_user


@app.on_event("startup")
async def startup():
    db.init_db()
    for user_id in db.get_all_active_user_ids():
        try:
            await worker.start_watching(user_id)
        except Exception as e:
            print(f"Failed to start watching user {user_id} at startup: {e}")


# ---------- Auth (Telegram login) ----------

class StartLoginBody(BaseModel):
    phone: str
    api_id: int
    api_hash: str


class VerifyLoginBody(BaseModel):
    phone: str
    code: str
    password: Optional[str] = None


@app.post("/auth/login/start")
@limiter.limit("5/minute")
async def login_start(request: Request, body: StartLoginBody):
    return await telegram_manager.start_login(body.phone, body.api_id, body.api_hash)


@app.post("/auth/login/verify")
@limiter.limit("10/minute")
async def login_verify(request: Request, body: VerifyLoginBody):
    result = await telegram_manager.verify_login(body.phone, body.code, body.password)
    if result["status"] == "ok":
        user_id = result["user_id"]
        is_admin = bool(result.get("is_admin"))
        token = auth_utils.create_access_token(user_id=user_id, phone=body.phone, is_admin=is_admin)
        result["token"] = token
        result["access_token"] = token
        await worker.start_watching(user_id)
    return result


# ---------- Chats ----------

@app.get("/users/{user_id}/chats")
async def get_chats(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: cannot access another user's chats.")
    try:
        return await telegram_manager.list_dialogs(user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/users/{user_id}/chats/resolve")
async def resolve_chat(user_id: int, query: str, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    try:
        return await telegram_manager.resolve_chat(user_id, query)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/users/{user_id}/chats/{chat_id}/members")
async def get_members(user_id: int, chat_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    try:
        return await telegram_manager.list_members(user_id, chat_id)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ---------- Rules ----------

class ChatItem(BaseModel):
    chat_id: int
    chat_name: str


class CreateRuleBody(BaseModel):
    chat_id: Optional[int] = None
    chat_name: Optional[str] = None
    chats: Optional[list[ChatItem]] = None
    is_global: Optional[bool] = False
    trigger_type: str
    trigger_value: Optional[str] = None
    reply_text: Optional[str] = None
    reaction_emoji: Optional[str] = None
    delay_seconds: int = 5
    match_mode: Optional[str] = "any"
    cooldown_seconds: Optional[int] = 0


@app.post("/users/{user_id}/rules")
async def add_rule(user_id: int, body: CreateRuleBody, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: cannot create rules for another user.")

    if not body.reply_text and not body.reaction_emoji:
        raise HTTPException(status_code=400, detail="Set a reply message, a reaction, or both.")

    # 1. Global rule
    if body.is_global or body.chat_id == 0:
        rule_id = db.create_rule(
            user_id=user_id,
            chat_id=0,
            chat_name="All Chats (Global)",
            trigger_type=body.trigger_type,
            trigger_value=body.trigger_value,
            reply_text=body.reply_text,
            delay_seconds=body.delay_seconds,
            reaction_emoji=body.reaction_emoji,
            match_mode=body.match_mode or "any",
            cooldown_seconds=body.cooldown_seconds or 0,
        )
        await worker.start_watching(user_id)
        return {"rule_id": rule_id, "count": 1}

    # 2. Multi-chat creation
    if body.chats and len(body.chats) > 0:
        chat_dicts = [{"chat_id": c.chat_id, "chat_name": c.chat_name} for c in body.chats]
        rule_ids = db.create_batch_rules(
            user_id=user_id,
            chats=chat_dicts,
            trigger_type=body.trigger_type,
            trigger_value=body.trigger_value,
            reply_text=body.reply_text,
            delay_seconds=body.delay_seconds,
            reaction_emoji=body.reaction_emoji,
            match_mode=body.match_mode or "any",
            cooldown_seconds=body.cooldown_seconds or 0,
        )
        await worker.start_watching(user_id)
        return {"rule_ids": rule_ids, "count": len(rule_ids), "rule_id": rule_ids[0] if rule_ids else None}

    # 3. Single chat creation
    if body.chat_id is None:
        raise HTTPException(status_code=400, detail="Select at least one chat or choose 'All Chats'.")

    rule_id = db.create_rule(
        user_id=user_id,
        chat_id=body.chat_id,
        chat_name=body.chat_name or "Chat",
        trigger_type=body.trigger_type,
        trigger_value=body.trigger_value,
        reply_text=body.reply_text,
        delay_seconds=body.delay_seconds,
        reaction_emoji=body.reaction_emoji,
        match_mode=body.match_mode or "any",
        cooldown_seconds=body.cooldown_seconds or 0,
    )
    await worker.start_watching(user_id)
    return {"rule_id": rule_id, "count": 1}


@app.get("/users/{user_id}/rules")
async def get_rules(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    return db.list_rules(user_id)


class UpdateRuleBody(BaseModel):
    reply_text: Optional[str] = None
    reaction_emoji: Optional[str] = None
    delay_seconds: Optional[int] = None
    trigger_type: Optional[str] = None
    trigger_value: Optional[str] = None
    match_mode: Optional[str] = None
    cooldown_seconds: Optional[int] = None
    active: Optional[bool] = None


@app.put("/rules/{rule_id}")
@app.patch("/rules/{rule_id}")
async def update_rule_endpoint(rule_id: int, body: UpdateRuleBody, current_user: dict = Depends(get_current_user)):
    rule = db.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found.")
    if rule["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden: cannot edit another user's rule.")

    success = db.update_rule(
        rule_id=rule_id,
        reply_text=body.reply_text,
        reaction_emoji=body.reaction_emoji,
        delay_seconds=body.delay_seconds,
        trigger_type=body.trigger_type,
        trigger_value=body.trigger_value,
        match_mode=body.match_mode,
        cooldown_seconds=body.cooldown_seconds,
        active=body.active,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Rule not found or no fields to update.")
    await worker.start_watching(current_user["id"])
    return {"status": "ok"}


@app.patch("/rules/{rule_id}/active")
async def toggle_rule_active_endpoint(rule_id: int, active: bool, current_user: dict = Depends(get_current_user)):
    rule = db.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found.")
    if rule["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden.")
    db.set_rule_active(rule_id, active)
    if active:
        await worker.start_watching(current_user["id"])
    return {"status": "ok"}


@app.delete("/rules/{rule_id}")
async def delete_rule_endpoint(rule_id: int, current_user: dict = Depends(get_current_user)):
    rule = db.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found.")
    if rule["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden: cannot delete another user's rule.")
    db.delete_rule(rule_id)
    return {"status": "ok"}


class BulkToggleBody(BaseModel):
    active: bool


@app.post("/users/{user_id}/rules/bulk-toggle")
async def bulk_toggle_rules(user_id: int, body: BulkToggleBody, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    count = db.toggle_all_rules(user_id, body.active)
    if body.active:
        await worker.start_watching(user_id)
    return {"status": "ok", "affected": count}


class BulkDeleteBody(BaseModel):
    inactive_only: bool = False


@app.post("/users/{user_id}/rules/bulk-delete")
async def bulk_delete_rules(user_id: int, body: BulkDeleteBody, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    count = db.delete_all_rules(user_id, body.inactive_only)
    return {"status": "ok", "deleted": count}


class TestRuleBody(BaseModel):
    chat_id: int
    text: str
    sender_id: Optional[int] = None
    is_mention: Optional[bool] = False


@app.post("/users/{user_id}/rules/test")
@app.post("/users/{user_id}/simulate")
async def test_rule_matcher(user_id: int, body: TestRuleBody, current_user: dict = Depends(get_current_user)):
    """Simulate which rule(s) would match an incoming message in a chat."""
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")

    rules = db.get_rules_for_chat(user_id, body.chat_id)
    matched = worker.evaluate_rules(rules, body.text, body.sender_id or 0, is_mention=bool(body.is_mention))
    
    results = []
    for r in matched:
        results.append({
            "rule_id": r["id"],
            "chat_name": r["chat_name"],
            "trigger_type": r["trigger_type"],
            "trigger_value": r["trigger_value"],
            "reply_text": worker.pick_reply_text(r["reply_text"]) if r["reply_text"] else None,
            "reaction_emoji": r["reaction_emoji"],
            "delay_seconds": r["delay_seconds"],
        })
    
    will_fallback_to_default = False
    default_info = None
    will_fallback_to_mention = False
    mention_info = None

    if not results:
        if body.is_mention:
            mention = db.get_mention_rule(user_id)
            if mention and mention.get("active"):
                will_fallback_to_mention = True
                mention_info = {
                    "reply_text": worker.pick_reply_text(mention["reply_text"]) if mention["reply_text"] else None,
                    "reaction_emoji": mention["reaction_emoji"],
                    "delay_seconds": mention["delay_seconds"],
                    "cooldown_seconds": mention.get("cooldown_seconds", 30),
                }
        elif not db.chat_has_any_rule(user_id, body.chat_id):
            default = db.get_default_rule(user_id)
            if default and default.get("active"):
                will_fallback_to_default = True
                default_info = {
                    "reply_text": worker.pick_reply_text(default["reply_text"]) if default["reply_text"] else None,
                    "reaction_emoji": default["reaction_emoji"],
                    "delay_seconds": default["delay_seconds"],
                }

    fallback_text = None
    if results:
        fallback_text = results[0]["reply_text"]
    elif mention_info:
        fallback_text = mention_info["reply_text"]
    elif default_info:
        fallback_text = default_info["reply_text"]

    return {
        "matched": len(results) > 0 or will_fallback_to_default or will_fallback_to_mention,
        "matched_rules": results,
        "match_count": len(results),
        "will_fallback_to_default": will_fallback_to_default,
        "default_rule": default_info,
        "will_fallback_to_mention": will_fallback_to_mention,
        "mention_rule": mention_info,
        "reply_text": fallback_text
    }


# ---------- Default / away rule ----------

class DefaultRuleBody(BaseModel):
    reply_text: Optional[str] = None
    reaction_emoji: Optional[str] = None
    delay_seconds: int = 5
    active: bool = True


@app.post("/users/{user_id}/default-rule")
async def save_default_rule(user_id: int, body: DefaultRuleBody, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    if body.active and not body.reply_text and not body.reaction_emoji:
        raise HTTPException(status_code=400, detail="Set a reply message, a reaction, or both.")
    db.upsert_default_rule(user_id, body.reply_text, body.reaction_emoji, body.delay_seconds, body.active)
    if body.active:
        await worker.start_watching(user_id)
    return {"status": "ok"}


@app.get("/users/{user_id}/default-rule")
async def read_default_rule(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    rule = db.get_default_rule(user_id)
    return rule or {}


# ---------- Mention rule (Groups & Channels) ----------

class MentionRuleBody(BaseModel):
    reply_text: Optional[str] = None
    reaction_emoji: Optional[str] = None
    delay_seconds: int = 0
    cooldown_seconds: int = 30
    active: bool = True
    target_chats: Optional[list[int]] = None


@app.post("/users/{user_id}/mention-rule")
async def save_mention_rule(user_id: int, body: MentionRuleBody, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    if body.active and not body.reply_text and not body.reaction_emoji:
        raise HTTPException(status_code=400, detail="Set a reply message, a reaction, or both.")
    db.upsert_mention_rule(
        user_id=user_id,
        reply_text=body.reply_text,
        reaction_emoji=body.reaction_emoji,
        delay_seconds=body.delay_seconds,
        cooldown_seconds=body.cooldown_seconds,
        active=body.active,
        target_chats=body.target_chats,
    )
    if body.active:
        await worker.start_watching(user_id)
    return {"status": "ok"}


@app.get("/users/{user_id}/mention-rule")
async def read_mention_rule(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    rule = db.get_mention_rule(user_id)
    return rule or {}


@app.get("/users/{user_id}/stats")
async def get_stats(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    rules = db.list_rules(user_id)
    logs = db.list_logs(user_id, limit=1000)
    default = db.get_default_rule(user_id)
    mention = db.get_mention_rule(user_id)
    active_cnt = len([r for r in rules if r.get("active")])
    return {
        "active_rules": active_cnt,
        "active_rules_count": active_cnt,
        "total_chats_covered": len(set(r["chat_id"] for r in rules)),
        "total_replies_sent": len(logs),
        "away_reply_on": bool(default and default.get("active")),
        "mention_reply_on": bool(mention and mention.get("active")),
    }


# ---------- Logs ----------

@app.get("/users/{user_id}/logs")
async def get_logs(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    return db.list_logs(user_id)


# ---------- Admin Endpoints (Strictly Protected) ----------

class UserStatusBody(BaseModel):
    is_approved: bool


@app.get("/admin/users")
async def admin_list_users(current_admin: dict = Depends(require_admin_user)):
    """Returns all connected users + activity stats for admin tracking. Protected by admin token."""
    users = db.list_all_users_with_stats()
    overview = db.admin_overview()
    return {
        "total_users": overview["total_users"],
        "total_replies_sent": overview["total_replies_sent"],
        "users": users,
    }


@app.patch("/admin/users/{target_user_id}/status")
async def admin_set_user_status(target_user_id: int, body: UserStatusBody, current_admin: dict = Depends(require_admin_user)):
    """Admin endpoint to approve or block a user."""
    db.set_user_approval(target_user_id, body.is_approved)
    return {"status": "ok", "user_id": target_user_id, "is_approved": body.is_approved}


@app.get("/users/{user_id}/is-admin")
async def is_admin(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden.")
    return {"is_admin": telegram_manager.is_admin_phone(current_user.get("phone", ""))}


@app.get("/admin/overview")
async def admin_overview_endpoint(current_admin: dict = Depends(require_admin_user)):
    return db.admin_overview()
