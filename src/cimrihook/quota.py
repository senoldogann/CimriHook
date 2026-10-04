"""Mevcut CLI bağlantısından model isteği göndermeden abonelik pencerelerini oku."""

import hashlib
import json
import math
import os
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final

from cimrihook.errors import CimriHookError

MAX_FRAME_BYTES: Final = 1_048_576


class QuotaError(CimriHookError):
    """CLI kontrol bağlantısı veya provider limit yanıtı geçersiz."""


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """Provider'ın bildirdiği yüzde; olmayan süre/sıfırlama bilgisi tahmin edilmez."""

    id: str
    used_percent: float
    resets_at: str | None
    duration_minutes: int | None


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """Hesap geneli gözlem; tek bir görevin tüketimi veya USD ölçümü değildir."""

    provider: str
    observed_at: str
    available: bool
    account_fingerprint: str | None
    windows: tuple[QuotaWindow, ...]


def object_value(value: object, where: str) -> dict[str, object]:
    """JSON nesnesini daralt; bozuk protokol verisini boş kayıt gibi kabul etme."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QuotaError(f"{where}: expected a JSON object")
    return {str(key): item for key, item in value.items()}


def percent_value(value: object, where: str) -> float:
    """Sonlu 0–100 yüzdesini ondalıklarını kaybetmeden doğrula."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QuotaError(f"{where}: expected a numeric utilization")
    percent = float(value)
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise QuotaError(f"{where}: utilization must be finite and between 0 and 100")
    return percent


def reset_string(value: object, where: str) -> str | None:
    """Claude ISO zamanını doğrula; eksik değer bilinmiyor olarak kalır."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise QuotaError(f"{where}: expected an ISO reset timestamp")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise QuotaError(f"{where}: invalid reset timestamp") from error
    if stamp.tzinfo is None:
        raise QuotaError(f"{where}: reset timestamp needs a timezone")
    return stamp.astimezone(UTC).isoformat()


def claude_quota(response: object, observed_at: str) -> QuotaSnapshot:
    """get_usage yüzde verir; streamed rate_limit_event kesriyle karıştırılmaz."""
    body = object_value(response, "Claude get_usage")
    available = body.get("rate_limits_available")
    if not isinstance(available, bool):
        raise QuotaError("Claude get_usage: missing rate_limits_available")
    if not available:
        return QuotaSnapshot("claude", observed_at, False, None, ())
    limits = object_value(body.get("rate_limits"), "Claude rate_limits")
    windows: list[QuotaWindow] = []
    for name, duration in (("five_hour", 300), ("seven_day", 10_080)):
        value = limits.get(name)
        if value is None:
            continue
        window = object_value(value, f"Claude {name}")
        if window.get("utilization") is None:
            continue
        windows.append(
            QuotaWindow(
                name,
                percent_value(window["utilization"], f"Claude {name}"),
                reset_string(window.get("resets_at"), f"Claude {name}"),
                duration,
            )
        )
    scoped = limits.get("model_scoped")
    if scoped is not None:
        if not isinstance(scoped, list):
            raise QuotaError("Claude model_scoped: expected a list")
        for value in scoped:
            window = object_value(value, "Claude model_scoped")
            model_name = window.get("display_name")
            if not isinstance(model_name, str) or not model_name.strip():
                raise QuotaError("Claude model_scoped: missing display_name")
            if window.get("utilization") is None:
                continue
            windows.append(
                QuotaWindow(
                    f"seven_day_model:{model_name}",
                    percent_value(window["utilization"], f"Claude {model_name}"),
                    reset_string(window.get("resets_at"), f"Claude {model_name}"),
                    10_080,
                )
            )
    return QuotaSnapshot("claude", observed_at, True, None, tuple(windows))


def codex_quota(response: object, observed_at: str) -> QuotaSnapshot:
    """Ana codex allowance'ı seç; model kapsamlı snapshot ana pencereyi değiştirmez."""
    body = object_value(response, "Codex account/rateLimits/read")
    by_id = body.get("rateLimitsByLimitId")
    buckets = {} if by_id is None else object_value(by_id, "Codex rateLimitsByLimitId")
    raw = buckets.get("codex", body.get("rateLimits"))
    if raw is None:
        return QuotaSnapshot("codex", observed_at, False, None, ())
    snapshot = object_value(raw, "Codex rateLimits")
    if snapshot.get("limitId") not in (None, "codex"):
        return QuotaSnapshot("codex", observed_at, False, None, ())
    windows: list[QuotaWindow] = []
    for name in ("primary", "secondary"):
        raw_window = snapshot.get(name)
        if raw_window is None:
            continue
        window = object_value(raw_window, f"Codex {name}")
        duration = window.get("windowDurationMins")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
        ):
            raise QuotaError(f"Codex {name}: invalid windowDurationMins")
        reset = window.get("resetsAt")
        reset_at: str | None = None
        if reset is not None:
            if isinstance(reset, bool) or not isinstance(reset, int | float):
                raise QuotaError(f"Codex {name}: invalid resetsAt")
            try:
                reset_at = datetime.fromtimestamp(reset, UTC).isoformat()
            except (ValueError, OverflowError, OSError) as error:
                raise QuotaError(f"Codex {name}: invalid resetsAt") from error
        windows.append(
            QuotaWindow(
                name, percent_value(window.get("usedPercent"), f"Codex {name}"), reset_at, duration
            )
        )
    account = body.get("accountId")
    fingerprint = hashlib.sha256(account.encode()).hexdigest() if isinstance(account, str) else None
    return QuotaSnapshot("codex", observed_at, bool(windows), fingerprint, tuple(windows))


