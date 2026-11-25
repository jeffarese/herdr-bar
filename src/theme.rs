//! Colors.
//!
//! Defaults are plain ANSI colors, so the bar inherits whatever theme the
//! terminal (and therefore Herdr) already uses. Status glyphs and colors
//! mirror Herdr's own sidebar so the popup reads as part of the app. Anything
//! can be overridden with a hex value from the plugin config.

use crate::pyjson::{self, Map, Value};
use crate::pystr;
use std::collections::HashMap;

pub const RESET: &str = "\x1b[0m";
pub const BOLD: &str = "\x1b[1m";
pub const UNDERLINE: &str = "\x1b[4m";

fn named(spec: &str) -> Option<u32> {
    Some(match spec {
        "black" => 0,
        "red" => 1,
        "green" => 2,
        "yellow" => 3,
        "blue" => 4,
        "magenta" => 5,
        "cyan" => 6,
        "white" => 7,
        "bright_black" | "gray" | "grey" => 8,
        "bright_red" => 9,
        "bright_green" => 10,
        "bright_yellow" => 11,
        "bright_blue" => 12,
        "bright_magenta" => 13,
        "bright_cyan" => 14,
        "bright_white" => 15,
        _ => return None,
    })
}

pub const DEFAULTS: &[(&str, &str)] = &[
    ("accent", "bright_blue"),
    ("text", "default"),
    ("muted", "244"),
    ("blocked", "bright_red"),
    ("working", "#c78a1f"),
    ("done", "bright_cyan"),
    ("idle", "bright_green"),
    ("unknown", "bright_black"),
    ("match", "bright_blue"),
    ("agent_claude", "#D97757"),
    ("agent_codex", "#10A37F"),
    ("agent_kimi", "#9B8AFB"),
    ("agent_pi", "default"),
    ("agent_grok", "default"),
    ("agent_gemini", "#4285F4"),
    ("agent_cursor", "#A78BFA"),
    ("agent_opencode", "#38BDF8"),
    ("agent_agy", "#7298F3"),
];

pub fn status_glyph(status: &str) -> &'static str {
    match status {
        "blocked" => "◉",
        "working" => "◐",
        "done" => "●",
        "idle" => "✓",
        "none" => "·",
        "action" => "›",
        _ => "○",
    }
}

fn hex2(text: &str) -> Option<i128> {
    pystr::parse_int_base(text, 16)
}

/// The SGR parameters for a color spec, or None for 'default'.
fn parse_spec(spec: &str) -> Option<String> {
    let spec = pystr::lower(pystr::strip(spec));
    if spec.is_empty() || matches!(spec.as_str(), "default" | "none" | "reset" | "inherit") {
        return None;
    }
    let chars: Vec<char> = spec.chars().collect();
    if spec.starts_with('#') && chars.len() == 7 {
        let part = |a: usize, b: usize| -> String { chars[a..b].iter().collect() };
        let red = hex2(&part(1, 3))?;
        let green = hex2(&part(3, 5))?;
        let blue = hex2(&part(5, 7))?;
        return Some(format!("2;{};{};{}", red, green, blue));
    }
    if let Some(code) = named(&spec) {
        return Some(format!("5;{}", code));
    }
    if pystr::str_isdigit(&spec) {
        // int() reads decimal digits only; a superscript is a digit it rejects.
        if let Some(value) = pystr::parse_int(&spec) {
            if (0..=255).contains(&value) {
                return Some(format!("5;{}", value));
            }
        }
    }
    None
}

/// `_parse(value)` for any JSON value: str() first, as the Python did.
pub fn parse_value(value: &Value) -> Option<String> {
    if value.is_null() {
        return None;
    }
    parse_spec(&pyjson::py_str(value))
}

#[derive(Clone, Debug)]
pub struct Theme {
    fg: HashMap<String, String>,
    pub agent_icons: bool,
    pub selection_bg: String,
}

impl Theme {
    pub fn new(overrides: Option<&Map>, selection_background: Option<&Value>, appearance: Option<&str>) -> Theme {
        let mut merged: Vec<(String, String)> =
            DEFAULTS.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        let set = |merged: &mut Vec<(String, String)>, key: &str, value: String| {
            if let Some(slot) = merged.iter_mut().find(|(k, _)| k == key) {
                slot.1 = value;
            } else {
                merged.push((key.to_string(), value));
            }
        };
        if let Some(muted) = default_muted(appearance) {
            set(&mut merged, "muted", muted.to_string());
        }
        if let Some(overrides) = overrides {
            for (key, value) in overrides.iter() {
                if let Value::Str(spec) = value {
                    set(&mut merged, key, spec.clone());
                }
            }
        }
        let mut fg = HashMap::new();
        for (role, spec) in merged {
            let params = parse_spec(&spec);
            fg.insert(role, params.map(|p| format!("\x1b[38;{}m", p)).unwrap_or_default());
        }
        let selection = selection_background.and_then(parse_value);
        Theme {
            fg,
            agent_icons: false,
            selection_bg: selection.map(|p| format!("\x1b[48;{}m", p)).unwrap_or_default(),
        }
    }

    pub fn fg(&self, role: &str) -> &str {
        self.fg.get(role).map(|s| s.as_str()).unwrap_or("")
    }

    pub fn paint(&self, role: &str, text: &str) -> String {
        self.paint_bold(role, text, false)
    }

    pub fn paint_bold(&self, role: &str, text: &str, bold: bool) -> String {
        let color = self.fg(role);
        if color.is_empty() && !bold {
            return text.to_string();
        }
        format!("{}{}{}{}", if bold { BOLD } else { "" }, color, text, RESET)
    }

}

/// The secondary foreground: legible, still clearly below the title.
pub fn default_muted(appearance: Option<&str>) -> Option<&'static str> {
    match appearance {
        Some("dark") => Some("249"),
        Some("light") => Some("240"),
        _ => None,
    }
}

/// A selection background that suits the terminal's own background.
pub fn default_selection_background(appearance: Option<&str>) -> Option<&'static str> {
    match appearance {
        Some("dark") => Some("237"),
        Some("light") => Some("254"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secondary_text_is_brighter_than_the_separators() {
        for appearance in [None, Some("dark"), Some("light")] {
            let theme = Theme::new(None, None, appearance);
            assert_ne!(theme.fg("muted"), theme.fg("unknown"), "{:?}", appearance);
        }
    }

    #[test]
    fn a_configured_muted_wins_over_the_appearance() {
        let mut overrides = Map::new();
        overrides.insert("muted", Value::Str("#ff00ff".into()));
        let theme = Theme::new(Some(&overrides), None, Some("dark"));
        assert_eq!(theme.fg("muted"), "\x1b[38;2;255;0;255m");
    }
}
