# herdr-bar — auto tab titles, agent icons & Cmd+K search

[![ci](https://github.com/jeffarese/herdr-bar/actions/workflows/ci.yml/badge.svg)](https://github.com/jeffarese/herdr-bar/actions/workflows/ci.yml)

**A [Herdr](https://herdr.dev) plugin for automatic tab titles, recognizable
agent icons, and fast session switching.** Press Cmd+K, type a few letters of a
task, tab, pane, agent, repo or branch, and Enter takes you there. Like the Slack
quick switcher, for the terminal.

![Herdr command bar with auto tab titles, Claude and Codex agent icons, and fuzzy search](assets/demo.gif)

- **auto title for unnamed tabs** — reads Claude Code's local transcript to
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
  runtime to install, no daemon.

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

Requires herdr 0.7.4+ (the release that added popup plugin panes), Python 3.9+
on PATH, and macOS or Linux. Herdr refuses to install the plugin on anything
older, so there is nothing to get wrong.

## Auto title: automatic tab naming

Open the bar and unnamed Claude Code tabs pick up their session's title from
the local transcript. A tab such as `3` becomes `Repair OAuth callbacks` — just
the task, without a repeated agent name, folder or tab-number prefix. If Claude
has not generated a title yet, the first human prompt or slash command provides
the name. Matching tab titles and agent summaries appear only once in the list.

Automatic tab renaming runs when the bar opens and while it refreshes. It uses
the transcript-reading idea from
[herdr-auto-title](https://github.com/kryptamine/herdr-auto-title), without
starting a background service or making an extra AI request.

- **Your names win.** Only empty names and default position numbers qualify.
  A custom tab title, pane label or agent name prevents automatic renaming,
  including on the very first launch. Names are checked again before writing.
- **Name once, keep it.** Once filled, a tab title stays put across refreshes
  and reopening the bar. Clear its name to let automatic naming fill it again.
  Tabs containing multiple agents are left alone to avoid choosing the wrong task.
- **Local Claude transcripts only.** Requires Herdr's Claude integration to
  report the session ID and a readable transcript under `CLAUDE_CONFIG_DIR`
  (default `~/.claude`). Missing transcripts and other agent types leave names
  unchanged. Icons and search support all the agents listed above.
- **Enabled by default.** Set `HERDR_AUTO_TITLE_TRANSCRIPT=false` in the
  environment inherited by the popup to disable transcript reading and renaming.
  The standalone Auto Title plugin's `config.env` is not read by this plugin.

Herdr exposes the current name rather than its author: a manual name equal to
the tab's default position number is indistinguishable from an unnamed tab.
It also has no conditional rename API, so a rename made in the brief interval
between the final check and the write cannot be detected.

## Agent icons at a glance

Logos sit before the status and task title, making a mixed Claude, Codex, Gemini,
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
Eligible unnamed Claude tabs are filled through `tab.rename`, after a bounded
read of their local transcript and a fresh name check. `--list` and `--doctor`
remain read-only. Nothing runs in the background; transcript offsets are cached
only while the popup is open, and the only local persistent state is a list of
recently visited rows under `HERDR_PLUGIN_STATE_DIR`. Renamed titles are stored
by Herdr itself.

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
