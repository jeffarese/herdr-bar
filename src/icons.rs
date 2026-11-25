//! Optional Herdr Agent Icons Max vendor marks, compatible with herdr-radar.
//!
//! Codepoints: https://github.com/hhdebb/herdr-radar/blob/main/tools/codepoints.toml
//! The popup never installs fonts or changes terminal configuration.

use crate::pyjson::Value;
use crate::pyos;
use crate::pystr;
use std::cell::Cell;
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread::JoinHandle;

pub const FAMILY: &str = "Herdr Agent Icons Max";
pub const VENDORS: &[&str] = &[
    "claude", "codex", "opencode", "omp", "cline", "mastracode", "kimi", "kilo", "maki", "pi",
    "hermes", "cursor", "copilot", "deepseek", "gemini", "gpt", "qwen", "grok", "agy", "kiro",
    "amp", "devin", "qodercli", "glm",
];

pub fn logo(index: usize) -> char {
    char::from_u32(0xE1A0 + index as u32).unwrap()
}

pub fn is_logo(c: char) -> bool {
    (0xE1A0..0xE1A0 + VENDORS.len() as u32).contains(&(c as u32))
}

thread_local! {
    static FONT: Cell<Option<bool>> = const { Cell::new(None) };
}

/// Forget the cached answer, as `font_available.cache_clear()` did.
pub fn clear_font_cache() {
    FONT.with(|f| f.set(None));
}

fn env_or(name: &str) -> Option<String> {
    std::env::var_os(name).filter(|v| !v.is_empty()).map(|v| v.to_string_lossy().into_owned())
}

fn has_font_file(directory: &PathBuf) -> bool {
    let Ok(entries) = std::fs::read_dir(directory) else { return false };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        #[cfg(windows)]
        let name = name.to_lowercase();
        #[cfg(windows)]
        let (prefix, suffix) = ("herdragenticonsmax", ".ttf");
        #[cfg(not(windows))]
        let (prefix, suffix) = ("HerdrAgentIconsMax", ".ttf");
        if name.len() >= prefix.len() + suffix.len() && name.starts_with(prefix) && name.ends_with(suffix) {
            return true;
        }
    }
    false
}

