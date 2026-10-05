"""Pinned local fixture eligibility and host acceptance, without provider calls."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from cimrihook.bench import (
    GIT_IDENTITY,
    WORKSPACE_EXCLUDES,
    apply_mutation,
    ensure_repo,
    prepare_workspace,
    run_checked,
    test_files,
    tool_env,
    walk_files,
)
from cimrihook.bench_preparation import Quality, StudyStep, StudyTask
from cimrihook.errors import BenchError
from cimrihook.preparation import SourceTarget, digest, parse_target, source_excerpt

PUBLIC_REGRESSION: Final = "tests/test_preparation_regression.py"
TEST_CONFIGS: Final = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "setup.py")
PUBLIC_CHECKS: Final = {
    "none": "",
    "compression": """import zlib
from itsdangerous.url_safe import URLSafeSerializer

def test_compression_boundary():
    serializer = URLSafeSerializer("public-key")
    for size in range(1, 100):
        value = "p" * size
        raw = serializer.serializer.dumps(value).encode("utf-8")
        payload = serializer.dump_payload(value)
        assert payload.startswith(b".") == (len(zlib.compress(raw)) < len(raw) - 1)
        assert serializer.load_payload(payload) == value
""",
    "rotation": """from itsdangerous.signer import Signer

def test_rotation_public():
    old = Signer("old", sep="~").sign(b"public")
    assert Signer(["old", "new"], sep="~").unsign(old) == b"public"
""",
    "age": """from unittest.mock import patch
from itsdangerous.timed import TimestampSigner

def test_age_equality_public():
    signer = TimestampSigner("public-key")
    with patch("itsdangerous.timed.time.time", return_value=1000):
        token = signer.sign(b"public")
    with patch("itsdangerous.timed.time.time", return_value=1010):
        assert signer.unsign(token, max_age=10) == b"public"
""",
    "integer": """from itsdangerous.encoding import int_to_bytes

def test_integer_public():
    assert int_to_bytes(256) == b"\\x01\\x00"
    assert int_to_bytes(0) == b""
""",
    "key": """from itsdangerous.signer import Signer

def test_newest_key_public():
    signer = Signer(["old", "new"], sep="~")
    assert signer.sign(b"public") == Signer("new", sep="~").sign(b"public")
""",
}

HIDDEN_COUNT: Final = """import itertools
import pytest
from more_itertools import exactly_n

@pytest.mark.parametrize("size", range(8))
@pytest.mark.parametrize("n", range(-1, 9))
def test_counts(size, n):
    for values in itertools.product((0, 1), repeat=size):
        assert exactly_n(iter(values), n) == (sum(values) == n)

def test_consumption_boundary():
    seen = []
    def values():
        for value in (0, 1, 0, 1, 0, 1, 99):
            seen.append(value)
            yield value
    assert not exactly_n(values(), 2)
    assert seen == [0, 1, 0, 1, 0, 1]
"""

HIDDEN_RANGES: Final = """import importlib.util
import itertools
from boltons.iterutils import chunk_ranges

def test_reference_grid():
    spec = importlib.util.spec_from_file_location("gold_iterutils", GOLD_PATH)
    gold = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gold)
    grid = itertools.product(range(10), range(1, 6), range(4), range(3), (False, True))
    for size, chunk, offset, overlap, align in grid:
        if overlap >= chunk:
            continue
        args = (size, chunk, offset, overlap, align)
        assert list(chunk_ranges(*args)) == list(gold.chunk_ranges(*args))
"""

HIDDEN_SALT_ROTATION: Final = """import pytest
from itsdangerous import BadSignature, Serializer
from itsdangerous.signer import Signer

