"""Which working directories belong to CimriHook's own runs and stay out of the statistics."""

from cimrihook.transcripts import is_bench_location


def test_the_harness_and_the_pilot_workspaces_are_left_out() -> None:
    assert is_bench_location("/tmp/cimrihook-bench/deeper/boltons-claude-governor-r1/workspace")
    assert is_bench_location("/Users/u/.cimrihook/experiments/closure-ab-20261004/workspace")
    # Claude Code's project directory names for the same workspaces.
    assert is_bench_location("-private-tmp-cimrihook-bench-deeper-boltons-r1-workspace")
    assert is_bench_location("-Users-u--cimrihook-experiments-prefix-ab-20261004-default-20")


def test_the_users_own_projects_are_kept() -> None:
    assert not is_bench_location("/Users/u/Desktop/CimriHook")
    assert not is_bench_location("-Users-u-Desktop-CimriHook-bench")
    assert not is_bench_location("/Users/u/.cimrihook/backups")