fn detect() -> bool {
    let home = PathBuf::from(pyos::path_home());
    let directories: Vec<PathBuf> = if cfg!(target_os = "macos") {
        vec![home.join("Library/Fonts"), PathBuf::from("/Library/Fonts")]
    } else if cfg!(windows) {
        let local = env_or("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| home.join("AppData/Local"));
        let system = env_or("SYSTEMROOT").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("C:/Windows"));
        vec![local.join("Microsoft/Windows/Fonts"), system.join("Fonts")]
    } else {
        let data = env_or("XDG_DATA_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".local/share"));
        vec![data.join("fonts"), home.join(".fonts")]
    };
    if directories.iter().any(has_font_file) {
        return true;
    }
    if cfg!(target_os = "linux") {
        if let Some(output) = crate::proc::run_capture(
            &["fc-match".into(), "--format".into(), "%{family}".into(), FAMILY.into()],
            1.0,
        ) {
            let text = String::from_utf8_lossy(&output.stdout);
            let families: Vec<&str> = text.split(',').map(pystr::strip).collect();
            return output.success && families.contains(&FAMILY);
        }
    }
    false
}

/// A detection already running on another thread, for the next caller.
static PREFETCH: Mutex<Option<JoinHandle<bool>>> = Mutex::new(None);

/// Start detecting now, while the caller does other startup work.
///
/// `fc-match` costs several milliseconds and nothing it reads depends on
/// what happens meanwhile, so the popup overlaps it with the snapshot fetch.
/// The answer is the same one `font_available` would have computed.
pub fn prefetch() {
    let handle = std::thread::spawn(detect);
    if let Ok(mut slot) = PREFETCH.lock() {
        *slot = Some(handle);
    }
}

fn prefetched() -> Option<bool> {
    let handle = PREFETCH.lock().ok()?.take()?;
    handle.join().ok()
}

/// Best-effort local detection, once per popup; remote fonts need opt-in.
pub fn font_available() -> bool {
    FONT.with(|f| {
        if let Some(known) = f.get() {
            return known;
        }
        let found = prefetched().unwrap_or_else(detect);
        f.set(Some(found));
        found
    })
}

pub fn enabled(mode: &Value) -> bool {
    match mode.as_str() {
        Some("font") => true,
        Some("auto") => font_available(),
        _ => false,
    }
}

/// The agent kind as the icon and color tables know it.
pub fn agent_kind(agent: &str) -> String {
    let lowered = pystr::lower(pystr::strip(agent));
    let mut kind = lowered.split(':').next().unwrap_or("").to_string();
    if kind.ends_with(" code") {
        kind.truncate(kind.len() - 5);
    }
    kind
}

pub fn logo_for(agent: Option<&str>) -> Option<char> {
    let kind = agent_kind(agent.unwrap_or(""));
    let kind = match kind.as_str() {
        "cursor-agent" => "cursor",
        "open-code" => "opencode",
        other => other,
    };
    VENDORS.iter().position(|v| *v == kind).map(logo)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::items::{Item, ItemInit, KIND_AGENT};
    use crate::render::{render_row, Row};
    use crate::textutil::{strip_ansi, visible_width};
    use crate::theme::Theme;
    use std::collections::BTreeMap;

    fn s(text: &str) -> Value {
        Value::Str(text.into())
    }

    fn agent_item(agent: &str, agent_name: Option<&str>) -> Item {
        Item::new(ItemInit {
            kind: KIND_AGENT,
            key: "agent:1".into(),
            title: "Fix search".into(),
            detail: String::new(),
            subtitle: String::new(),
            workspace_id: String::new(),
            workspace_label: String::new(),
            tab_id: None,
            pane_id: None,
            agent: Some(agent.into()),
            agent_name: agent_name.map(Into::into),
            status: "none".into(),
            tab_number: None,
            focused: false,
            extra_terms: Vec::new(),
        })
    }

    fn row(field_positions: BTreeMap<usize, Vec<usize>>) -> Row {
        Row { item: 0, positions: Vec::new(), field_positions, tree: crate::render::Tree::None }
    }

    #[test]
    fn auto_falls_back_without_font() {
        // The detection result is cached per thread; seed it instead of
        // probing this machine's fonts.
        FONT.with(|f| f.set(Some(false)));
        assert!(!enabled(&s("auto")));
        assert!(enabled(&s("font")));
        FONT.with(|f| f.set(Some(true)));
        assert!(enabled(&s("auto")));
        assert!(!enabled(&s("none")));
        assert!(!enabled(&s("invalid")));
        clear_font_cache();
    }

    #[test]
    fn aliases_and_unknown_agents() {
        assert_eq!(logo_for(Some("Codex")), Some('\u{e1a1}'));
        assert_eq!(logo_for(Some("Claude Code")), Some('\u{e1a0}'));
        assert_eq!(logo_for(Some("cursor-agent")), logo_for(Some("cursor")));
        assert_eq!(logo_for(Some("open-code")), logo_for(Some("opencode")));
        assert_eq!(logo_for(Some("codex:model")), logo_for(Some("codex")));
        assert_eq!(logo_for(Some("unrecognised")), None);
        assert_eq!(logo_for(None), None);
    }

    #[test]
    fn logos_preserve_width_and_name_highlights() {
        let item = agent_item("codex", Some("helper"));
        let helper = item.fields.iter().position(|f| f == "helper").unwrap();
        let row = row(BTreeMap::from([(helper, vec![0, 1])]));
        let mut theme = Theme::new(None, Some(&s("237")), None);
        theme.agent_icons = true;
        for width in [24, 46, 80, 120] {
            for selected in [true, false] {
                let rendered = render_row(&theme, &item, &row, width, selected, false, None);
                assert_eq!(visible_width(&rendered) as isize, width);
            }
        }
        let rendered = render_row(&theme, &item, &row, 80, true, false, None);
        assert!(strip_ansi(&rendered).starts_with("▌ \u{e1a1} "));
        assert!(strip_ansi(&rendered).contains("@helper"));
        assert!(rendered.contains("\x1b[4m"));
        theme.agent_icons = false;
        let plain = strip_ansi(&render_row(&theme, &item, &row, 80, false, false, None));
        assert!(!plain.contains('\u{e1a1}'));
        assert!(plain.contains("@helper"));
    }

    #[test]
    fn leading_logo_replaces_vendor_label_even_on_narrow_rows() {
        let mut theme = Theme::new(None, None, None);
        theme.agent_icons = true;
        for agent in ["codex", "claude", "pi", "grok", "kimi", "unrecognised"] {
            let item = agent_item(agent, None);
            for width in [24, 46, 80] {
                let plain = strip_ansi(&render_row(&theme, &item, &row(BTreeMap::new()), width, false, false, None));
                assert_eq!(visible_width(&plain) as isize, width);
                if let Some(logo) = logo_for(Some(agent)) {
                    assert!(plain.starts_with(&format!("  {} ", logo)), "{:?}", plain);
                    assert!(!plain.contains(agent), "{:?}", plain);
                } else if width == 80 {
                    assert!(plain.contains(agent));
                }
            }
        }
        theme.agent_icons = false;
        let item = agent_item("codex", None);
        assert!(strip_ansi(&render_row(&theme, &item, &row(BTreeMap::new()), 80, false, false, None)).contains("codex"));
    }
}