@pytest.mark.parametrize("salt", (b"hidden-a", "hidden-b", b"", "default"))
@pytest.mark.parametrize("payload", ({"hidden": [1, 2, 3]}, "unicode-ö", 0, None))
def test_salts(salt, payload):
    serializer = Serializer(["legacy-hidden", "current-hidden"], salt="default")
    token = serializer.dumps(payload, salt=salt)
    assert serializer.loads(token, salt=salt) == payload
    assert serializer.make_signer(salt).salt == (salt.encode() if isinstance(salt, str) else salt)
    with pytest.raises(BadSignature):
        serializer.loads(token, salt="different-hidden-domain")

@pytest.mark.parametrize("derivation", ("concat", "django-concat", "hmac", "none"))
@pytest.mark.parametrize("sep", (".", "~", "*"))
def test_rotation(derivation, sep):
    keys = ["legacy-1", "legacy-2", "current-hidden"]
    rotated = Signer(keys, sep=sep, key_derivation=derivation)
    for key in keys:
        token = Signer(key, sep=sep, key_derivation=derivation).sign(b"hidden-payload")
        assert rotated.unsign(token) == b"hidden-payload"
    latest = Signer(keys[-1], sep=sep, key_derivation=derivation).sign(b"hidden-payload")
    assert rotated.sign(b"hidden-payload") == latest
    with pytest.raises(BadSignature):
        rotated.unsign(rotated.sign(b"hidden-payload")[:-1] + b"!")
"""

HIDDEN_MIXED: Final = """import pytest
from itsdangerous import BadData
from itsdangerous.encoding import base64_encode, base64_decode
from itsdangerous.url_safe import URLSafeSerializer

@pytest.mark.parametrize("size", range(1, 97))
def test_binary_padding(size):
    value = bytes((i * 37) % 256 for i in range(size))
    assert base64_decode(base64_encode(value)) == value

@pytest.mark.parametrize("value", ({"x": "h" * 1200}, {"a": 1, "b": 2}, "ö" * 93, ""))
def test_url_roundtrip(value):
    serializer = URLSafeSerializer("hidden-key")
    assert serializer.loads(serializer.dumps(value)) == value

def test_invalid_base64():
    with pytest.raises(BadData):
        base64_decode(b"a")
"""

HIDDEN_COMPRESSION: Final = """import zlib
import pytest
from itsdangerous.encoding import base64_encode
from itsdangerous.url_safe import URLSafeSerializer

@pytest.mark.parametrize("size", range(1, 160))
def test_wire_boundary(size):
    serializer = URLSafeSerializer("hidden-key")
    value = "z" * size
    raw = serializer.serializer.dumps(value).encode("utf-8")
    compressed = zlib.compress(raw)
    expected = (b"." + base64_encode(compressed)
                if len(compressed) < len(raw) - 1 else base64_encode(raw))
    assert serializer.dump_payload(value) == expected
    assert serializer.load_payload(expected) == value
"""

HIDDEN_MEMORY: Final = (
    HIDDEN_SALT_ROTATION
    + """
from unittest.mock import patch
from itsdangerous.encoding import int_to_bytes, bytes_to_int
from itsdangerous.timed import TimestampSigner

@pytest.mark.parametrize("number", (0, 1, 255, 256, 65536, 2**32, 2**63 - 1))
def test_integer_memory(number):
    expected = number.to_bytes(8, "big").lstrip(b"\\x00")
    assert int_to_bytes(number) == expected
    assert bytes_to_int(expected) == number

def test_age_and_legacy_wire():
    with patch("itsdangerous.timed.time.time", return_value=2000):
        legacy = TimestampSigner("legacy-1", sep="~").sign(b"hidden-payload")
    rotated = TimestampSigner(["legacy-1", "current-hidden"], sep="~")
    with patch("itsdangerous.timed.time.time", return_value=2010):
        assert rotated.unsign(legacy, max_age=10) == b"hidden-payload"
