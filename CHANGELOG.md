# Changelog

## 0.1.0

Initial release. Requires herdr 0.7.4+ and Python 3.9+.

- Fuzzy bar over agents, agent-less tabs, and workspaces, matched on title,
  working directory, agent name and kind, branch, workspace, and tab number.
- Live status glyphs and colors mirroring herdr's sidebar, refreshed while open.
- Resting order: recents first, then blocked, done, working, idle; the current
  row is never first, so open-then-Enter behaves like alt-tab.
- Preview column tailing the selected pane.
- `@` agents, `$` shells, and `!` needs-you filters, plus tab to cycle them.
- Mouse click and wheel support, bracketed paste, CJK-safe layout.
- Terminal-native colors with optional overrides in `config.json`.
