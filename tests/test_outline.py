"""İskelet çıkarımı saf bir veri dönüşümüdür; diller için küçük tablo testi."""

import pytest

from cimrihook.outline import extract_outline


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        (
            "a.py",
            "import os\n\nclass A:\n    def run(self):\n        pass\nasync def main():",
            [3, 4, 6],
        ),
        (
            "a.ts",
            "export interface X {}\nexport const f = async (a: number) => a\nclass B {\n"
            "  method(a: string): void {\n    if (a) {\n    }\n  }\n}",
            [1, 2, 3, 4],
        ),
        ("a.go", "package a\nfunc Run() {}\ntype S struct {}", [2, 3]),
        ("a.rs", "pub fn run() {}\nimpl<T> S<T> {}\nstruct X;", [1, 2, 3]),
        ("a.md", "# T\n```\n# not a heading\n```\n## U", [1, 5]),
    ],
)
def test_outline_lines(path: str, source: str, expected: list[int]) -> None:
    entries = extract_outline(path, source.split("\n"), 1)
    assert entries is not None
    assert [entry.line for entry in entries] == expected


def test_unknown_extension_has_no_outline() -> None:
    assert extract_outline("data.bin", ["x"], 1) is None
