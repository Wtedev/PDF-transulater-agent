"""Multi-provider API keys with automatic fallback.

Each book runs with a KeyChain: an ordered list of credentials.
  shared_first (default): the site's shared Anthropic key, then the owner's own keys
  mine_first:             the owner's keys, then the shared key
  mine_only:              only the owner's keys
When a key runs out of credit (or is rejected), the chain marks it and moves to
the next one *for the rest of the book* — already finished segments are kept,
so the book simply continues on the next key.
"""
import base64
import contextvars
import logging
import os
import threading
import time
from dataclasses import dataclass, field

from . import config
from .db import ApiKey, SessionLocal, User

log = logging.getLogger("providers")

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "prefix": "sk-ant-",
                  "main": "claude-sonnet-5-5", "fast": "claude-haiku-5-5"},
    "openai": {"label": "OpenAI (GPT)", "prefix": "sk-",
               "main": os.getenv("OPENAI_MODEL_MAIN", "gpt-6.1-terra"),
               "fast": os.getenv("OPENAI_MODEL_FAST", "gpt-6-luna")},
    "openrouter": {"label": "OpenRouter", "prefix": "sk-or-",
                   "main": os.getenv("OPENROUTER_MODEL_MAIN", "openrouter/auto"),
                   "fast": os.getenv("OPENROUTER_MODEL_FAST", "openrouter/auto")},
}
FAST_AGENTS = {"router"}
SHARED_TOKENS_PER_USER = int(os.getenv("SHARED_TOKENS_PER_USER", "0"))   # 0 = unlimited
SHARED_COOLDOWN = 15 * 60
_shared_exhausted_until = 0.0


class KeyExhausted(Exception):
    """Out of credit / quota for this key."""


class KeyInvalid(Exception):
    """Key rejected or model not available for this key."""


class NoCreditLeft(Exception):
    """Every key in the chain is exhausted or invalid."""


@dataclass
class Cred:
    provider: str
    api_key: str
    label: str
    key_id: int | None = None          # None = the shared key
    model_main: str = ""
    model_fast: str = ""

    @property
    def shared(self):
        return self.key_id is None

    def model_for(self, agent: str) -> str:
        if self.shared:                 # the shared key keeps its per-agent configuration
            return {"router": config.MODEL_ROUTER, "ocr": config.MODEL_OCR, "glossary": config.MODEL_GLOSSARY,
                    "translator": config.MODEL_TRANSLATOR, "reviewer": config.MODEL_REVIEWER,
                    "editor": config.MODEL_EDITOR, "consistency": config.MODEL_CONSISTENCY}.get(agent, config.MODEL_TRANSLATOR)
        d = PROVIDERS[self.provider]
        if agent in FAST_AGENTS:
            return self.model_fast or d["fast"]
        return self.model_main or d["main"]


# ---------------------------------------------------------------- provider adapters

def _classify(provider: str, err: Exception):
    status = getattr(err, "status_code", None)
    body = str(getattr(err, "body", "") or err).lower()
    if status == 402 or "credit balance" in body or "insufficient_quota" in body \
            or "insufficient_credits" in body or "credit_limit" in body or "billing" in body:
        return KeyExhausted(body[:300])
    if status in (401, 403) or "invalid x-api-key" in body or "invalid_api_key" in body:
        return KeyInvalid("المفتاح مرفوض")
    if status == 404 or "model_not_found" in body or "does not exist" in body:
        return KeyInvalid("الموديل غير متاح لهذا المفتاح")
    return None


def _anthropic(cred: Cred, model, instructions, cached_context, content_parts, max_tokens):
    import anthropic
    client = anthropic.Anthropic(api_key=cred.api_key, max_retries=3)
    system = [{"type": "text", "text": instructions}]
    if cached_context:
        system.append({"type": "text", "text": cached_context, "cache_control": {"type": "ephemeral"}})
    content = []
    for kind, value in content_parts:
        if kind == "image":
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(value).decode()}})
        else:
            content.append({"type": "text", "text": value})
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text, resp.usage.input_tokens + resp.usage.output_tokens


def _openai_compatible(cred: Cred, model, instructions, cached_context, content_parts, max_tokens):
    from openai import OpenAI
    base = "https://openrouter.ai/api/v1" if cred.provider == "openrouter" else None
    client = OpenAI(api_key=cred.api_key, base_url=base, max_retries=3)
    system = instructions + ("\n\n" + cached_context if cached_context else "")
    content = []
    for kind, value in content_parts:
        if kind == "image":
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(value).decode()}})
        else:
            content.append({"type": "text", "text": value})
    kwargs = {"max_completion_tokens": max_tokens} if cred.provider == "openai" else {"max_tokens": max_tokens}
    resp = client.chat.completions.create(model=model, messages=[
        {"role": "system", "content": system}, {"role": "user", "content": content}], **kwargs)
    text = resp.choices[0].message.content or ""
    used = (resp.usage.prompt_tokens + resp.usage.completion_tokens) if resp.usage else 0
    return text, used


