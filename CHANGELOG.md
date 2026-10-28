# Changelog

## Unreleased

- Add Pi as an `auto_titles` source. Pi prints `<app> - <folder>` until `/name`
  names the session, and `<app> - <name> - <folder>` after that, so the name is
  read from the terminal title Herdr already reports and no transcript is
  opened. The session file path Pi's integration reports (`kind: "path"`, an
  absolute `.jsonl`) is the identity that keeps an owned name tied to one
  session.

## 0.12.0

- Add `actions` (default `false`) to list plugin actions as rows (#23): Enter
  runs the action, and `>` on an empty query filters to them. Actions of
  disabled plugins and other platforms, the bar's own open action and
  selection-only actions stay out. The action gets the tab and pane you came
  from as its context, and the bar's popup closes right after the invoke, so
  an action that opens a popup of its own is not refused.

## 0.11.0

- Add `follow_new_sessions` (default `false`) to let the next Claude or Codex
  session in a pane take over the task title written for the one before it,
  after `/clear`, a restart or a new terminal (#21). A tab you renamed stays
  yours.

## 0.10.0

- Add `scope` (default `"all"`) to open the bar on a filter: `"agent"`,
  `"pane"`, `"tab"` or `"blocked"` (#17). `backspace` on an empty query still
  clears the filter, and `ctrl+u` now returns to the configured one instead of
  everything. Any other value falls back to `"all"`.
- Add `select_current` (default `false`) to open with the row you are in
  selected instead of the one you came from (#19). The order does not change.
- `herdr-bar.open` works on Windows too, so one keybinding works on every
  platform. The action runs the binary's `--open` instead of an `sh`
  one-liner. `herdr-bar.open-windows` still works on Windows, for configs
  that bind it.

## 0.9.1

- Fix tab titles and tab status on Windows, broken in 0.9.0: the lock files
  were opened without the access a Windows lock needs, so the title watcher
  never started and "Start tab watcher" never returned.
- Remove the Python implementation, its tests and its tools. Rust tests now
  hold the binary to what the last Python release did: the Python unit tests
  are ported, about 19,000 recorded answers of the Python functions are
  replayed, and an end-to-end test drives the binary on a pty against a fake
  Herdr and compares every screen, repaint size, Herdr request and state file
  with transcripts recorded from the Python release.
- Remove `run.py`. A title watcher that a Python release (0.8 or older)
  started hands over to a new release through `run.py`, so after an upgrade
  straight from 0.8 or older its tab titles stop updating. Run "Stop tab
  watcher" and then "Start tab watcher" once. Restarting Herdr is not enough,
  because the old watcher keeps running across a server restart.

## 0.9.0

- Run the bar and the title watcher as a Rust binary at `bin/herdr-bar`
  (`bin/herdr-bar.exe` on Windows) instead of Python. It is a drop-in
  replacement: a parity harness drives both on a pty against a fake Herdr
  and a differential fuzzer checks the pure functions against Python.
- Install the binary through a manifest build step. On install (updating is
  reinstalling), Herdr runs `scripts/install.sh` (`scripts/install.ps1` on
  Windows), which downloads the release binary for this version and platform,
  checks it against the release's `SHA256SUMS` and its reported version, and
  falls back to `cargo build --release` (Rust 1.88+). Set
  `HERDR_BAR_BUILD=source` to skip the download.
- Open faster and type faster on big sessions. At 2,000 rows, the first
  frame drops from about 140 ms to about 24 ms and a keystroke p99 from
  about 295 ms to about 10 ms.

## 0.8.0

- Run on Windows (#7, #12). The bar draws on the Windows console, talks to
  Herdr over the named pipe behind `HERDR_SOCKET_PATH` with the same timeouts
  as the Unix socket, and reads running times from the kernel. Task titles and
  tab status run in the same detached watcher; on Windows it polls every two
  seconds instead of following an event stream.
- Skip a Windows console's default title, the path of the program that opened
  it, when naming a tab. Codex panes keep that title until Codex sets one.
- Add an `open-windows` action that runs `python3 run.py --open`. Windows has
  no `sh`, and Herdr rejects a second `open` action even with a different
  `platforms` list. The open path imports nothing beyond the CLI call. On
  Linux and macOS, `open` keeps its `sh -c` one-liner and its startup cost.
- Add `running_time` (default `true`) to hide the per-row running time (#11).
  `false` also skips the `pane.process_info` and `ps` reads behind it, so the
  bar opens faster in sessions with many panes.

## 0.7.3

- Add `lowercase_titles` (default `false`) to lowercase automatic Claude and
  Codex task titles (#10). Changes apply while watching without a restart;
  custom names retain their original case.

## 0.7.2

- Enable automatic task titles by default when no competing naming plugin is
  detected. Preserve explicit `auto_titles: false`; status prefixes remain opt-in.
- Use terminal task titles even when Herdr has no agent session ID, with
  ownership tied to the pane and terminal. Claude transcript fallback still
  requires a session ID.

## 0.7.1

- Skip all automatic tab writes, including cleanup, while a recognized naming
  plugin is enabled. Support additional IDs through `tab_renaming_plugins`.
- Fix competing tab-renaming loops (#9): yield externally changed labels across
  watcher restarts, and restore only status labels still owned by this plugin.
- Make task titles (`auto_titles`) and status prefixes (`tab_status`) separate,
  opt-in settings, both disabled by default. Reload settings while watching and
  exit after both are disabled; the command bar works without either feature.

## 0.7.0

- Show live agent status in the tab bar: agent tabs lead with the agent's logo
  and a static status glyph. A tab is renamed only when its agent's status or
  its name changes, driven by `events.subscribe`, so idle sessions cost no extra
  polling. Configure with `tab_status`; stopping the watcher restores bare names.
- Remove the working spinner and the `spinner` setting. Working shows a steady
  `◐` in the bar and in tabs; animating tab labels cost a rename per frame,
  around 6 per second per working tab.
- The tab watcher notices a newly installed icon font within a minute.
- Document installing the Herdr Agent Icons Max font without herdr-radar.
- Preserve user-supplied leading glyphs after a failed tab rename or watcher restart.
- Keep event reads bounded, preserve events delivered with the subscription
  acknowledgement, and reconnect safely after invalid subscription responses.
- Existing `spinner` and `tab_flash` configuration keys are ignored; status
  glyphs remain static.

## 0.6.0

- Add `claude_config_dirs` so auto titles find Claude transcripts written under
  other `CLAUDE_CONFIG_DIR` directories, and document installing Herdr's Claude
  hook in each one.

## 0.5.1

- Auto titles now treat the folder-derived agent name that launchers such as
  herdr-newtab-plus pass to `herdr agent start` as a default, so those tabs
  get task titles too.

## 0.5.0

- Follow Claude and Codex task titles every two seconds, including while the
  popup is closed. Start one watcher through startup and agent-detection hooks.
- Persist ownership of automatic names so later task titles update them while
  manual tab, pane, and agent names remain untouched.
- Keep all naming I/O out of the popup; handle unavailable state storage,
  avoid duplicate idle snapshots, and isolate ownership and locks by server.
- Reconnect after server restarts, check registration through the API, reload
  upgraded watcher code without releasing its lock, and add start/stop actions.
- Give status glyphs more space after vendor logos and color working titles orange.
- Require Herdr 0.9.0 for startup hooks and agent session metadata.

## 0.4.0

- Automatically name default Claude tabs from local session transcripts when
  the bar opens or refreshes. Preserve existing tab, pane and agent names,
  including names set by another plugin. Set `HERDR_AUTO_TITLE_TRANSCRIPT=false`
  to opt out; clear a tab's name to let the bar fill it again.
- Refresh the demo with task titles shown once, leading agent logos, and
  documentation for automatic tab naming and icon setup.
- Lead agent rows with Claude, Codex, Pi, Grok, Kimi, Gemini, Cursor and OpenCode
  logos when the Herdr Agent Icons Max font is installed, with colored text
  fallbacks and optional font overrides.

## 0.3.0

- `%` filters to named panes: one row per pane you gave a name with
  `herdr pane rename`, led by that name, and Enter focuses that exact pane.
  Named panes also show up in Everything and its searches; unnamed panes stay
  out of the way. (#5, thanks @silva96)
- The top bar always names the active filter — `everything`, `@ agents`,
  `% panes`, `$ shells`, or `! needs you` — as a highlighted label, instead of
  showing a chip only when a filter is on. A scoped empty state names the
  filter that came up empty rather than saying "this filter". (#4, thanks
  @silva96)
- `ctrl+j` moves the selection down, as documented, instead of confirming the
  row: the decoder read its `0x0A` byte as Enter, so the movement binding
  never fired. Enter itself is unaffected — terminals send `0x0D` for it.
  (#6, thanks @skarakayali-lh)
- The deterministic README demo now shows the `%` panes filter and the
  always-visible filter label, and the README points at
  [herdr-newtab-plus](https://github.com/jeffarese/herdr-newtab-plus) for the
  tab-opening half of the loop.

## 0.2.1

- `"preview": false` now means the preview starts hidden rather than staying
  hidden: `ctrl+o` brings it up for the rest of the run. `ctrl+o` also opens the
  preview in a popup that `"auto"` considers too narrow, so one press always
  flips what is on screen.

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
