# Changelog

## Unreleased

- `delete`, and `backspace` once it has nothing left to erase, close the
  selected row's tab after a confirmation in the footer; `enter` or `y` goes
  through with it, `esc` or any other key calls it off. A tab running more than
  one agent says how many go with it. Forward delete in the query moved to
  `ctrl+d`.
- Agent rows lead with the tab's own name and trail it with the agent's current
  summary, dropping the summary when a narrow row has no space for it or when it
  only repeats the name.
- Rows show how long their process has been running — the agent session, or
  whatever a plain tab is running. Read once per pane from `pane.process_info`
  and `ps`, then ticked locally.

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
