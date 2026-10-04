"""Cold-prompt guard (UserPromptSubmit).

When the cache lifetime has run out, the next request writes the whole conversation to the cache
again; in a large session that single request costs several dollars. The guard stops the prompt
once in this idle period, states the cost and suggests /compact first; if the user sends the
prompt again it goes through. An error does not stop the prompt: the CLI exits with code 1, which
Claude Code treats as a non-blocking error.

Only a human can send a stopped prompt again; prompts Claude Code sends itself (background
notifications, loops, subagents) are dropped if stopped. Claude Code 2.1.288 does not tell the hook
where a prompt comes from, so prompts that are evidently not from a human are never stopped:
subagent prompts (agent_id), commands that start with a slash (/compact, /loop), messages that
start with a tag (such as <task-notification>) and system notifications. Scheduled prompts that
arrive as plain text cannot be told apart from a human's.
"""

import json
from dataclasses import dataclass
from typing import Final

from cimrihook.claude import as_object, optional_str, require_str
from cimrihook.config import Config
from cimrihook.errors import HookPayloadError
from cimrihook.ledger import claim_guard, ledger_path
from cimrihook.simulate import claude_prices, usd_per_token
from cimrihook.statusline import compact_tokens
from cimrihook.tail import ONE_HOUR, SessionTail, read_session_tail

# Claude Code'un otomatik ilettiği istemlerin başı: bildirimler, etiketli iletiler, komutlar.
AUTOMATED_PREFIXES: Final = ("[SYSTEM NOTIFICATION", "<", "/")
GUARD_BUSY_TIMEOUT_SECONDS: Final = 2.0  # hook zaman aşımının (10 s) çok altında


@dataclass(frozen=True, slots=True)
class PromptEvent:
    """UserPromptSubmit yükünden kullanılan alanlar."""

    session_id: str
    transcript_path: str
    prompt: str
    agent_id: str | None  # alt ajanın istemi; ana oturumda yok


def guard_prompt(raw: str, config: Config, now: float) -> str:
    """UserPromptSubmit hook yanıtı: istem durdurulacaksa JSON karar, değilse boş metin."""
    event = parse_prompt_event(raw)
    if not config.guard_enabled or is_automated(event):
        return ""
    tail = read_session_tail(event.transcript_path)
    if tail is None or not cache_expired(tail, config.guard_min_tokens, now):
        return ""
    first = claim_guard(
        ledger_path(config.home),
        event.session_id,
        tail.last_response_at,
        now,
        GUARD_BUSY_TIMEOUT_SECONDS,
    )
    if not first:
        return ""
    return json.dumps({"decision": "block", "reason": guard_reason(tail, now)})


def is_automated(event: PromptEvent) -> bool:
    """İnsan dışı olduğu belli istem: durdurulursa yeniden gönderen olmaz."""
    return event.agent_id is not None or event.prompt.lstrip().startswith(AUTOMATED_PREFIXES)


def parse_prompt_event(raw: str) -> PromptEvent:
    """stdin'deki UserPromptSubmit yükünü ayrıştırır."""
    payload = parse_payload(raw, "UserPromptSubmit")
    return PromptEvent(
        session_id=require_str(payload, "session_id", "UserPromptSubmit"),
        transcript_path=require_str(payload, "transcript_path", "UserPromptSubmit"),
        prompt=require_str(payload, "prompt", "UserPromptSubmit"),
        agent_id=optional_str(payload, "agent_id", "UserPromptSubmit"),
    )


def parse_payload(raw: str, event: str) -> dict[str, object]:
    """Hook yükünü çözer ve beklenen olay adını doğrular."""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookPayloadError(
            f"{event} payload is not valid JSON ({error.msg}); first bytes: {raw[:120]!r}"
        ) from error
    payload = as_object(decoded, event)
    name = require_str(payload, "hook_event_name", event)
    if name != event:
        raise HookPayloadError(f"expected a {event} payload, got hook_event_name {name!r}")
    return payload


def cache_expired(tail: SessionTail, min_tokens: int, now: float) -> bool:
    """Önbellek soğudu mu ve yeniden yazılacak bağlam uyarmaya değecek kadar büyük mü?"""
    return tail.context_tokens >= min_tokens and now - tail.last_response_at > tail.ttl_seconds


def guard_reason(tail: SessionTail, now: float) -> str:
    """Kullanıcıya gösterilen açıklama: ne kadar süredir boşta, ne yeniden yazılacak, ne yapmalı."""
    base = usd_per_token(tail.model)
    prices = claude_prices(tail.model)
    weight = prices.write_1h if tail.ttl_seconds >= ONE_HOUR else prices.write_5m
    cost = (
        ""
        if base is None
        else f" (about ${tail.context_tokens * weight * base:.2f} at list prices)"
    )
    return (
        f"CimriHook: the prompt cache expired {idle_text(now - tail.last_response_at)} ago, so "
        f"this message would re-cache the whole {compact_tokens(tail.context_tokens)}-token "
        f"conversation{cost}. Run /compact first to continue from a short summary, or send your "
        "message again to go ahead."
    )


def idle_text(seconds: float) -> str:
    """Boşta geçen sürenin kısa hali (ör. 74 min, 3.5 h)."""
    return f"{seconds / 60:.0f} min" if seconds < 2 * ONE_HOUR else f"{seconds / ONE_HOUR:.1f} h"
