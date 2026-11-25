//! User configuration.
//!
//! Read from `$HERDR_PLUGIN_CONFIG_DIR/config.json`. Unknown keys are ignored,
//! and a broken file falls back to defaults instead of taking the bar down.
//! Values keep their JSON type where the Python release tested identity
//! (`preview is True`), so a config means exactly what it used to.

use crate::app::SCOPES;
use crate::pyjson::{self, Map, Value};
use crate::pystr;
use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct Config {
    pub preview: Value,
    pub mouse: bool,
    pub agent_icons: Value,
    pub refresh_ms: i128,
    pub workspaces: Value,
    pub colors: Map,
    pub selection_background: Value,
    pub claude_config_dirs: Vec<String>,
    pub tab_renaming_plugins: Vec<String>,
    pub auto_titles: bool,
    pub lowercase_titles: bool,
    pub tab_status: bool,
    pub running_time: bool,
    /// The filter the bar opens with: one of `SCOPES`, "all" unless set.
    pub scope: &'static str,
    /// Open with the row you are in selected, not the one you came from.
    pub select_current: bool,
    /// Let the next session in a pane take over the task title written for
    /// the one before it.
    pub follow_new_sessions: bool,
    /// List plugin actions as rows the bar can run.
    pub actions: bool,
    /// `"list"`, the flat alt-tab order, or `"tree"`: rows grouped under
    /// their workspace.
    pub layout: &'static str,
}

/// Python's `int(value)` for whatever JSON put there; None where it raises.
fn py_int(value: &Value) -> Option<i128> {
    match value {
        Value::Int(i) => Some(*i),
        Value::Bool(b) => Some(*b as i128),
        Value::BigInt(text) => Some(if text.starts_with('-') { i128::MIN } else { i128::MAX }),
        Value::Float(f) => {
            if f.is_finite() {
                Some(f.trunc() as i128)
            } else {
                None
            }
        }
        Value::Str(s) => pystr::parse_int(s),
        _ => None,
    }
}

fn strings(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::List(items)) => items
            .iter()
            .filter_map(|v| v.as_str())
            .filter(|s| !pystr::strip(s).is_empty())
            .map(|s| s.to_string())
            .collect(),
        _ => Vec::new(),
    }
}

impl Default for Config {
    fn default() -> Self {
        Config::from_values(&Map::new())
    }
}

impl Config {
    pub fn from_values(values: &Map) -> Config {
        let get = |key: &str| values.get(key);
        let or = |key: &str, default: Value| get(key).cloned().unwrap_or(default);
        let refresh_ms = match get("refresh_ms") {
            None => 900,
            Some(value) => py_int(value).map(|v| v.max(200)).unwrap_or(900),
        };
        let actions = get("actions").map(|v| v.is_true()).unwrap_or(false);
        Config {
            preview: or("preview", Value::str("auto")),
            mouse: get("mouse").map(|v| v.truthy()).unwrap_or(true),
            agent_icons: or("agent_icons", Value::str("auto")),
            refresh_ms,
            workspaces: or("workspaces", Value::str("auto")),
            colors: match get("colors") {
                Some(Value::Dict(m)) => m.clone(),
                _ => Map::new(),
            },
            selection_background: or("selection_background", Value::str("auto")),
            claude_config_dirs: strings(get("claude_config_dirs")),
            tab_renaming_plugins: strings(get("tab_renaming_plugins")),
            auto_titles: get("auto_titles").map(|v| v.is_true()).unwrap_or(true),
            lowercase_titles: get("lowercase_titles").map(|v| v.is_true()).unwrap_or(false),
            tab_status: get("tab_status").map(|v| v.is_true()).unwrap_or(false),
            running_time: get("running_time").map(|v| !v.is_false()).unwrap_or(true),
            scope: get("scope")
                .and_then(|v| v.as_str())
                .and_then(|name| SCOPES.iter().copied().find(|scope| *scope == name))
                // The action filter is empty without the action rows.
                .filter(|scope| *scope != "action" || actions)
                .unwrap_or("all"),
            select_current: get("select_current").map(|v| v.is_true()).unwrap_or(false),
            follow_new_sessions: get("follow_new_sessions").map(|v| v.is_true()).unwrap_or(false),
            actions,
            layout: if get("layout").and_then(|v| v.as_str()) == Some("tree") { "tree" } else { "list" },
        }
    }

    pub fn path() -> Option<PathBuf> {
        let directory = std::env::var_os("HERDR_PLUGIN_CONFIG_DIR")?;
        if directory.is_empty() {
            return None;
        }
        Some(PathBuf::from(directory).join("config.json"))
    }