class CliControl:
    """Sınırlı newline JSON bağlantısı; stdout/stderr beraber boşaltılır."""

    def __init__(self, command: Sequence[str], env: Mapping[str, str], timeout: float) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise QuotaError("quota timeout must be positive and finite")
        self.deadline = time.monotonic() + timeout
        self.command = command[0]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd="/tmp",
            )
        except OSError as error:
            raise QuotaError(f"cannot start {self.command}: {error}") from error
        self.selector = selectors.DefaultSelector()
        assert self.process.stdout is not None and self.process.stderr is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.buffer = b""

    def send(self, payload: Mapping[str, object]) -> None:
        """Yalnız kontrol kayıtları gönder; user prompt veya turn oluşturma yoktur."""
        assert self.process.stdin is not None
        try:
            self.process.stdin.write((json.dumps(payload, allow_nan=False) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise QuotaError(f"{self.command}: control input closed") from error

    def receive(self) -> dict[str, object]:
        """Bildirimleri çağırana bırak; buffered kayıtlar select beklemeden okunur."""
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise QuotaError(f"{self.command}: subscription usage request timed out")
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    payload: object = json.loads(line)
                except ValueError as error:
                    raise QuotaError(f"{self.command}: invalid JSON control response") from error
                return object_value(payload, f"{self.command} control response")
            events = self.selector.select(remaining)
            for key, _ in events:
                block = os.read(key.fd, 65_536)
                if not block:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        raise QuotaError(f"{self.command}: exited before usage response")
                elif key.data == "stdout":
                    self.buffer += block
                    if len(self.buffer) > MAX_FRAME_BYTES:
                        raise QuotaError(f"{self.command}: control response exceeded 1 MiB")

    def close(self) -> None:
        """Başarı/timeout/hata fark etmeksizin alt süreci ve pipe'ları kapat."""
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


def claude_response(control: CliControl, identity: str, subtype: str) -> dict[str, object]:
    """Claude control_response içindeki matching request sonucunu bekle."""
    control.send(
        {"type": "control_request", "request_id": identity, "request": {"subtype": subtype}}
    )
    while True:
        event = control.receive()
        if event.get("type") != "control_response":
            continue
        response = object_value(event.get("response"), "Claude control_response")
        if response.get("request_id") != identity:
            continue
        if response.get("subtype") != "success":
            raise QuotaError(f"Claude {subtype}: control request failed ({response.get('error')})")
        return object_value(response.get("response"), f"Claude {subtype}")


def codex_response(
    control: CliControl, identity: int, method: str, params: Mapping[str, object]
) -> dict[str, object]:
    """Codex JSON-RPC matching request sonucunu bekle."""
    control.send({"id": identity, "method": method, "params": dict(params)})
    while True:
        event = control.receive()
        if event.get("id") != identity:
            continue
        if "error" in event:
            error = object_value(event["error"], f"Codex {method} error")
            raise QuotaError(f"Codex {method}: {error.get('message')} (code {error.get('code')})")
        return object_value(event.get("result"), f"Codex {method}")


def read_claude_quota(timeout: float) -> QuotaSnapshot:
    """T3 probe gibi hooks/MCP kapalı bir CLI kontrol oturumu aç; model çağrısı yok."""
    command = (
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        '{"disableAllHooks":true}',
        "--tools",
        "",
    )
    env = dict(os.environ, ENABLE_CLAUDEAI_MCP_SERVERS="false", CLAUDE_CODE_AUTO_CONNECT_IDE="0")
    control = CliControl(command, env, timeout)
    try:
        claude_response(control, "quota-init", "initialize")
        response = claude_response(control, "quota-read", "get_usage")
        return claude_quota(response, datetime.now(UTC).isoformat())
    finally:
        control.close()


def read_codex_quota(timeout: float) -> QuotaSnapshot:
    """Codex app-server'dan hesabın ana limitini oku; thread/turn başlatma yok."""
    control = CliControl(("codex", "app-server", "--stdio"), os.environ, timeout)
    try:
        codex_response(
            control,
            1,
            "initialize",
            {
                "clientInfo": {"name": "cimrihook", "version": "0.1.0"},
                "capabilities": {},
            },
        )
        control.send({"method": "initialized", "params": {}})
        response = codex_response(control, 2, "account/rateLimits/read", {})
        return codex_quota(response, datetime.now(UTC).isoformat())
    finally:
        control.close()


def quota_json(snapshot: QuotaSnapshot) -> str:
    """Provider gözlemini session fiyatlarına karıştırmadan JSON olarak sun."""
    return json.dumps(asdict(snapshot), indent=2, allow_nan=False)
