# herdr-bar

[![ci](https://github.com/jeffarese/herdr-bar/actions/workflows/ci.yml/badge.svg)](https://github.com/jeffarese/herdr-bar/actions/workflows/ci.yml)

**Cmd+K for [herdr](https://herdr.dev).** One chord opens a search field over your
session, you type a few letters of a tab, an agent, a repo or a branch, and Enter
takes you there. Like the Slack quick switcher, for the terminal.

```text
 ❯  jump to a tab or agent…    @ agents   $ shells   ! needs you
 ──────────────────────────────────────────────────────────────────────────────────────────────
 ▌ ◉ retry queue backoff                    codex · needs you │ retry queue backoff
   ⠋ Check weekly explanation display       current · working │ ~/workspace/erestor
   ◉ cards meaningless                web · codex · needs you │ ───────────────────────────────
   ● library depth battery          library · @battery · done │
   ⠋ Redesign week loading screen            claude · working │
   ✓ migrations                       erestor · gemini · idle │ ● Ready · pacebeats
   · server                                         pacebeats │   Capacity: 0/32 · new session…
   · logs                                           pacebeats │
   ◉ erestor                                needs you · space │ > the cards still read as mean…
   ⠋ pacebeats                                working · space │
                                                              │ ⏺ Reading src/components/WeekC…
                                                              │   Found 3 call sites that rend…
 ──────────────────────────────────────────────────────────────────────────────────────────────
 ↑↓ move  ⏎ jump  ⇥ scope  ^o preview  esc close                                             10
```

- **fuzzy search over everything you can jump to** — agents, plain tabs, and
  workspaces, matched on title, working directory, agent name and kind, branch,
  workspace, and tab number.
- **live status, herdr's own language** — `◉` needs you, spinner working,
  `●` done, `✓` idle. Colors and glyphs mirror the herdr sidebar, and the list
  keeps updating while it is open.
- **opens on what matters** — blocked and finished agents float to the top,
  recently visited rows above them, and the tab you are in is never first, so
  open-then-Enter works like alt-tab.
- **live preview** — the right column tails the selected pane, so you can look
  before you leap.
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
| `esc`, `ctrl+c`, `ctrl+g` | close, change nothing |
| `tab` / `shift+tab` | cycle the filter |
| `@` `$` `!` on an empty query | filter to agents / plain tabs / rows that need you |
| `backspace` on an empty query | clear the filter |
| `ctrl+u` / `ctrl+w` | clear the query / delete a word |
| `ctrl+o` | toggle the preview |
| `ctrl+r` | refresh now |
| `pgup` / `pgdn` | page |
| wheel / click | move / select, click again to jump |

**What is in the list.** Every agent, every tab that has no agent, and — once a
session has more than one — every workspace. Selecting an agent focuses its tab
and its pane; selecting a workspace focuses the workspace.

**How it is ordered.** With no query: recently visited rows first, then rows that
want your attention (blocked, then done, then working), then the rest by
workspace and tab number. The tab you are currently in is never first. With a
query: best fuzzy score wins, ties broken by recency and then status. Matches in
the title outrank matches in a working directory or an agent name, and only title
matches are highlighted.

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
| `preview` | `"auto"` | `true`, `false`, or `"auto"` (on when the popup is wide enough) |
| `mouse` | `true` | click and wheel support |
| `spinner` | `true` | animate the working glyph |
| `refresh_ms` | `900` | how often the open bar re-reads the session |
| `workspaces` | `"auto"` | `true`, `false`, or `"auto"` (on with more than one workspace) |
| `selection_background` | `"auto"` | `"auto"` asks the terminal for its background color, or set `"none"`, a hex value, or a 0-255 ANSI index |
| `colors` | `{}` | role → `#rrggbb`, an ANSI name (`bright_blue`), or 0-255. Roles: `accent`, `match`, `text`, `muted`, `blocked`, `working`, `done`, `idle`, `unknown` |

Colors default to plain ANSI, so the bar follows whatever theme your terminal
already uses.

Popup size lives in herdr, not here. Override the manifest's `74%` × `62%` per
invocation with `herdr plugin pane open --plugin herdr-bar --entrypoint bar
--placement popup --width 60% --height 50%`.

## How it works

The bar is one short-lived process in a herdr popup pane. It reads the whole
session in a single `session.snapshot` call over herdr's Unix socket (~15ms),
re-reads it while it is open so statuses stay live, tails the selected pane with
`pane.read` for the preview, and calls `tab.focus` / `agent.focus` /
`workspace.focus` when you press Enter. If the socket is unavailable it falls
back to the `herdr` CLI. Nothing runs in the background, and the only state it
keeps is a list of recently visited rows under `HERDR_PLUGIN_STATE_DIR`.

## Development

```bash
git clone https://github.com/jeffarese/herdr-bar
cd herdr-bar
herdr plugin link .

python3 -m unittest discover -s tests -t .      # 142 tests, no dependencies
python3 run.py --doctor                         # environment diagnostics
python3 run.py --list                           # the rows, as JSON
python3 scripts/demo.py                         # run against fixture data
python3 scripts/demo.py --frame --plain         # print one static frame
ruff check .                                    # lint, if you have it
```

`scripts/demo.py` needs no herdr server, which makes it the fastest way to work
on the UI. `herdr plugin log list --plugin herdr-bar` shows what herdr
recorded when it launched the plugin.

## License

MIT. See [LICENSE](LICENSE).
