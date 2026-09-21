# herdr-bar — auto tab titles, agent icons & Cmd+K search

[![ci](https://github.com/jeffarese/herdr-bar/actions/workflows/ci.yml/badge.svg)](https://github.com/jeffarese/herdr-bar/actions/workflows/ci.yml)

**A [Herdr](https://herdr.dev) plugin for automatic tab titles, recognizable
agent icons, and fast session switching.** Press Cmd+K, type a few letters of a
task, tab, pane, agent, repo or branch, and Enter takes you there. Like the Slack
quick switcher, for the terminal.

![Herdr command bar with auto tab titles, Claude and Codex agent icons, and fuzzy search](assets/demo.gif)

- **auto title for unnamed tabs** — follows Claude and Codex task titles to
  replace default tab numbers with the task title. Existing tab names, named
  panes and named agents stay yours, including names set by another plugin.
- **recognizable agent icons** — Claude, Codex, Pi, Grok, Kimi, Gemini, Cursor
  and OpenCode have distinct marks and colors. With the Herdr Agent Icons Max
  font installed, vendor logos lead each agent row, as shown in the demo.

- **fuzzy search over everything you can jump to** — agents, plain tabs, and
  workspaces, matched on title, working directory, agent name and kind, branch,
  workspace, and tab number.
- **live status, herdr's own language** — `◉` needs you, spinner working,
  `●` done, `✓` idle. Colors and glyphs mirror the herdr sidebar, and the list
  keeps updating while it is open.
- **named panes on demand** — `%` switches to one row per pane, led by the
  name you assigned it, and Enter focuses that exact pane.
- **opens on what matters** — blocked and finished agents float to the top,
  recently visited rows above them, and the tab you are in is never first, so
  open-then-Enter works like alt-tab.
- **running time on every row** — how long the agent session, or whatever the
  tab is running, has been up: `47s`, `12m`, `2h04m`, `4d3h`.
- **live preview** — the right column tails the selected pane, so you can look
  before you leap.
- **closes tabs** — `⌦` on a row closes its tab once you confirm, so the
  session you are looking at is the session you can tidy.
- **no dependencies** — Python 3 standard library only. No build step, no
  runtime to install. A small title watcher runs in the background.

## Install

```bash
herdr plugin install jeffarese/herdr-bar
```

Then bind a key (herdr does not add keybindings for you). In your herdr
`config.toml`:

```toml
[[keys.command]]
key = "prefix+k"
type = "plugin_action"
command = "herdr-bar.open"
description = "command bar"
```

Reload with `herdr server reload-config`, then press `ctrl+b k`.

Requires herdr 0.9.0+ (startup hooks and agent session metadata), Python 3.9+
on PATH, and macOS or Linux. Herdr refuses to install the plugin on anything
older, so there is nothing to get wrong.

## Auto title: automatic tab naming

Unnamed Claude and Codex tabs pick up their agent's task title automatically,
even while the bar is closed. The watcher checks every two seconds and follows
later task-title changes. Codex's trailing ` | folder` is removed; a bare folder
or agent name is ignored until a task title is available. Claude's local
transcript supplies its generated title or first human prompt when Herdr has no
useful terminal title yet. No extra model requests are made.

- **Your names win.** Existing custom tab names, pane labels and agent names
  are preserved. Only default names and names recorded as written by this
  plugin are updated. Manually changing a name opts that tab out; clearing it
  opts back in. Tabs with multiple agents are left alone.
- **Updates survive reopening.** Ownership is saved in the plugin state
  directory, tied to the agent session and terminal. Old custom names are not
  adopted just because they match a suggested title.
- **Runs automatically.** Herdr's startup and agent-detection hooks start a
  single background watcher per server. Opening the bar also ensures it is running. After
  linking or enabling the plugin in an already running server, start it with
  `herdr plugin action invoke herdr-bar.start-titles`.
- **Stop or resume.** `herdr plugin action invoke herdr-bar.stop-titles` stops
  naming for this server; event hooks and popup opens leave it stopped until
  you invoke `herdr-bar.start-titles` again. Disabling or uninstalling the
  plugin stops its connected watchers within ten seconds.
- **Local sources.** Requires Herdr to report the Claude or Codex session ID.
  Claude transcript fallback uses `CLAUDE_CONFIG_DIR` (default `~/.claude`).
  Set `HERDR_AUTO_TITLE_TRANSCRIPT=false` in Herdr's environment before starting
  it to disable automatic naming. The standalone Auto Title plugin's
  `config.env` is not read.

Herdr exposes the current name rather than its author: a manual name equal to
the tab's default position number is indistinguishable from an unnamed tab.
It also has no conditional rename API, so a rename made in the brief interval
between the final check and the write cannot be detected.

## Agent icons at a glance

Logos sit before the evenly spaced status and task title. Working titles use
the orange `working` color (configurable), making a mixed Claude, Codex, Gemini,
Pi, Grok or Kimi session easy to scan. Custom agent names remain visible; the
vendor label is omitted when its logo already identifies it.

The logos use the **Herdr Agent Icons Max** font from
[herdr-radar](https://github.com/hhdebb/herdr-radar). If Radar already shows
logos, you are ready. Otherwise follow Radar's font setup and reload your
terminal configuration. The bar detects the font locally and falls back to
colored text labels when it is unavailable. It never installs fonts for you.

Set `"agent_icons": "font"` to force logos (for example over SSH when the font
is installed on your local terminal), or `"agent_icons": "none"` for text only.
The demo uses the icon font and tabs already named for their tasks.

## Making it a real Cmd+K

`prefix+k` works everywhere and is the safe default. If you want the physical
Cmd+K, add a second binding for a chord your terminal can actually deliver —
`ctrl+alt` is the one modifier family that is free in every terminal we know of:

```toml
[[keys.command]]
key = ["prefix+k", "ctrl+alt+k"]
type = "plugin_action"
command = "herdr-bar.open"
description = "command bar"
```

…and then teach your terminal to send that chord when you press Cmd+K. On macOS,
Cmd never reaches the program inside the terminal on its own; the terminal has to
translate it. These send what herdr reads as `ctrl+alt+k`:

| Terminal | Setting |
| --- | --- |
| Ghostty | `keybind = cmd+k=text:\x1b[107;7u` in `~/.config/ghostty/config` |
| kitty | `map cmd+k send_text all \x1b[107;7u` |
| WezTerm | `{ key = "k", mods = "CMD", action = wezterm.action.SendString("\x1b[107;7u") }` |
| iTerm2 | Settings → Keys → Key Bindings → `⌘K` → *Send Escape Sequence* → `[107;7u` |
| Terminal.app | No arbitrary key mapping; stay on `prefix+k` |

`\x1b[107;7u` is the CSI-u encoding of `ctrl+alt+k`. If your terminal prefers the
legacy form, `\x1b\x0b` says the same thing. Some terminals can forward Cmd
directly, in which case plain `key = "cmd+k"` in the herdr binding is worth a
try first — it depends on your terminal's keyboard protocol.

Whichever you pick, Cmd+K is likely already taken by the terminal (usually
"clear scrollback"); the mapping above replaces it.

## Using it

| Key | Does |
| --- | --- |
| type | fuzzy search; space separates terms, all of which must match |
| `↑` `↓`, `ctrl+p` `ctrl+n`, `ctrl+k` `ctrl+j` | move |
| `enter` | jump to the selected row and close |
| `esc`, `ctrl+c`, `ctrl+g` | leave, change nothing |
| `tab` / `shift+tab` | cycle the filter |
| `@` `%` `$` `!` on an empty query | filter to agents / panes / plain tabs / rows that need you |
| `backspace` on an empty query | clear the filter, then close the selected row's tab |
| `delete` (fn+`⌫` on a Mac laptop) | close the selected row's tab, whatever is typed |
| `ctrl+u` / `ctrl+w` / `ctrl+d` | clear the query / delete a word / forward delete |
| `ctrl+o` | toggle the preview |
| `ctrl+r` | rename the selected row's tab; Enter saves, Esc keeps the old name |
| `pgup` / `pgdn` | page |
| wheel / click | move / select, click again to jump |

**What is in the list.** Every agent, every tab that has no agent, and — once a
session has more than one — every workspace. Selecting an agent focuses its tab
and its pane; selecting a workspace focuses the workspace.

`% panes` switches to one row per named pane. Unnamed panes stay out of the
list. Named panes also appear in Everything and participate in its searches;
the pane filter narrows the list to just those direct pane targets. Selecting a
pane focuses its tab and that exact pane.

**What a row says.** The tab's own name comes first — that is what you named
the work and what you remember it by — and an agent's current summary follows it
in dimmer text. A narrow row keeps the name and drops the summary; a tab with no
name of its own lets the summary stand in for it. Folder and workspace metadata
is omitted when the same name is already present in the title or summary, and
agent labels use distinct colors so mixed-agent sessions scan quickly.

**Closing a tab.** `backspace` — the key macOS labels *delete* — erases the
query first, then clears the filter, and once there is nothing left to unwind
it arms the close instead. `delete` (fn+`⌫`) arms it whatever is typed. The
footer says what is going; `enter` or `y` does it, `esc` or any other key calls
it off, including the delete keys themselves, so a held key that repeats can
never answer its own question. Rows are per agent but tabs are what close, so a
tab running two agents says so before it takes both. Workspaces cannot be
closed from the bar.

**Running time.** The number next to a row is how long its process has been
running: the agent session for an agent, the running command — or the shell
itself, which reads as the age of the tab — for a plain tab. Herdr keeps no
clocks, so this comes from the operating system, once per pane; a status that
changed a minute ago on a two-hour-old session still says two hours.

**How it is ordered.** With no query: recently visited rows first, then rows that
want your attention (blocked, then done, then working), then the rest by
workspace and tab number. The tab you are currently in is never first. With a
query: best fuzzy score wins, ties broken by recency and then status. Matches in
the title outrank matches in a working directory or an agent name. Matched
characters are drawn bold, colored and underlined wherever the row shows the
text they landed in — the tab name, the summary, the directory, the agent — so a
subsequence scattered across a sentence still reads as one, and a row that
matched on something off screen simply shows no highlight.

## Configuring

Optional. Write `config.json` in the plugin config directory
(`herdr plugin config-dir herdr-bar`):

```json
{
  "preview": "auto",
  "mouse": true,
  "spinner": true,
  "refresh_ms": 900,
  "workspaces": "auto",
  "selection_background": "auto",
  "colors": {
    "accent": "#89b4fa",
    "match": "#89b4fa",
    "muted": "bright_black"
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `preview` | `"auto"` | `true`, `false` (hidden until `ctrl+o`), or `"auto"` (on when the popup is wide enough) |
| `mouse` | `true` | click and wheel support |
| `spinner` | `true` | animate the working glyph |
| `agent_icons` | `"auto"` | Detect the local Herdr Agent Icons Max font; `"font"` forces logos (useful over SSH); `"none"` keeps text labels only |
| `refresh_ms` | `900` | how often the open bar re-reads the session |
| `workspaces` | `"auto"` | `true`, `false`, or `"auto"` (on with more than one workspace) |
| `selection_background` | `"auto"` | `"auto"` asks the terminal for its background color, or set `"none"`, a hex value, or a 0-255 ANSI index |
| `colors` | `{}` | role → `#rrggbb`, an ANSI name (`bright_blue`), or 0-255. Roles: `accent`, `match`, `text`, `muted`, `blocked`, `working`, `done`, `idle`, `unknown`, plus `agent_claude`, `agent_codex`, `agent_pi`, `agent_grok`, `agent_kimi`, `agent_gemini`, `agent_cursor`, `agent_opencode` |

Colors default to plain ANSI, so the bar follows whatever theme your terminal
already uses. The exception is `muted` — the second tier of text, used for
summaries and running times — which is a gray picked from the
terminal's own background (lighter on a dark theme, darker on a light one) so it
stays readable; agent labels use their agent role, and `unknown` is the dimmer
tier below it for separators and rules.

See [Agent icons at a glance](#agent-icons-at-a-glance) for font setup.
Detection cannot verify your terminal's active font mapping; if you see boxes,
check the mapping or set `"agent_icons": "none"`. Unknown agents keep text labels.

Popup size lives in herdr, not here. Override the manifest's `74%` × `62%` per
invocation with `herdr plugin pane open --plugin herdr-bar --entrypoint bar
--placement popup --width 60% --height 50%`.

## How it works

The bar is one short-lived process in a herdr popup pane. It reads the whole
session in a single `session.snapshot` call over herdr's Unix socket (~15ms),
re-reads it while it is open so statuses stay live, tails the selected pane with
`pane.read` for the preview, and calls `tab.focus`, `pane.focus`, `agent.focus`,
or `workspace.focus` when you press Enter, or `tab.close` when you confirm a
delete. Running times come from `pane.process_info` plus `ps`, one reading per
pane on the way onto the screen and then ticked locally, because a start time
never moves. If the socket is unavailable it falls back to the `herdr` CLI.
Eligible Claude and Codex tabs are updated through `tab.rename`, with a fresh
name and session check before each write. Claude transcript fallback uses a
bounded local read. `--list` and `--doctor` remain read-only. A singleton
background watcher checks titles every two seconds; popup refreshes only read
Herdr's snapshot. Each idle tick makes one snapshot request. Plugin registration
is checked through `plugin.list` every ten seconds, without assuming any registry
file location. Automatic-name ownership and watcher locks are isolated by socket
path under `HERDR_PLUGIN_STATE_DIR/title-servers/`; recent rows remain shared.
The watcher uses only the injected socket, reconnects with backoff up to thirty
seconds during server downtime, and remains asleep between attempts. It never
starts a server or falls back to a different session. An explicit stop still
works while offline. Plugin replacement reloads the registered code while
retaining the singleton lock. Diagnostics are written to `title-watcher.log`
in the server's state subdirectory. Tab titles themselves are stored by Herdr.

## Development

```bash
git clone https://github.com/jeffarese/herdr-bar
cd herdr-bar
herdr plugin link .

PYTHONPATH=src python3 -m unittest discover -s tests -t .   # no deps
python3 run.py --doctor                         # environment diagnostics
python3 run.py --list                           # the rows, as JSON
python3 scripts/demo.py                         # run against fixture data
python3 scripts/demo.py --frame --plain         # print one static frame
ruff check .                                    # lint, if you have it
```

`scripts/demo.py` needs no herdr server, which makes it the fastest way to work
on the UI. `herdr plugin log list --plugin herdr-bar` shows what herdr
recorded when it launched the plugin.

## Goes well with

[herdr-newtab-plus](https://github.com/jeffarese/herdr-newtab-plus) is the
other half of the loop. This bar jumps you to the tabs you already have; that
plugin opens the one you don't — it asks which folder and which agent,
completes real paths, remembers where you work, and starts the agent for you.

## License

MIT. See [LICENSE](LICENSE).
