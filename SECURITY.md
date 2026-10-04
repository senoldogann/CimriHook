# Security

## Reporting a vulnerability

Please report security problems privately through this repository's Security tab ("Report a
vulnerability"), not in a public issue. Include the command you ran, your settings file with
secrets removed, and the output.

## What CimriHook touches

- **Reads** Claude Code transcripts (`~/.claude/projects`), Codex CLI rollouts (`~/.codex/sessions`)
  and the settings file it is asked to change. Transcripts contain your prompts and tool output;
  CimriHook keeps only token counts, timestamps, session ids and usage-limit readings.
- **Writes** its own directory (`~/.cimrihook`: a SQLite ledger, the install record, the mod, the
  limit meter's readings) and, only on `cimrihook init`, the Claude Code settings file
  (`~/.claude/settings.json`) or Codex's `~/.codex/config.toml`. The settings are backed up first,
  written atomically with their permissions kept, and `init --dry-run` prints the diff with the
  values of every `env` and `headers` object hidden (except the variables CimriHook manages).
  `init --remove` takes out only what CimriHook added.
- **Runs** a `UserPromptSubmit` hook and a status line command that only read the local transcript
  and ledger, and an optional mod that runs inside Claude Code. Your previous status line command
  is chained, not replaced, and runs with the same input it always got.
- **Network:** the package makes no network calls. `cimrihook bench-run` clones the task
  repositories with `git` and runs the agents you are logged in to, in an environment that passes
  only an allow-list of variables, so your shell's secrets do not reach them.

Nothing is sent anywhere by CimriHook. Sharing numbers (for example in an issue) is up to you; the
output of `doctor` and `limits` is aggregate.
