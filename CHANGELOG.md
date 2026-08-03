# Changelog

## Unreleased

## 0.2.0

- `ctrl+r` opens an inline editor for the selected row's tab name. `enter`
  saves through `tab.rename`, `esc` keeps the old name, and failures leave the
  editor open for correction.
- Claude, Codex, Kimi, Gemini, Cursor, and OpenCode labels use distinct,
  configurable colors. Folder or workspace metadata is omitted when the same
  name is already visible in the title or summary.
- The deterministic README demo now shows both the agent colors and the
  `ctrl+r` rename interaction.
- `delete`, and `backspace` once it has nothing left to erase, close the
  selected row's tab after a confirmation in the footer; `enter` or `y` goes
  through with it, `esc` or any other key calls it off. A tab running more than
  one agent says how many go with it. Forward delete in the query moved to
  `ctrl+d`.
- Agent rows lead with the tab's own name and trail it with the agent's current
  summary, dropping the summary when a narrow row has no space for it or when it
  only repeats the name.
- Matches are highlighted wherever the row draws the text they landed in — the
  tab name, the agent's summary, the directory, the agent name, the workspace —
  instead of only in the tab name, which left a row matched entirely on its
  summary with nothing lit up at all.
- Matched characters are underlined as well as colored and bold, so the fuzzy
  subsequence is legible when it lands one letter at a time.
- Secondary text (summaries, agent names, running times) is a readable gray
  chosen from the terminal's background rather than the terminal's dim color,
  which left it near-invisible on a selected row. Separators and rules keep the
  dim color, so the row still reads in tiers. Override with `colors.muted`.
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
