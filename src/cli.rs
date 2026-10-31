//! Command line entry point.

use crate::app::{in_popup, jump, load_actions, run_action, Bar, Pool};
use crate::client::HerdrClient;
use crate::config::Config;
use crate::host;
use crate::icons;
use crate::items::{build_items, KIND_ACTION};
use crate::mru::Recents;
use crate::opener::open_bar;
use crate::pyjson::{self, Map, Value};
use crate::term::{detect_appearance, isatty, Terminal};
use crate::theme::{default_selection_background, Theme};
use crate::title_watcher;
use std::io::Write;
use std::path::PathBuf;

pub const VERSION: &str = "0.14.0";

pub const USAGE: &str = "herdr-bar - jump to any Herdr tab, pane, or agent

usage: run.py [options]

options:
  --open      open the bar popup through the Herdr CLI
  --list      print the rows the bar would show, as JSON
  --doctor    print environment diagnostics
  --version   print the plugin version
  --help      show this message

Normally Herdr launches this itself:
  herdr plugin action invoke herdr-bar.open
";

fn out(text: &str) {
    let mut stdout = std::io::stdout().lock();
    let _ = stdout.write_all(text.as_bytes());
    let _ = stdout.flush();
}

pub fn state_path() -> Option<PathBuf> {
    let state_dir = std::env::var_os("HERDR_PLUGIN_STATE_DIR").filter(|v| !v.is_empty())?;
    // os.path.join, which keeps the directory exactly as given.
    let mut text = state_dir.to_string_lossy().into_owned();
    let separator = if cfg!(windows) { ['\\', '/'] } else { ['/', '/'] };
    if !text.ends_with(separator[0]) && !text.ends_with(separator[1]) {
        text.push(if cfg!(windows) { '\\' } else { '/' });
    }
    text.push_str("recent.json");
    Some(PathBuf::from(text))
}

fn fail(message: &str) -> i32 {
    eprintln!("herdr-bar: {}", message);
    2
}

fn platform() -> &'static str {
    if cfg!(windows) {
        "win32"
    } else if cfg!(target_os = "macos") {
        "darwin"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else if cfg!(target_os = "freebsd") {
        "freebsd"
    } else {
        std::env::consts::OS
    }
}

fn count(value: &Value) -> usize {
    match value {
        Value::List(list) => list.len(),
        Value::Dict(map) => map.len(),
        Value::Str(text) => crate::pystr::len(text),
        _ => 0,
    }
}

fn doctor(client: &mut HerdrClient, config: &Config) -> i32 {
    let mut lines = vec![
        format!("herdr-bar {}", VERSION),
        format!(
            "binary           {}",
            std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|_| "?".into())
        ),
        format!("platform         {}", platform()),
        format!("herdr bin        {}", client.bin_path),
        format!("socket           {}", client.socket_path.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| "(unset)".into())),
    ];
    if host::WINDOWS {
        if let Some(path) = client.socket_path.clone().filter(|s| !s.is_empty()) {
            lines.push(format!("named pipe       {}", host::pipe_name(&path)));
        }
    }
    lines.push(format!("socket usable    {}", if client.socket_ok() { "yes" } else { "no, using the CLI" }));
    lines.push(format!(
        "config           {}",
        Config::path().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "(no HERDR_PLUGIN_CONFIG_DIR)".into())
    ));
    lines.push(format!(
        "state            {}",
        state_path().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "(no HERDR_PLUGIN_STATE_DIR)".into())
    ));
    lines.push(format!("preview          {}", pyjson::py_str(&config.preview)));
    lines.push(format!("refresh          {}ms", config.refresh_ms));
    lines.push(format!("stdin is a tty   {}", if isatty(0) { "yes" } else { "no" }));
    let snapshot = match client.snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            lines.push(format!("herdr            unreachable: {}", error));
            out(&(lines.join("\n") + "\n"));
            return 1;
        }
    };
    let items = build_items(&snapshot);
    let version = match snapshot.as_dict().and_then(|s| s.get("version")) {
        Some(value) => pyjson::py_str(value),
        None => "?".into(),
    };
    lines.push(format!("herdr            {}", version));
    lines.push(format!(
        "rows             {} ({} workspaces, {} tabs, {} agents)",
        items.len(),
        count(snapshot.get("workspaces")),
        count(snapshot.get("tabs")),
        count(snapshot.get("agents")),
    ));
    if config.actions {
        lines.push(format!("action rows      {}", load_actions(client).len()));
    }
    out(&(lines.join("\n") + "\n"));
    0
}

fn optional(value: &Option<String>) -> Value {
    value.clone().map(Value::str).unwrap_or(Value::Null)
}

fn list(client: &mut HerdrClient) -> crate::client::Result<i32> {
    let snapshot = client.snapshot()?;
    let payload: Vec<Value> = build_items(&snapshot)
        .iter()
        .map(|item| {
            let mut map = Map::new();
            map.insert("kind", Value::str(item.kind));
            map.insert("key", Value::str(item.key.clone()));
            map.insert("title", Value::str(item.title.clone()));
            map.insert("subtitle", Value::str(item.subtitle.clone()));
            map.insert("workspace", Value::str(item.workspace_label.clone()));
            map.insert("tab_id", optional(&item.tab_id));
            map.insert("pane_id", optional(&item.pane_id));
            map.insert("agent", optional(&item.agent));
            map.insert("status", Value::str(item.status.clone()));
            map.insert("tab", item.tab_number_value.clone().unwrap_or(Value::Null));
            map.insert("focused", Value::Bool(item.focused));
            Value::Dict(map)
        })
        .collect();
    out(&(pyjson::dumps_indent(&Value::List(payload), 2) + "\n"));
    Ok(0)
}

