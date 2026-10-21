//! The Python release's answers, replayed against the port.
//!
//! `tests/data/fuzz-corpus.jsonl` holds one case per line: a function name,
//! its arguments, and what the Python release returned for them, shaped as
//! JSON. The inputs are the ones the old differential fuzzer generated with
//! seed 1, recorded once with Python 3.13 (Unicode 15.1, the release
//! `src/unicode_tables.rs` comes from) and `HOME=/home/fuzz`. Inputs the
//! Python functions raised on were left out. `call` runs the Rust port of
//! each function and shapes its result the same way. Test builds only.

use crate::keys::{Event, KeyDecoder, MouseKind};
use crate::paint::Painter;
use crate::pyjson::{self, Map, Value};
use crate::{age, auto_title, fuzzy, items, pystr, renaming_guard, sequences, tab_status, textutil, theme};

fn s(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

fn int(value: &Value) -> i128 {
    value.as_int().unwrap_or(0)
}

fn bytes(value: &Value) -> Vec<u8> {
    value.list_or_empty().iter().map(|b| int(b) as u8).collect()
}

fn opt(text: Option<&str>) -> Value {
    text.map(Value::str).unwrap_or(Value::Null)
}

fn opt_string(text: &Option<String>) -> Value {
    text.clone().map(Value::str).unwrap_or(Value::Null)
}

fn strs<S: AsRef<str>>(values: &[S]) -> Value {
    Value::List(values.iter().map(|v| Value::str(v.as_ref())).collect())
}

fn events(list: Vec<Event>) -> Vec<Value> {
    list.into_iter()
        .map(|event| {
            let (kind, value, x, y) = match event {
                Event::Key(v) => ("key", v, 0, 0),
                Event::Text(v) => ("text", v, 0, 0),
                Event::Paste(v) => ("paste", v, 0, 0),
                Event::Mouse { kind, x, y } => {
                    let name = match kind {
                        MouseKind::Press => "press",
                        MouseKind::Release => "release",
                        MouseKind::WheelUp => "wheel_up",
                        MouseKind::WheelDown => "wheel_down",
                    };
                    ("mouse", name.to_string(), x, y)
                }
            };
            Value::List(vec![Value::str(kind), Value::str(value), Value::Int(x), Value::Int(y)])
        })
        .collect()
}

fn item(item: &items::Item) -> Value {
    let mut map = Map::new();
    map.insert("kind", Value::str(item.kind));
    map.insert("key", Value::str(item.key.clone()));
    map.insert("title", Value::str(item.title.clone()));
    map.insert("detail", Value::str(item.detail.clone()));
    map.insert("subtitle", Value::str(item.subtitle.clone()));
    map.insert("workspace_id", Value::str(item.workspace_id.clone()));
    map.insert("workspace_label", Value::str(item.workspace_label.clone()));
    map.insert("tab_id", opt_string(&item.tab_id));
    map.insert("pane_id", opt_string(&item.pane_id));
    map.insert("agent", opt_string(&item.agent));
    map.insert("agent_name", opt_string(&item.agent_name));
    map.insert("status", Value::str(item.status.clone()));
    map.insert("tab_number", item.tab_number_value.clone().unwrap_or(Value::Null));
    map.insert("focused", Value::Bool(item.focused));
    map.insert("fields", strs(&item.fields));
    map.insert("detail_field", Value::Int(item.detail_field as i128));
    Value::Dict(map)
}

fn call(name: &str, args: &[Value]) -> Value {
    let a = |i: usize| args.get(i).unwrap_or(&Value::Null);
    match name {
        "lower" => Value::str(pystr::lower(s(a(0)))),
        "casefold" => Value::str(pystr::casefold(s(a(0)))),
        "islower" => Value::Bool(pystr::str_islower(s(a(0)))),
        "isdigit" => Value::Bool(pystr::str_isdigit(s(a(0)))),
        "strip" => Value::str(pystr::strip(s(a(0)))),
        "split" => strs(&pystr::split_whitespace(s(a(0)))),
        "splitlines" => strs(&pystr::splitlines(s(a(0)))),
        "int" => pystr::parse_int(s(a(0))).map(|v| Value::str(v.to_string())).unwrap_or(Value::Null),
        "len" => Value::Int(pystr::len(s(a(0))) as i128),
        "json" => match pyjson::loads(s(a(0))) {
            Ok(value) => Value::List(vec![
                Value::str(pyjson::dumps(&value, false)),
                Value::str(pyjson::dumps(&value, true)),
                Value::str(pyjson::dumps_indent(&value, 2)),
                Value::str(pyjson::py_str(&value)),
                Value::Bool(value.truthy()),
            ]),
            Err(_) => Value::str("error"),
        },
        "float_repr" => Value::str(match a(0) {
            Value::Float(f) => pyjson::float_repr(*f),
            other => pyjson::py_repr(other),
        }),
        "json_eq" => match (pyjson::loads(s(a(0))), pyjson::loads(s(a(1)))) {
            (Ok(x), Ok(y)) => Value::Bool(pyjson::py_eq(&x, &y)),
            _ => Value::str("error"),
        },
        "display_width" => Value::Int(textutil::display_width(s(a(0))) as i128),
        "visible_width" => Value::Int(textutil::visible_width(s(a(0))) as i128),
        "strip_ansi" => Value::str(textutil::strip_ansi(s(a(0)))),
        "sanitize" => Value::str(textutil::sanitize(s(a(0)), s(a(1)))),
        "truncate" => Value::str(textutil::truncate_with(s(a(0)), int(a(1)) as isize, s(a(2)))),
        "truncate_middle" => Value::str(textutil::truncate_middle(s(a(0)), int(a(1)) as isize)),
        "pad" => Value::str(textutil::pad(s(a(0)), int(a(1)) as isize)),
        "window_positions" => {
            let positions: Vec<usize> = a(2).list_or_empty().iter().map(|p| int(p) as usize).collect();
            let (text, shifted, start) = textutil::window_positions(s(a(0)), int(a(1)) as isize, &positions);
            Value::List(vec![
                Value::str(text),
                Value::List(shifted.iter().map(|&p| Value::Int(p as i128)).collect()),
                Value::Int(start as i128),
            ])
        }
        "split_query" => strs(&fuzzy::split_query(s(a(0)))),
        "match" => {
            let (query, text) = (s(a(0)), s(a(1)));
            let found = if query.is_empty() {
                Some(fuzzy::Match { score: 0, positions: Vec::new() })
            } else if text.is_empty() {
                None
            } else {
                fuzzy::match_prepared(&fuzzy::Term::new(query), &fuzzy::Prepared::new(text))
            };
            match found {
                Some(m) => Value::List(vec![
                    Value::Int(m.score as i128),
                    Value::List(m.positions.iter().map(|&p| Value::Int(p as i128)).collect()),
                ]),
                None => Value::Null,
            }
        }
        "keys" => {
            // Feed the chunks in order, flushing where the chunk list says to.
            let mut decoder = KeyDecoder::new();
            let mut out = Vec::new();
            for chunk in a(0).list_or_empty() {
                if chunk.is_null() {
                    out.push(Value::Bool(decoder.pending_escape()));
                    out.extend(events(decoder.flush()));
                } else {
                    out.extend(events(decoder.feed(&bytes(chunk))));
                }
            }
            Value::List(out)
        }
        "reply_complete" => Value::Bool(sequences::reply_complete(&bytes(a(0)))),
        "appearance" => opt(sequences::appearance_from_reply(&bytes(a(0)))),
        "parse_etime" => age::parse_etime(s(a(0))).map(Value::Float).unwrap_or(Value::Null),
        "format_age" => Value::str(age::format_age(match a(0) {
            Value::Float(f) => *f,
            other => int(other) as f64,
        })),
        "pid_for" => age::pid_for(a(0)).unwrap_or(Value::Null),
        "theme_parse" => opt_string(&theme::parse_value(a(0))),
        "tab_strip" => Value::str(tab_status::strip(s(a(0)))),
        "tab_compose" => Value::str(tab_status::compose(s(a(0)), s(a(1)), s(a(2)).chars().next())),
        "tab_states" => {
            let mut keys: Vec<(String, (String, String))> = tab_status::tab_states(a(0)).into_iter().collect();
            keys.sort();
            Value::List(
                keys.into_iter()
                    .map(|(k, (x, y))| Value::List(vec![Value::str(k), Value::str(x), Value::str(y)]))
                    .collect(),
            )
        }
        "naming" => Value::Bool(renaming_guard::naming_search(s(a(0)))),
        "session_id" => Value::Bool(auto_title::is_session_id(s(a(0)))),
        "opening" => Value::str(auto_title::opening(a(0))),
        "agent_title" => Value::str(auto_title::agent_title(a(0), a(1))),
        "default_agent_name" => Value::Bool(auto_title::default_agent_name(a(0), a(1), s(a(2)))),
        "candidates" => {
            let empty = Map::new();
            let owned = a(1).as_dict().unwrap_or(&empty);
            let mut out = Map::new();
            for (tab, c) in auto_title::candidates(a(0), owned, false) {
                let mut map = Map::new();
                map.insert("identity", c.identity);
                map.insert("agent", Value::str(c.agent));
                map.insert("suggested", Value::str(c.suggested));
                map.insert("session", opt_string(&c.session));
                map.insert("pane", c.pane);
                map.insert("terminal", c.terminal);
                map.insert("label", c.label);
                map.insert("cwd", c.cwd);
                out.insert(tab, Value::Dict(map));
            }
            Value::Dict(out)
        }
        "shorten_path" => Value::str(items::shorten_path(s(a(0)))),
        "build_items" => Value::List(items::build_items(a(0)).iter().map(item).collect()),
        "build_pane_items" => Value::List(items::build_pane_items(a(0)).iter().map(item).collect()),
        "painter" => {
            let mut painter = Painter::new();
            let mut out = Vec::new();
            for frame in a(0).list_or_empty() {
                if frame.is_null() {
                    painter.reset();
                    continue;
                }
                let parts = frame.list_or_empty();
                let rows: Vec<String> = parts[0].list_or_empty().iter().map(|r| s(r).to_string()).collect();
                out.push(Value::str(painter.frame(&rows, int(&parts[1]) as usize, int(&parts[2]) as usize)));
            }
            Value::List(out)
        }
        _ => Value::str(format!("unknown probe {}", name)),
    }
}

#[cfg(unix)]
#[test]
fn python_answers() {
    // Paths shorten against HOME, and the corpus was recorded with this one.
    let _env = crate::testutil::env_lock();
    let mut vars = crate::testutil::EnvVars::new();
    vars.set("HOME", "/home/fuzz");
    for key in ["HERDR_SOCKET_PATH", "HERDR_PLUGIN_CONFIG_DIR", "HERDR_PLUGIN_STATE_DIR", "HERDR_PLUGIN_ROOT", "HERDR_BIN_PATH"] {
        vars.remove(key);
    }
    let mut cases = 0;
    let mut failures = Vec::new();
    for (index, line) in include_str!("../tests/data/fuzz-corpus.jsonl").lines().enumerate() {
        let case = pyjson::loads(line).expect("corpus line parses");
        let parts = case.list_or_empty();
        let name = parts[0].as_str().expect("case name");
        let expected = pyjson::dumps(&parts[2], true);
        let actual = pyjson::dumps(&call(name, parts[1].list_or_empty()), true);
        cases += 1;
        if actual != expected {
            failures.push(format!(
                "line {} {}\n    args   {}\n    python {}\n    rust   {}",
                index + 1,
                name,
                pyjson::dumps(&parts[1], false),
                expected,
                actual
            ));
        }
    }
    assert!(cases > 19_000, "the corpus has only {} cases", cases);
    assert!(
        failures.is_empty(),
        "{} of {} cases differ from Python:\n{}",
        failures.len(),
        cases,
        failures.iter().take(10).cloned().collect::<Vec<_>>().join("\n")
    );
}
