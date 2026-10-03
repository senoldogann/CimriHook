"""Uçtan uca: gerçek CLI süreci, gerçek dosyalar ve gerçek SQLite defteri ile hook akışları.

Yükler Claude Code 2.1.288'in PostToolUse şemasını (1 tabanlı Read offset'i, varsayılan 2000
satır) izler; Claude Code ile gerçek entegrasyon ayrıca `claude -p` duman testiyle doğrulanır.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SESSION = "11111111-2222-3333-4444-555555555555"


def run_hook(home: Path, payload: dict[str, object]) -> dict[str, object] | None:
    """Hook'u ayrı bir süreç olarak çalıştırır; stdout boşsa None (karar yok)."""
    completed = subprocess.run(
        [sys.executable, "-m", "cimrihook", "hook"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CIMRIHOOK_HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stderr
    if completed.stdout == "":
        return None
    parsed: object = json.loads(completed.stdout)
    assert isinstance(parsed, dict)
    return parsed


def sent_text(response: dict[str, object] | None) -> str:
    """updatedToolOutput içinde modele gidecek metin (Read: file.content, Bash: stdout)."""
    assert response is not None
    specific = response["hookSpecificOutput"]
    assert isinstance(specific, dict)
    output = specific["updatedToolOutput"]
    assert isinstance(output, dict)
    if "file" in output:
        file = output["file"]
        assert isinstance(file, dict)
        content = file["content"]
    else:
        content = output["stdout"]
    assert isinstance(content, str)
    return content


def envelope(tmp: Path, agent_id: str | None) -> dict[str, object]:
    """Tüm PostToolUse yüklerinin ortak alanları."""
    payload: dict[str, object] = {
        "session_id": SESSION,
        "transcript_path": str(tmp / "transcript.jsonl"),
        "cwd": str(tmp),
        "hook_event_name": "PostToolUse",
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def read_event(
    tmp: Path, path: Path, offset: int | None, limit: int | None, agent_id: str | None
) -> dict[str, object]:
    """Claude Code'un Read sonucunu taklit eder."""
    lines = path.read_text().removesuffix("\n").split("\n")
    start = 1 if offset is None else offset
    window = lines[start - 1 : start - 1 + (2000 if limit is None else limit)]
    tool_input: dict[str, object] = {"file_path": str(path)}
    if offset is not None:
        tool_input["offset"] = offset
    if limit is not None:
        tool_input["limit"] = limit
    file: dict[str, object] = {
        "filePath": str(path),
        "content": "\n".join(window),
        "numLines": len(window),
        "startLine": start,
        "totalLines": len(lines),
    }
    return envelope(tmp, agent_id) | {
        "tool_name": "Read",
        "tool_use_id": f"toolu_{os.urandom(6).hex()}",
        "tool_input": tool_input,
        "tool_response": {"type": "text", "file": file},
    }


def bash_event(tmp: Path, command: str, stdout: str) -> dict[str, object]:
    """Başarılı Bash sonucu."""
    return envelope(tmp, None) | {
        "tool_name": "Bash",
        "tool_use_id": f"toolu_{os.urandom(6).hex()}",
        "tool_input": {"command": command, "description": "run it"},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
    }


def edit_event(tmp: Path, path: Path) -> dict[str, object]:
    """Ajanın kendi Edit çağrısı (sonucu değiştirebilecek bir adım)."""
    return envelope(tmp, None) | {
        "tool_name": "Edit",
        "tool_use_id": f"toolu_{os.urandom(6).hex()}",
        "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"},
        "tool_response": {"filePath": str(path)},
    }


def reset_event(tmp: Path, event_name: str) -> dict[str, object]:
    """SessionStart / PreCompact yükü."""
    return {
        "session_id": SESSION,
        "transcript_path": str(tmp / "transcript.jsonl"),
        "cwd": str(tmp),
        "hook_event_name": event_name,
        "trigger": "auto",
    }


def python_module(functions: int) -> str:
    """Her fonksiyonu dört satır tutan sentetik Python modülü."""
    return "".join(
        f"def compute_value_{i}(value):\n"
        f"    total = value * {i} + len(str(value))\n"
        f"    return total + {i}  # sonuç\n\n"
        for i in range(functions)
    )


def test_reread_of_known_lines_is_sent_as_ref(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(python_module(40))
    home = tmp_path / "home"
    assert run_hook(home, read_event(tmp_path, source, None, None, None)) is None
    text = sent_text(run_hook(home, read_event(tmp_path, source, 10, 60, None)))
    assert text.startswith("[CimriHook] REF: lines 10-69 of")


def test_change_after_edit_is_sent_as_delta(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(python_module(40))
    home = tmp_path / "home"
    assert run_hook(home, read_event(tmp_path, source, None, None, None)) is None
    source.write_text(python_module(40).replace("value * 7 +", "value * 700 +"))
    assert run_hook(home, edit_event(tmp_path, source)) is None
    text = sent_text(run_hook(home, read_event(tmp_path, source, None, None, None)))
    assert text.startswith("[CimriHook] DELTA:")
    assert "-    total = value * 7 + len(str(value))" in text
    assert "+    total = value * 700 + len(str(value))" in text


def test_large_file_gets_outline_once_then_raw(tmp_path: Path) -> None:
    source = tmp_path / "big.py"
    source.write_text(python_module(400))
    home = tmp_path / "home"
    text = sent_text(run_hook(home, read_event(tmp_path, source, None, None, None)))
    assert text.startswith("[CimriHook] OUTLINE")
    assert "L5: def compute_value_1(value):" in text
    assert run_hook(home, read_event(tmp_path, source, None, None, None)) is None


def test_bash_output_ref_insistence_and_delta(tmp_path: Path) -> None:
    home = tmp_path / "home"
    report = "\n".join(f"tests/test_module.py::test_case_{i} PASSED" for i in range(80))
    assert run_hook(home, bash_event(tmp_path, "pytest -q", report)) is None
    assert run_hook(home, edit_event(tmp_path, tmp_path / "x.py")) is None
    text = sent_text(run_hook(home, bash_event(tmp_path, "pytest -q", report)))
    assert text.startswith("[CimriHook] REF: the output of `pytest -q`")
    # Hiçbir şey değişmeden aynı komut: ajan ısrar ediyor, ham çıktı gider.
    assert run_hook(home, bash_event(tmp_path, "pytest -q", report)) is None
    assert run_hook(home, edit_event(tmp_path, tmp_path / "x.py")) is None
    changed = report.replace("test_case_41 PASSED", "test_case_41 FAILED")
    text = sent_text(run_hook(home, bash_event(tmp_path, "pytest -q", changed)))
    assert text.startswith("[CimriHook] DELTA:")
    assert "+tests/test_module.py::test_case_41 FAILED" in text


def test_compaction_and_subagents_isolate_context(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(python_module(40))
    home = tmp_path / "home"
    assert run_hook(home, read_event(tmp_path, source, None, None, None)) is None
    # Alt ajan kendi bağlam penceresindedir; ana bağlamın okuması ona sayılmaz.
    assert run_hook(home, read_event(tmp_path, source, 1, 100, "agent-1")) is None
    # Sıkıştırmadan sonra eski içerik bağlamda değildir.
    assert run_hook(home, reset_event(tmp_path, "PreCompact")) is None
    assert run_hook(home, read_event(tmp_path, source, 1, 100, None)) is None
    text = sent_text(run_hook(home, read_event(tmp_path, source, 1, 60, None)))
    assert text.startswith("[CimriHook] REF:")
    # Transcript'e yazılan sıkıştırma sınırı da yeni bir kuşak başlatır.
    with (tmp_path / "transcript.jsonl").open("a") as transcript:
        boundary = {"type": "system", "subtype": "compact_boundary"}
        transcript.write(json.dumps(boundary, separators=(",", ":")) + "\n")
    assert run_hook(home, read_event(tmp_path, source, 1, 60, None)) is None
