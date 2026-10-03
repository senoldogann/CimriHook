"""Alt ajan transcript'lerinde yalnızca modele giden metin vardır; Read metninin ayrıştırılması."""

from cimrihook.claude import observe_read_text


def test_numbered_range_keeps_content_and_drops_trailing_reminder() -> None:
    text = "10\tdef a():\n11\t    return 1\n\n<system-reminder>note</system-reminder>"
    obs = observe_read_text({"file_path": "/repo/a.py", "offset": 10, "limit": 2}, text, "/repo")
    assert obs is not None
    assert (obs.start_line, obs.lines, obs.whole) == (10, ("def a():", "    return 1"), False)


def test_full_read_below_default_limit_is_the_whole_file() -> None:
    obs = observe_read_text({"file_path": "/repo/b.py"}, "1\tx = 1\n2\ty = 2", "/repo")
    assert obs is not None
    assert (obs.whole, obs.total_lines) == (True, 2)


def test_text_without_consecutive_line_numbers_is_not_a_read_result() -> None:
    assert observe_read_text({"file_path": "/repo/c.py"}, "1\ta\n3\tb", "/repo") is None
    assert observe_read_text({"file_path": "/repo/c.py"}, "File does not exist.", "/repo") is None