pub fn main(args: Vec<String>) -> i32 {
    let has = |flag: &str| args.iter().any(|a| a == flag);
    if has("--help") || has("-h") {
        out(USAGE);
        return 0;
    }
    if has("--version") {
        out(&format!("herdr-bar {}\n", VERSION));
        return 0;
    }
    if has("--open") {
        return open_bar();
    }

    if has("--start-titles") || has("--watch-titles") || has("--stop-titles") {
        if args[0] == "--watch-titles" {
            let Some(token) = args.get(1) else {
                eprintln!("herdr-bar: --watch-titles needs a descriptor or -");
                return 1;
            };
            if token == title_watcher::LOCK_BY_NAME {
                return title_watcher::watch(None);
            }
            return match crate::pystr::parse_int(token) {
                Some(fd) => title_watcher::watch(Some(fd as i64)),
                None => {
                    eprintln!("herdr-bar: invalid literal for int() with base 10: {}", crate::pyjson::str_repr(token));
                    1
                }
            };
        }
        if args[0] == "--stop-titles" {
            if let Err(message) = title_watcher::stop() {
                eprintln!("herdr-bar: {}", message);
                return 1;
            }
        } else {
            title_watcher::start(has("--resume"));
        }
        return 0;
    }

    let config = Config::load();
    let mut client = HerdrClient::from_env();

    if has("--doctor") {
        return doctor(&mut client, &config);
    }
    if has("--list") {
        return match list(&mut client) {
            Ok(code) => code,
            Err(error) => fail(&error.message),
        };
    }

    if !isatty(0) || !isatty(1) {
        return fail("needs a terminal; run it from Herdr with `plugin pane open`");
    }

    // Two slow, independent reads run on their own threads while the
    // terminal answers the appearance query: the font check (fc-match) and
    // the first snapshot. Both are joined, in the original order, below.
    if config.agent_icons.as_str() == Some("auto") {
        icons::prefetch();
    }
    let fetch_config = config.clone();
    // The plugin actions, when asked for, are a third read of their own.
    let actions = config.actions.then(|| {
        let mut client = client.clone();
        std::thread::spawn(move || load_actions(&mut client))
    });
    // The answer comes back over a channel so the thread can go on to free
    // the parsed snapshot while the bar draws.
    let (fetched, fetch) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut client = client;
        let mut answer = Vec::new();
        // Nothing to compare against yet, so this always reads a snapshot.
        let snapshot = client.snapshot_since(&mut answer).map(|snapshot| snapshot.unwrap_or(Value::Null));
        let pool = snapshot.as_ref().map(|snapshot| Pool::build(snapshot, &fetch_config)).map_err(|error| error.clone());
        let _ = fetched.send((client, pool, answer));
        drop(snapshot);
    });

    let mut recents = Recents::new(state_path());
    let configured = config.selection_background.clone();
    let auto = configured.as_str() == Some("auto");
    // One appearance query, and only when something still depends on it: it
    // costs a round trip to the terminal on every open.
    let mut appearance = None;
    if auto || !config.colors.contains_key("muted") {
        appearance = detect_appearance(0.12);
    }
    let selection_background: Option<Value> =
        if auto { default_selection_background(appearance).map(Value::str) } else { Some(configured) };
    let theme = Theme::new(Some(&config.colors), selection_background.as_ref(), appearance);

    title_watcher::start(false);
    let mouse = config.mouse;
    let (client, pool, answer) = fetch.recv().expect("the snapshot thread panicked");
    // Never dropped: the process ends right after, and freeing thousands of
    // rows one by one would only delay the exit (Bar owns nothing to flush).
    let mut bar = std::mem::ManuallyDrop::new(Bar::new(client, config, Recents::new(None), theme));
    std::mem::swap(&mut bar.recents, &mut recents);
    if let Some(actions) = actions {
        bar.actions = actions.join().unwrap_or_default();
    }
    if let Err(error) = bar.bootstrap(pool, answer) {
        return fail(&error.message);
    }

    let chosen = {
        let mut terminal = match Terminal::enter(mouse) {
            Ok(terminal) => terminal,
            Err(error) => return fail(&host::os_error_str(&error)),
        };
        bar.run(&mut terminal)
    };

    let Some(chosen) = chosen else { return 0 };
    let item = bar.pool[chosen].clone();
    if item.kind == KIND_ACTION {
        // Recents stay as they were: an action is not a place to come back to.
        return match run_action(&mut bar.client, &item, in_popup()) {
            Ok(()) => 0,
            Err(error) => fail(&error.message),
        };
    }
    if let Err(error) = jump(&mut bar.client, &item) {
        return fail(&error.message);
    }
    let current_key = bar.current_key.clone();
    let current_title = bar.current_title.clone();
    if let Some(current) = current_key.filter(|k| !k.is_empty()) {
        if current != item.key {
            bar.recents.touch(&current, &current_title);
        }
    }
    bar.recents.touch(&item.key, &item.title);
    bar.recents.save();
    0
}