def raw_call(cred: Cred, agent: str, instructions: str, cached_context: str, content_parts, max_tokens: int):
    model = cred.model_for(agent)
    fn = _anthropic if cred.provider == "anthropic" else _openai_compatible
    try:
        return fn(cred, model, instructions, cached_context, content_parts, max_tokens)
    except Exception as e:
        mapped = _classify(cred.provider, e)
        if mapped:
            raise mapped from e
        raise


# ---------------------------------------------------------------- key chain

def _shared_cred() -> Cred | None:
    if not config.ANTHROPIC_API_KEY:
        return None
    return Cred("anthropic", config.ANTHROPIC_API_KEY, "المفتاح المشترك")


def user_creds(user_id: int) -> list[Cred]:
    from .security import decrypt
    with SessionLocal() as s:
        rows = s.query(ApiKey).filter(ApiKey.user_id == user_id, ApiKey.status != "invalid") \
            .order_by(ApiKey.position, ApiKey.id).all()
        out = []
        for r in rows:
            key = decrypt(r.secret)
            if key:
                out.append(Cred(r.provider, key, f"{PROVIDERS[r.provider]['label']} ••{r.last4}", r.id,
                                r.model_main, r.model_fast))
        return out


@dataclass
class KeyChain:
    user_id: int | None
    creds: list[Cred]
    used: list[str] = field(default_factory=list)
    _idx: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def for_user(cls, user_id: int | None) -> "KeyChain":
        order = "shared_first"
        if user_id:
            with SessionLocal() as s:
                u = s.get(User, user_id)
                order = (u.key_order if u else None) or "shared_first"
        mine = user_creds(user_id) if user_id else []
        shared = _shared_cred()
        if order == "mine_only" or not shared:
            creds = mine
        elif order == "mine_first":
            creds = mine + [shared]
        else:
            creds = [shared] + mine
        return cls(user_id, creds)

    def _current(self) -> Cred:
        with self._lock:
            while self._idx < len(self.creds):
                c = self.creds[self._idx]
                if c.shared and (time.time() < _shared_exhausted_until or self._over_quota()):
                    self._idx += 1
                    continue
                return c
        raise NoCreditLeft()

    def _over_quota(self) -> bool:
        if not SHARED_TOKENS_PER_USER or not self.user_id:
            return False
        with SessionLocal() as s:
            u = s.get(User, self.user_id)
            return bool(u and u.shared_tokens >= SHARED_TOKENS_PER_USER)

    def _drop(self, cred: Cred, status: str, note: str):
        global _shared_exhausted_until
        with self._lock:
            if self._idx < len(self.creds) and self.creds[self._idx] is cred:
                self._idx += 1
        log.warning("key %s %s: %s", cred.label, status, note[:200])
        if cred.shared:
            if status == "no_credit":
                _shared_exhausted_until = time.time() + SHARED_COOLDOWN
            return
        with SessionLocal() as s:
            row = s.get(ApiKey, cred.key_id)
            if row:
                row.status, row.status_note = status, note[:500]
                s.commit()

    def _account(self, cred: Cred, tokens: int):
        if cred.label not in self.used:
            self.used.append(cred.label)
        if not tokens:
            return
        with SessionLocal() as s:
            if cred.shared and self.user_id:
                u = s.get(User, self.user_id)
                if u:
                    u.shared_tokens = (u.shared_tokens or 0) + tokens
            elif not cred.shared:
                row = s.get(ApiKey, cred.key_id)
                if row:
                    row.tokens_used = (row.tokens_used or 0) + tokens
            s.commit()

    def call(self, agent, instructions, cached_context, content_parts, max_tokens) -> str:
        while True:
            cred = self._current()
            try:
                text, tokens = raw_call(cred, agent, instructions, cached_context, content_parts, max_tokens)
            except KeyExhausted as e:
                self._drop(cred, "no_credit", str(e))
                continue
            except KeyInvalid as e:
                self._drop(cred, "invalid", str(e))
                continue
            self._account(cred, tokens)
            return text


current_chain: contextvars.ContextVar[KeyChain | None] = contextvars.ContextVar("current_chain", default=None)


def test_key(provider: str, api_key: str, model: str = "") -> tuple[bool, str]:
    """Tiny real call used by the 'test key' button."""
    cred = Cred(provider, api_key, "test", key_id=-1, model_main=model, model_fast=model)
    try:
        text, _ = raw_call(cred, "translator", "Reply with the single word: OK", "", [("text", "ping")], 20)
        return True, f"يعمل ✓ ({cred.model_for('translator')})"
    except KeyExhausted:
        return False, "المفتاح صحيح لكن الرصيد منتهي"
    except KeyInvalid as e:
        return False, str(e)
    except Exception as e:
        return False, f"تعذر الاتصال: {type(e).__name__}"