"""
)

HIDDEN_CHECKS: Final = {
    "count": HIDDEN_COUNT,
    "ranges": HIDDEN_RANGES,
    "salt-rotation": HIDDEN_SALT_ROTATION,
    "mixed": HIDDEN_MIXED,
    "compression": HIDDEN_COMPRESSION,
    "memory": HIDDEN_MEMORY,
}


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """Complete host test output, with an explicit timeout status."""

    passed: bool
    output: str
    exit_code: int
    timed_out: bool


def full_suite(workspace: Path, command: tuple[str, ...], timeout: int) -> TestOutcome:
    """Capture complete public evidence, not a tail or an agent success assertion."""
    clear_bytecode(workspace)
    env = tool_env(os.environ) | {"PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        text = (stdout if isinstance(stdout, bytes) else stdout.encode()).decode(errors="replace")
        text += (stderr if isinstance(stderr, bytes) else stderr.encode()).decode(errors="replace")
        return TestOutcome(False, text + f"\nHost test timeout after {timeout}s", -1, True)
    return TestOutcome(
        result.returncode == 0, result.stdout + result.stderr, result.returncode, False
    )


def clear_bytecode(workspace: Path) -> None:
    """Same-size fast edits must not reuse timestamp-valid bytecode from a previous state."""
    for path in workspace.rglob("__pycache__"):
        if ".venv" not in path.relative_to(workspace).parts:
            shutil.rmtree(path)


def protected_test_files(workspace: Path) -> dict[str, bytes]:
    """Test-selection configuration is protected along with test source bytes."""
    return test_files(workspace) | {
        name: (workspace / name).read_bytes()
        for name in TEST_CONFIGS
        if (workspace / name).exists()
    }


def pinned_repo(task: StudyTask, work_dir: Path) -> Path:
    """Fetch a version with the existing helper, then require the registered commit."""
    root = ensure_repo(task.task.repo, task.task.ref, work_dir)
    resolved = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=root, text=True).strip()
    if resolved != task.commit:
        raise BenchError(f"{task.task.id}: expected commit {task.commit}, got {resolved}")
    return root


def fixture_workspace(task: StudyTask, repo: Path, workspace: Path) -> None:
    """Use the existing isolated setup and install only this source copy for src layout."""
    prepare_workspace(task.task, repo, workspace)
    if (workspace / "src" / "itsdangerous").is_dir():
        run_checked(
            (
                "uv",
                "pip",
                "install",
                "--python",
                str(workspace / ".venv/bin/python"),
                "--no-build-isolation",
                "--no-deps",
                "-q",
                "--editable",
                ".",
            ),
            workspace,
        )


def freeze_fixture(workspace: Path) -> None:
    """Seal only named fixture files; no oracle reference history or broad git add."""
    git_dir = workspace / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)
    run_checked(("git", "init", "-q"), workspace)
    exclude = workspace / ".git/info/exclude"
    exclude.write_text(WORKSPACE_EXCLUDES + "*.egg-info/\n", encoding="utf-8")
    files = tuple(sorted(walk_files(workspace)))
    for offset in range(0, len(files), 100):
        run_checked(("git", "add", "--", *files[offset : offset + 100]), workspace)
    run_checked(("git", *GIT_IDENTITY, "commit", "-q", "-m", "frozen task fixture"), workspace)


def inject_public_check(step: StudyStep, workspace: Path) -> None:
    """Host regression changes establish a new baseline before any agent invocation."""
    if step.public_check not in PUBLIC_CHECKS:
        raise BenchError(f"unknown public regression {step.public_check}")
    code = PUBLIC_CHECKS[step.public_check]
    path = workspace / PUBLIC_REGRESSION
    if code:
        path.write_text(code, encoding="utf-8")
    elif path.exists():
        path.unlink()


def mutate_threshold(workspace: Path) -> None:
    """Change the helper comparison by AST coordinates, independent of source formatting."""
    path = workspace / "src/itsdangerous/url_safe.py"
    text = path.read_text(encoding="utf-8")
    helpers = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.FunctionDef) and node.name == "_compress_payload"
    ]
    if len(helpers) != 1:
        raise BenchError(f"{path}: required compression helper is absent or ambiguous")
    expected = ast.dump(ast.parse("len(compressed) < (len(json) - 1)", mode="eval").body)
    matches = [
        node
        for node in ast.walk(helpers[0])
        if isinstance(node, ast.Compare) and ast.dump(node) == expected
    ]
    if len(matches) != 1:
        raise BenchError(f"{path}: required helper comparison found {len(matches)} times")
    node = matches[0].comparators[0]
    if node.end_lineno is None or node.end_col_offset is None:
        raise BenchError(f"{path}: missing comparison AST end coordinates")
    lines = text.encode("utf-8").splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    raw = text.encode("utf-8")
    path.write_bytes(raw[:start] + b"len(json) + 1" + raw[end:])


def inject_step(task: StudyTask, index: int, workspace: Path) -> None:
    """Dependent steps use candidate state; a missing anchor is not secretly repaired."""
    step = task.steps[index]
    inject_public_check(step, workspace)
    if task.task.id == "id-stale" and index == 1:
        mutate_threshold(workspace)
    else:
        for mutation in step.mutations:
            apply_mutation(workspace, mutation)


def gold_refactor(workspace: Path) -> None:
    """A fixed eligible helper extraction used only in local host validation."""
    path = workspace / "src/itsdangerous/url_safe.py"
    text = path.read_text(encoding="utf-8")
    helper = """def _compress_payload(json: bytes) -> tuple[bytes, bool]:
    compressed = zlib.compress(json)
    if len(compressed) < (len(json) - 1):
        return compressed, True
    return json, False