    pub fn load() -> Config {
        let Some(path) = Config::path() else { return Config::default() };
        let Ok(data) = std::fs::read(&path) else { return Config::default() };
        let Ok(text) = String::from_utf8(data) else { return Config::default() };
        match pyjson::loads(&text) {
            Ok(Value::Dict(map)) => Config::from_values(&map),
            _ => Config::default(),
        }
    }

    /// Whether the preview is showing before anyone presses ^o.
    pub fn preview_starts_on(&self) -> bool {
        !self.preview.is_false()
    }

    /// Whether the popup is wide enough to split a preview column off it.
    pub fn fits_preview(&self, available_width: isize, chosen: bool) -> bool {
        if chosen || self.preview.is_true() || self.preview.is_false() {
            return available_width >= 60;
        }
        available_width >= 92
    }

    pub fn wants_workspaces(&self, workspace_count: usize) -> bool {
        // The tree is headed by its workspaces.
        if self.layout == "tree" {
            return workspace_count > 1;
        }
        if self.workspaces.is_true() {
            return true;
        }
        if self.workspaces.is_false() {
            return false;
        }
        workspace_count > 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with(key: &str, value: &str) -> Config {
        Config::from_values(pyjson::loads(&format!(r#"{{"{}": {}}}"#, key, value)).unwrap().as_dict().unwrap())
    }

    #[test]
    fn defaults_and_bad_values() {
        assert!(!Config::default().tab_status);
        assert!(Config::default().auto_titles);
        let pick = |config: &Config, key: &str| if key == "tab_status" { config.tab_status } else { config.auto_titles };
        for key in ["tab_status", "auto_titles"] {
            assert!(pick(&with(key, "true"), key), "{}", key);
            for value in ["false", "\"true\"", "1", "null"] {
                assert!(!pick(&with(key, value), key), "{} = {}", key, value);
            }
        }
    }

    #[test]
    fn scope_takes_the_filter_names_and_falls_back_to_all() {
        assert_eq!(Config::default().scope, "all");
        for name in SCOPES.iter().filter(|name| **name != "action") {
            assert_eq!(with("scope", &format!("\"{}\"", name)).scope, *name);
        }
        for value in ["\"agents\"", "\"Agent\"", "\"@\"", "\"\"", "1", "true", "null", "[\"agent\"]"] {
            assert_eq!(with("scope", value).scope, "all", "scope = {}", value);
        }
    }

    #[test]
    fn layout_is_list_unless_tree() {
        assert_eq!(Config::default().layout, "list");
        assert_eq!(with("layout", "\"tree\"").layout, "tree");
        for value in ["\"list\"", "\"Tree\"", "true", "1", "null"] {
            assert_eq!(with("layout", value).layout, "list", "layout = {}", value);
        }
        let tree = with("layout", "\"tree\"");
        assert!(tree.wants_workspaces(2));
        assert!(!tree.wants_workspaces(1));
        let tree_without = Config::from_values(
            pyjson::loads(r#"{"layout": "tree", "workspaces": false}"#).unwrap().as_dict().unwrap(),
        );
        assert!(tree_without.wants_workspaces(2));
    }

    #[test]
    fn select_current_takes_only_true() {
        assert!(!Config::default().select_current);
        assert!(with("select_current", "true").select_current);
        for value in ["false", "\"true\"", "1", "null"] {
            assert!(!with("select_current", value).select_current, "select_current = {}", value);
        }
    }

    #[test]
    fn follow_new_sessions_takes_only_true() {
        assert!(!Config::default().follow_new_sessions);
        assert!(with("follow_new_sessions", "true").follow_new_sessions);
        for value in ["false", "\"true\"", "1", "null"] {
            assert!(!with("follow_new_sessions", value).follow_new_sessions, "follow_new_sessions = {}", value);
        }
    }

    #[test]
    fn the_action_scope_needs_the_action_rows() {
        let config = |text: &str| Config::from_values(pyjson::loads(text).unwrap().as_dict().unwrap());
        assert_eq!(config(r#"{"scope": "action"}"#).scope, "all");
        assert_eq!(config(r#"{"scope": "action", "actions": false}"#).scope, "all");
        assert_eq!(config(r#"{"scope": "action", "actions": true}"#).scope, "action");
    }

    #[test]
    fn actions_takes_only_true() {
        assert!(!Config::default().actions);
        assert!(with("actions", "true").actions);
        for value in ["false", "\"true\"", "1", "null"] {
            assert!(!with("actions", value).actions, "actions = {}", value);
        }
    }
}
