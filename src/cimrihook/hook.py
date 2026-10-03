"""Hook olaylarını uçtan uca işler: Claude Code yükü → defter → codec → yanıt."""

import json
from pathlib import Path
from typing import Final

from cimrihook.claude import (
    MUTATING_TOOLS,
    ContextReset,
    Session,
    ToolResult,
    context_key,
    is_unchanged_read,
    message_output,
    observe,
    observe_read_from_disk,
    parse_event,
    post_tool_use_output,
    read_output,
    read_request,
    transcript_has_new_boundary,
    transcript_size,
)
from cimrihook.codec import agent_wants_raw, decide, raw_tokens
from cimrihook.config import Config
from cimrihook.ledger import BUSY_TIMEOUT_SECONDS, Ledger
from cimrihook.model import Decision, Encoding

LEDGER_FILE: Final = "ledger.sqlite3"


def ledger_path(home: Path) -> Path:
    """Defter veritabanının yolu."""
    return home / LEDGER_FILE


def run_hook(raw_payload: str, config: Config) -> str:
    """Hook çağrısını işler; Claude Code'a yazılacak stdout metnini döndürür ('' = karar yok)."""
    event = parse_event(raw_payload)
    if isinstance(event, ContextReset):
        reset_context(event, config)
        return ""
    return encode_tool_result(event, config)


def reset_context(event: ContextReset, config: Config) -> None:
    """Sıkıştırma ya da oturum (yeniden) başlangıcı: önceki kuşağın bilgisi artık geçersiz."""
    with Ledger(ledger_path(config.home), BUSY_TIMEOUT_SECONDS) as ledger:
        ledger.reset_session(event.session_id, transcript_size(event.transcript_path))


def encode_tool_result(event: ToolResult, config: Config) -> str:
    """Araç sonucunu codec ile kodlar ve gerekiyorsa updatedToolOutput yanıtı üretir."""
    key = context_key(event.session)
    with Ledger(ledger_path(config.home), BUSY_TIMEOUT_SECONDS) as ledger:
        generation = current_generation(ledger, event.session)
        step = ledger.next_step(key, generation)
        if is_unchanged_read(event.tool_name, event.tool_response):
            return rehydrate_unchanged_read(ledger, event, key, generation, step)
        mutating = event.tool_name in MUTATING_TOOLS
        obs = observe(event.tool_name, event.tool_input, event.tool_response, event.session.cwd)
        if obs is None:
            ledger.record_step(key, generation, step, event.tool_name, event.tool_use_id, mutating)
            return ""
        previous = ledger.latest_request_view(key, generation, obs.request_key)
        mutated = previous is not None and ledger.mutated_since(
            key, generation, previous.step, obs.request_key
        )
        views = ledger.stream_views(key, generation, obs.stream)
        decision = decide(obs, views, previous, mutated, config.codec)
        ledger.record_step(key, generation, step, event.tool_name, obs.request_key, mutating)
        ledger.record_view(event.session.session_id, key, generation, step, obs, decision)
    if decision.encoding is Encoding.RAW:
        return ""
    updated = message_output(obs, event.tool_response, decision.message)
    return json.dumps(post_tool_use_output(updated))


def current_generation(ledger: Ledger, session: Session) -> int:
    """Transcript'e yeni sıkıştırma sınırı yazıldıysa kuşağı ilerletir; güncel kuşağı döndürür."""
    size = transcript_size(session.transcript_path)
    state = ledger.session_state(session.session_id, size)
    if transcript_has_new_boundary(session.transcript_path, state.transcript_offset, size):
        ledger.bump_generation(session.session_id)
    ledger.set_transcript_offset(session.session_id, size)
    return ledger.session_state(session.session_id, size).generation


def rehydrate_unchanged_read(
    ledger: Ledger, event: ToolResult, key: str, generation: int, step: int
) -> str:
    """Claude Code'un yerel 'dosya değişmedi' yanıtını gerektiğinde gerçek içerikle değiştirir.

    Ajan içeriği bizden yalnızca iskelet ya da referans olarak aldıysa ve aynı isteği ısrarla
    tekrarlıyorsa yerel yanıt yanlış yönlendirir; içerik diskten okunup ham iletilir.
    """
    request = read_request(event.tool_input, event.session.cwd)
    previous = ledger.latest_request_view(key, generation, request.key)
    mutated = previous is not None and ledger.mutated_since(
        key, generation, previous.step, request.key
    )
    ledger.record_step(key, generation, step, event.tool_name, request.key, False)
    if not agent_wants_raw(previous, mutated):
        return ""
    obs = observe_read_from_disk(request)
    raw = raw_tokens(obs)
    decision = Decision(Encoding.RAW, "", raw, raw)
    ledger.record_view(event.session.session_id, key, generation, step, obs, decision)
    updated = read_output(request.path, obs.lines, obs.start_line, obs.total_lines)
    return json.dumps(post_tool_use_output(updated))