"""
    text = text.replace("class URLSafeSerializerMixin", helper + "class URLSafeSerializerMixin", 1)
    begin = text.index("        json = super().dump_payload(obj)")
    end = text.index("        base64d = base64_encode(json)", begin)
    text = (
        text[:begin]
        + "        json, is_compressed = _compress_payload(super().dump_payload(obj))\n\n"
        + text[end:]
    )
    path.write_text(text, encoding="utf-8")


def restore_gold(task: StudyTask, index: int, workspace: Path, repo: Path) -> None:
    """Restore declared production files only; never expose a reference to an agent."""
    if task.task.id == "id-stale" and index == 1:
        path = workspace / "src/itsdangerous/url_safe.py"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("len(json) + 1", "len(json) - 1"), encoding="utf-8")
        return
    for relative in {m.path for m in task.steps[index].mutations}:
        (workspace / relative).write_bytes((repo / relative).read_bytes())
    if task.steps[index].reference == "refactor":
        gold_refactor(workspace)


def hidden_suite(
    task: StudyTask, step: StudyStep, workspace: Path, repo: Path, validation: Path
) -> TestOutcome:
    """Materialize private cases only after the agent exits, in a disposable host copy."""
    if validation.exists():
        raise BenchError(f"hidden validation directory already exists: {validation}")
    shutil.copytree(
        workspace,
        validation,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", "*.egg-info"
        ),
    )
    try:
        hidden = validation / "host_hidden"
        hidden.mkdir()
        if step.hidden_check not in HIDDEN_CHECKS:
            raise BenchError(f"unknown hidden acceptance {step.hidden_check}")
        prelude = ""
        if step.hidden_check == "ranges":
            prelude = f"GOLD_PATH = {str(repo / 'boltons/iterutils.py')!r}\n"
        (hidden / "test_hidden.py").write_text(
            prelude + HIDDEN_CHECKS[step.hidden_check], encoding="utf-8"
        )
        env = tool_env(os.environ) | {"PYTHONDONTWRITEBYTECODE": "1"}
        env["PYTHONPATH"] = (
            str(validation / "src") if task.task.id.startswith("id-") else str(validation)
        )
        try:
            result = subprocess.run(
                (str(workspace / ".venv/bin/python"), "-m", "pytest", "-q", "host_hidden"),
                cwd=validation,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
            text += stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
            return TestOutcome(False, text + "\nHost hidden-test timeout after 120s", -1, True)
        return TestOutcome(
            result.returncode == 0, result.stdout + result.stderr, result.returncode, False
        )
    finally:
        shutil.rmtree(validation)


def source_inventory(task: StudyTask, workspace: Path) -> tuple[str, ...]:
    """Only relative production Python paths; no mutation/reference lookup."""
    return tuple(
        sorted(
            str(path.relative_to(workspace))
            for root in task.roots
            for path in (workspace / root).rglob("*.py")
        )
    )


def source_hashes(task: StudyTask, workspace: Path) -> dict[str, str]:
    """Whole production fingerprints reveal stale packet inputs after edits."""
    return {
        p: hashlib.sha256((workspace / p).read_bytes()).hexdigest()
        for p in source_inventory(task, workspace)
    }


def reference_match(task: StudyTask, step: StudyStep, workspace: Path, repo: Path) -> bool:
    """Byte reference for bug fixes; explicit shape plus host behavior for the refactor."""
    if step.reference == "bytes":
        return source_inventory(task, workspace) == source_inventory(task, repo) and all(
            (workspace / path).read_bytes() == (repo / path).read_bytes()
            for path in source_inventory(task, repo)
        )
    if step.reference != "refactor":
        raise BenchError(f"unknown reference basis {step.reference}")
    if not (workspace / "src/itsdangerous/url_safe.py").is_file():
        return False
    try:
        tree = ast.parse((workspace / "src/itsdangerous/url_safe.py").read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_compress_payload"
    ]
    if len(helpers) != 1 or [arg.arg for arg in helpers[0].args.args] != ["json"]:
        return False
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_compress_payload"
    ]
    unchanged = source_inventory(task, workspace) == source_inventory(task, repo) and all(
        (workspace / p).read_bytes() == (repo / p).read_bytes()
        for p in source_inventory(task, repo)
        if p != "src/itsdangerous/url_safe.py"
    )
    return bool(calls) and unchanged


def host_quality(
    task: StudyTask,
    index: int,
    workspace: Path,
    repo: Path,
    baseline: dict[str, bytes],
    artifact: Path,
) -> Quality:
    """Always retain public, integrity, private and reference outcomes separately."""
    public = full_suite(workspace, task.task.test_command, 120)
    hidden = hidden_suite(task, task.steps[index], workspace, repo, artifact / "validation")
    (artifact / "public.after.txt").write_text(public.output, encoding="utf-8")
    (artifact / "hidden.after.txt").write_text(hidden.output, encoding="utf-8")
    quality = Quality(
        public.passed,
        protected_test_files(workspace) == baseline,
        hidden.passed,
        reference_match(task, task.steps[index], workspace, repo),
    )
    (artifact / "quality.json").write_text(
        json.dumps(asdict(quality), indent=2) + "\n", encoding="utf-8"
    )
    return quality


def target_source_bytes(workspace: Path, targets: tuple[str, ...]) -> int:
    """Fixture size matching uses literal excerpts before provider assignment."""
    return sum(
        len(source_excerpt(workspace, parse_target(target)).text.encode("utf-8"))
        for target in targets
    )


def wrong_targets(step: StudyStep, workspace: Path) -> tuple[SourceTarget, ...]:
    """Frozen offsets follow their wrong symbol after line shifts; never select a new symbol."""
    result: list[SourceTarget] = []
    for raw, (first, last) in zip(step.wrong, step.wrong_offsets, strict=True):
        source = source_excerpt(workspace, parse_target(raw))
        start = source.start + first
        end = source.start + last
        if not 1 <= start <= end <= source.total_lines:
            raise BenchError(f"wrong excerpt {raw}: invalid registered offsets {first}:{last}")
        result.append(SourceTarget(source.path, start, end, None))
    return tuple(result)


def wrong_source_bytes(step: StudyStep, workspace: Path) -> int:
    """Count the resolved wrong excerpt, rather than its unbounded anchor symbol."""
    oracle = tuple(source_excerpt(workspace, parse_target(raw)) for raw in step.oracle)
    wrong = tuple(source_excerpt(workspace, target) for target in wrong_targets(step, workspace))
    for target in wrong:
        if any(
            target.path == gold.path and target.start <= gold.end and gold.start <= target.end
            for gold in oracle
        ):
            raise BenchError(f"wrong target overlaps an oracle excerpt: {target.path}")
    return sum(len(target.text.encode("utf-8")) for target in wrong)


def eligibility(task: StudyTask, work_dir: Path) -> dict[str, object]:
    """Run original, mutated and gold public/hidden suites locally; never invoke agents."""
    repo = pinned_repo(task, work_dir)
    workspace = work_dir / "eligibility" / task.task.id / "workspace"
    if workspace.exists():
        raise BenchError(f"eligibility attempt already exists: {workspace}")
    fixture_workspace(task, repo, workspace)
    baseline = full_suite(workspace, task.task.test_command, 120)
    if not baseline.passed:
        raise BenchError(f"{task.task.id}: original public suite failed\n{baseline.output}")
    reports: list[dict[str, object]] = []
    for index, step in enumerate(task.steps):
        artifact = workspace.parent / f"step-{index + 1}"
        artifact.mkdir()
        inject_public_check(step, workspace)
        individual_failures: list[bool] = []
        for mutation in step.mutations:
            path = workspace / mutation.path
            original = path.read_bytes()
            if task.task.id == "id-stale" and index == 1:
                mutate_threshold(workspace)
            else:
                apply_mutation(workspace, mutation)
            individual = full_suite(workspace, task.task.test_command, 120)
            path.write_bytes(original)
            if individual.passed or individual.timed_out:
                raise BenchError(
                    f"{task.task.id}: individual mutation has no finite public failure"
                )
            individual_failures.append(True)
        original_hidden = hidden_suite(task, step, workspace, repo, artifact / "original-hidden")
        if not original_hidden.passed:
            raise BenchError(
                f"{task.task.id}: original hidden suite failed\n{original_hidden.output}"
            )
        inject_step(task, index, workspace)
        failing = full_suite(workspace, task.task.test_command, 120)
        (artifact / "public.before.txt").write_text(
            failing.output.replace(str(workspace) + "/", ""), encoding="utf-8"
        )
        if failing.passed or failing.timed_out:
            raise BenchError(
                f"{task.task.id} step {index + 1}: mutation did not produce a finite public failure"
            )
        freeze_fixture(workspace)
        oracle_bytes = target_source_bytes(workspace, step.oracle)
        wrong_bytes = wrong_source_bytes(step, workspace)
        if not 0.8 * oracle_bytes <= wrong_bytes <= 1.2 * oracle_bytes:
            raise BenchError(
                f"{task.task.id}: wrong source bytes do not match the oracle allowance"
            )
        restore_gold(task, index, workspace, repo)
        gold = host_quality(task, index, workspace, repo, protected_test_files(workspace), artifact)
        if not gold.accepted:
            raise BenchError(
                f"{task.task.id} step {index + 1}: gold acceptance failed: {asdict(gold)}"
            )
        reports.append(
            {
                "step": index + 1,
                "oracle_source_bytes": oracle_bytes,
                "wrong_source_bytes": wrong_bytes,
                "evidence_sha256": digest(failing.output.replace(str(workspace) + "/", "")),
                "gold": asdict(gold),
                "individual_mutations_failed": individual_failures,
            }
        )
    return {
        "task_id": task.task.id,
        "commit": task.commit,
        "steps": reports,
        "eligible": True,
        "task_sha256": digest(json.dumps(asdict(task), sort_keys=True)),
    }
