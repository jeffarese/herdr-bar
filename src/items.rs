//! Turn a Herdr session snapshot into the rows the bar shows.
//!
//! The rule is one row per agent, plus one row per tab that has no agent,
//! plus one row per workspace once a session has more than one. Named pane
//! rows are built separately for the dedicated pane filter, where duplicating
//! those higher-level rows is intentional. Plugin action rows, when the
//! config asks for them, come from Herdr's plugin list instead.

use crate::auto_title;
use crate::fuzzy::Prepared;
use crate::pyjson::{self, Value};
use crate::pyos;
use crate::pystr;
use crate::tab_status;
use crate::fxhash::FxHashMap;

pub const KIND_AGENT: &str = "agent";
pub const KIND_PANE: &str = "pane";
pub const KIND_TAB: &str = "tab";
pub const KIND_SPACE: &str = "space";
pub const KIND_ACTION: &str = "action";

/// This build's platform, as plugin manifests name it.
pub const PLATFORM: &str = if cfg!(windows) {
    "windows"
} else if cfg!(target_os = "macos") {
    "macos"
} else {
    "linux"
};

pub fn status_order(status: &str) -> i64 {
    match status {
        "blocked" => 0,
        "done" => 1,
        "working" => 2,
        "idle" => 3,
        "none" => 5,
        _ => 4,
    }
}

#[derive(Clone, Debug)]
pub struct Item {
    pub kind: &'static str,
    pub key: String,
    pub title: String,
    pub detail: String,
    pub subtitle: String,
    pub workspace_id: String,
    pub workspace_label: String,
    pub tab_id: Option<String>,
    pub pane_id: Option<String>,
    pub agent: Option<String>,
    pub agent_name: Option<String>,
    pub status: String,
    /// The tab's number as the snapshot gave it (an int, or a bool).
    pub tab_number_value: Option<Value>,
    pub tab_number: Option<i128>,
    pub focused: bool,
    pub fields: Vec<String>,
    pub prepared: Vec<Prepared>,
    pub detail_field: isize,
    /// The plugin and action ids an action row invokes.
    pub plugin_action: Option<(String, String)>,
}

pub struct ItemInit {
    pub kind: &'static str,
    pub key: String,
    pub title: String,
    pub detail: String,
    pub subtitle: String,
    pub workspace_id: String,
    pub workspace_label: String,
    pub tab_id: Option<String>,
    pub pane_id: Option<String>,
    pub agent: Option<String>,
    pub agent_name: Option<String>,
    pub status: String,
    pub tab_number: Option<Value>,
    pub focused: bool,
    pub extra_terms: Vec<String>,
}

impl Item {
    pub fn new(init: ItemInit) -> Item {
        let tab_number = init.tab_number.as_ref().and_then(|v| v.as_int());
        // Searchable fields, title first. Each query term has to match one
        // field on its own.
        let number = tab_number.map(|number| format!("#{}", number));
        let mut candidates: Vec<&str> = vec![&init.title, &init.detail, &init.subtitle, &init.workspace_label];
        candidates.extend(init.extra_terms.iter().map(String::as_str));
        candidates.push(init.agent.as_deref().unwrap_or(""));
        candidates.push(init.agent_name.as_deref().unwrap_or(""));
        candidates.push(&init.status);
        candidates.extend(number.as_deref());
        let mut seen: Vec<String> = Vec::new();
        let mut fields: Vec<String> = Vec::new();
        for candidate in candidates {
            if candidate.is_empty() {
                continue;
            }
            let key_text = pystr::lower(candidate);
            if !seen.contains(&key_text) {
                seen.push(key_text);
                fields.push(candidate.to_string());
            }
        }
        let detail_field = if !init.detail.is_empty() {
            fields.iter().position(|f| *f == init.detail).map(|p| p as isize).unwrap_or(-1)
        } else {
            -1
        };
        let prepared = fields.iter().map(|f| Prepared::new(f)).collect();
        Item {
            kind: init.kind,
            key: init.key,
            title: init.title,
            detail: init.detail,
            subtitle: init.subtitle,
            workspace_id: init.workspace_id,
            workspace_label: init.workspace_label,
            tab_id: init.tab_id,
            pane_id: init.pane_id,
            agent: init.agent,
            agent_name: init.agent_name,
            status: init.status,
            tab_number_value: init.tab_number,
            tab_number,
            focused: init.focused,
            fields,
            prepared,
            detail_field,
            plugin_action: None,
        }
    }

    pub fn status_rank(&self) -> i64 {
        status_order(&self.status)
    }
}

pub fn shorten_path(path: &str) -> String {
    if path.is_empty() {
        return String::new();
    }
    if let Some(home) = pyos::home() {
        if !home.is_empty() && path.starts_with(&home) {
            return format!("~{}", &path[home.len()..]);
        }
    }
    path.to_string()
}

/// `_clean`: a stripped string without control characters, or "".
pub fn clean(value: &Value) -> String {
    match value {
        Value::Str(text) => clean_str(text),
        _ => String::new(),
    }
}

pub fn clean_str(text: &str) -> String {
    let text = pystr::strip(text);
    if text.is_empty() {
        return String::new();
    }
    if !text.bytes().any(|b| b < b' ' && b != b'\t') {
        return text.to_string(); // nothing to drop, and already stripped
    }
    let kept: String = text.chars().filter(|&c| c >= ' ' || c == '\t').collect();
    pystr::strip(&kept).to_string()
}

fn first(values: &[&Value]) -> String {
    for value in values {
        let cleaned = clean(value);
        if !cleaned.is_empty() {
            return cleaned;
        }
    }
    String::new()
}

fn first_str(values: &[&str]) -> String {
    for value in values {
        let cleaned = clean_str(value);
        if !cleaned.is_empty() {
            return cleaned;
        }
    }
    String::new()
}

/// The tab's name without the status prefix the watcher puts on agent tabs.
fn tab_label(tab: &Value, has_agents: bool) -> String {
    let label = clean(tab.get("label"));
    if has_agents {
        clean_str(&tab_status::strip(&label))
    } else {
        label
    }
}

fn status_of(record: &Value) -> String {
    let status = clean(record.get("agent_status"));
    if status.is_empty() {
        "none".to_string()
    } else {
        status
    }
}

fn normalize(text: &str) -> String {
    pystr::casefold(&pystr::collapse_spaces(text))
}

/// Ignore the terminal title's project suffix when it adds only context.
fn summary_repeats_title(summary: &str, title: &str, cwd: &str, workspace: &str) -> bool {
    if normalize(summary) == normalize(title) {
        return true;
    }
    let Some(index) = summary.rfind(" | ") else { return false };
    let head = &summary[..index];
    let suffix = &summary[index + 3..];
    let folder = normalize(pyos::basename(cwd.trim_end_matches('/')));
    let workspace = normalize(workspace);
    let suffix = normalize(suffix);
    normalize(head) == normalize(title)
        && ((!folder.is_empty() && suffix == folder) || (!workspace.is_empty() && suffix == workspace))
}

/// A string id for a truthy JSON value, or None.
fn id_of(value: &Value) -> Option<String> {
    if value.truthy() {
        Some(pyjson::py_str(value))
    } else {
        None
    }
}

fn tab_number_of(record: &Value) -> Option<Value> {
    let number = record.get("number");
    if matches!(number, Value::Int(_) | Value::Bool(_) | Value::BigInt(_)) {
        Some(number.clone())
    } else {
        None
    }
}

/// A dict keyed by JSON values, compared the way Python compares keys.
///
/// Ids are strings in every real snapshot, and a string only ever equals
/// another string with the same text, so string keys go through a hash
/// index. Anything else (numbers, bools, null) keeps the ordered `py_eq`
/// scan, over the non-string keys alone: the answer is the same either way.
struct Lookup<'a, T> {
    entries: Vec<(&'a Value, T)>,
    strings: FxHashMap<&'a str, usize>,
    others: Vec<usize>,
}

impl<'a, T> Lookup<'a, T> {
    fn new() -> Self {
        Lookup { entries: Vec::new(), strings: FxHashMap::default(), others: Vec::new() }
    }

    fn position(&self, key: &Value) -> Option<usize> {
        match key {
            Value::Str(text) => self.strings.get(text.as_str()).copied(),
            _ => self.others.iter().copied().find(|&index| pyjson::py_eq(self.entries[index].0, key)),
        }
    }

    /// Append a new entry; a string key must not be present yet.
    fn push(&mut self, key: &'a Value, value: T) -> usize {
        let index = self.entries.len();
        match key {
            Value::Str(text) => {
                self.strings.insert(text.as_str(), index);
            }
            _ => self.others.push(index),
        }
        self.entries.push((key, value));
        index
    }

    fn insert(&mut self, key: &'a Value, value: T) {
        match self.position(key) {
            Some(index) => self.entries[index].1 = value,
            None => {
                self.push(key, value);
            }
        }
    }

    /// Every value in order, answering `get` the way `next(v for v in values
    /// if v.key == key)` does: the first one whose key is equal.
    ///
    /// Number keys are all kept, because `==` across ints and floats is not
    /// transitive and the first equal one depends on the key asked for.
    fn push_first(&mut self, key: &'a Value, value: T) {
        match key {
            Value::Str(text) if self.strings.contains_key(text.as_str()) => {}
            _ => {
                self.push(key, value);
            }
        }
    }

    fn get(&self, key: &Value) -> Option<&T> {
        self.position(key).map(|index| &self.entries[index].1)
    }

    fn get_mut_or_insert(&mut self, key: &'a Value, default: impl FnOnce() -> T) -> &mut T {
        let index = match self.position(key) {
            Some(index) => index,
            None => self.push(key, default()),
        };
        &mut self.entries[index].1
    }
}

fn workspace_labels(workspaces: &[Value]) -> Lookup<'_, String> {
    let mut labels = Lookup::new();
    for workspace in workspaces {
        let number = match workspace.as_dict().and_then(|m| m.get("number")) {
            Some(value) => pyjson::py_str(value),
            None => String::new(),
        };
        let fallback = format!("workspace {}", number);
        let label = first(&[workspace.get("label"), &Value::Str(fallback)]);
        labels.insert(workspace.get("workspace_id"), label);
    }
    labels
}

fn or_empty(value: &Value) -> String {
    if value.truthy() {
        pyjson::py_str(value)
    } else {
        String::new()
    }
}

static EMPTY: Value = Value::Null;

pub fn build_items(snapshot: &Value) -> Vec<Item> {
    let workspaces = snapshot.get("workspaces").list_or_empty();
    let tabs = snapshot.get("tabs").list_or_empty();
    let panes = snapshot.get("panes").list_or_empty();
    let agents = snapshot.get("agents").list_or_empty();
    let focused_tab_id = snapshot.get("focused_tab_id");
    let focused_pane_id = snapshot.get("focused_pane_id");

    let labels = workspace_labels(workspaces);
    let mut panes_by_id: Lookup<&Value> = Lookup::new();
    for pane in panes {
        let id = pane.get("pane_id");
        if id.truthy() {
            panes_by_id.insert(id, pane);
        }
    }
    let mut tabs_by_id: Lookup<&Value> = Lookup::new();
    for tab in tabs {
        let id = tab.get("tab_id");
        if id.truthy() {
            tabs_by_id.insert(id, tab);
        }
    }
    let mut first_pane_by_tab: Lookup<&Value> = Lookup::new();
    for pane in panes {
        first_pane_by_tab.push_first(pane.get("tab_id"), pane);
    }
    let mut agents_by_tab: Lookup<Vec<&Value>> = Lookup::new();
    for agent in agents {
        let tab_id = agent.get("tab_id");
        if tab_id.truthy() {
            agents_by_tab.get_mut_or_insert(tab_id, Vec::new).push(agent);
        }
    }

    // Each tab makes its rows from read-only lookups, so a big session
    // builds them in parallel chunks (see `par`), joined back in order.
    let mut items = crate::par::map_chunks(tabs, |tabs| {
        let mut items = Vec::new();
        for tab in tabs {
            let tab_id = tab.get("tab_id");
            if !tab_id.truthy() {
                continue;
            }
            let workspace_id_value = tab.get("workspace_id");
            let workspace_id = or_empty(workspace_id_value);
            let workspace_key = if workspace_id_value.truthy() { workspace_id_value.clone() } else { Value::str("") };
            let workspace_label = labels.get(&workspace_key).cloned().unwrap_or_default();
            let tab_number = tab_number_of(tab);
            let tab_agents: Vec<&Value> = agents_by_tab.get(tab_id).cloned().unwrap_or_default();
            let label = tab_label(tab, !tab_agents.is_empty());

            if tab_agents.is_empty() {
                let pane = first_pane_by_tab.get(tab_id).copied();
                let pane_value = pane.unwrap_or(&EMPTY);
                let cwd = shorten_path(&first(&[pane_value.get("foreground_cwd"), pane_value.get("cwd")]));
                let title = first_str(&[&label, pyos::basename(&cwd), "tab"]);
                let pane_id = pane.and_then(|p| {
                    let id = p.get("pane_id");
                    if id.is_null() {
                        None
                    } else {
                        Some(pyjson::py_str(id))
                    }
                });
                items.push(Item::new(ItemInit {
                    kind: KIND_TAB,
                    key: pyjson::py_str(tab_id),
                    title,
                    detail: String::new(),
                    subtitle: cwd,
                    workspace_id,
                    workspace_label,
                    tab_id: Some(pyjson::py_str(tab_id)),
                    pane_id,
                    agent: None,
                    agent_name: None,
                    status: status_of(tab),
                    tab_number,
                    focused: pyjson::py_eq(tab_id, focused_tab_id),
                    extra_terms: Vec::new(),
                }));
                continue;
            }

            for agent in &tab_agents {
                let pane_id = agent.get("pane_id");
                let pane = panes_by_id.get(pane_id).copied().unwrap_or(&EMPTY);
                let cwd = shorten_path(&first(&[
                    agent.get("foreground_cwd"),
                    agent.get("cwd"),
                    pane.get("foreground_cwd"),
                    pane.get("cwd"),
                ]));
                // pi wraps its session name as `π - <name> - <folder>`; show the
                // name the tab title is read from, not the whole terminal title.
                // An omo pane Herdr reports as pi has no such name, and keeps
                // the activity its terminal title shows, as an omo row does.
                let task = if agent.get("agent").as_str() == Some("pi") {
                    Value::str(auto_title::terminal_title_name(agent, pane))
                } else {
                    Value::Null
                };
                // agy leaves the shell's terminal title (`user@host: ~/dir`) in
                // place: it names no task, so it is no summary either.
                let terminal_titles = if agent.get("agent").as_str() == Some("agy") {
                    [&Value::Null, &Value::Null]
                } else {
                    [agent.get("terminal_title_stripped"), pane.get("terminal_title_stripped")]
                };
                let summary = first(&[
                    pane.get("title"),
                    agent.get("title"),
                    pane.get("label"),
                    &task,
                    terminal_titles[0],
                    terminal_titles[1],
                ]);
                let title = first_str(&[&label, &summary, pyos::basename(&cwd), "agent"]);
                let detail =
                    if summary_repeats_title(&summary, &title, &cwd, &workspace_label) { String::new() } else { summary };
                let agent_kind = first(&[agent.get("display_agent"), agent.get("agent")]);
                let agent_name = first(&[agent.get("name")]);
                let key = id_of(pane_id).unwrap_or_else(|| pyjson::py_str(tab_id));
                items.push(Item::new(ItemInit {
                    kind: KIND_AGENT,
                    key,
                    title,
                    detail,
                    subtitle: cwd,
                    workspace_id: workspace_id.clone(),
                    workspace_label: workspace_label.clone(),
                    tab_id: Some(pyjson::py_str(tab_id)),
                    pane_id: if pane_id.is_null() { None } else { Some(pyjson::py_str(pane_id)) },
                    agent: Some(agent_kind),
                    agent_name: Some(agent_name),
                    status: status_of(agent),
                    tab_number: tab_number.clone(),
                    focused: pyjson::py_eq(pane_id, focused_pane_id)
                        || (tab_agents.len() == 1 && pyjson::py_eq(tab_id, focused_tab_id)),
                    extra_terms: Vec::new(),
                }));
            }
        }
        items
    });

    if workspaces.len() > 1 {
        for workspace in workspaces {
            let workspace_id = workspace.get("workspace_id");
            if !workspace_id.truthy() {
                continue;
            }
            let worktree = workspace.get("worktree");
            let branch = clean(worktree.get("branch"));
            let active_tab = tabs_by_id.get(workspace.get("active_tab_id")).copied().unwrap_or(&EMPTY);
            let subtitle = if !branch.is_empty() {
                branch.clone()
            } else {
                let active_id = active_tab.get("tab_id");
                tab_label(active_tab, active_id.truthy() && agents_by_tab.get(active_id).is_some())
            };
            let label = labels.get(workspace_id).cloned().unwrap_or_else(|| pyjson::py_str(workspace_id));
            items.push(Item::new(ItemInit {
                kind: KIND_SPACE,
                key: pyjson::py_str(workspace_id),
                title: label.clone(),
                detail: String::new(),
                subtitle,
                workspace_id: pyjson::py_str(workspace_id),
                workspace_label: labels.get(workspace_id).cloned().unwrap_or_default(),
                tab_id: None,
                pane_id: None,
                agent: None,
                agent_name: None,
                status: status_of(workspace),
                tab_number: None,
                focused: workspace.get("focused").truthy(),
                extra_terms: vec!["space".into(), "workspace".into(), branch],
            }));
        }
    }
    items
}

/// One directly focusable row for every named pane in the snapshot.
pub fn build_pane_items(snapshot: &Value) -> Vec<Item> {
    let workspaces = snapshot.get("workspaces").list_or_empty();
    let tabs = snapshot.get("tabs").list_or_empty();
    let panes = snapshot.get("panes").list_or_empty();
    let focused_pane_id = snapshot.get("focused_pane_id");
    let labels = workspace_labels(workspaces);
    let mut tabs_by_id: Lookup<&Value> = Lookup::new();
    for tab in tabs {
        let id = tab.get("tab_id");
        if id.truthy() {
            tabs_by_id.insert(id, tab);
        }
    }

    crate::par::map_chunks(panes, |panes| {
        let mut items = Vec::new();
        for pane in panes {
            let pane_id = pane.get("pane_id");
            let tab_id = pane.get("tab_id");
            let pane_label = clean(pane.get("label"));
            if !pane_id.truthy() || !tab_id.truthy() || pane_label.is_empty() {
                continue;
            }
            let tab = tabs_by_id.get(tab_id).copied().unwrap_or(&EMPTY);
            let workspace_value = if pane.get("workspace_id").truthy() {
                pane.get("workspace_id").clone()
            } else if tab.get("workspace_id").truthy() {
                tab.get("workspace_id").clone()
            } else {
                Value::str("")
            };
            let workspace_label = labels.get(&workspace_value).cloned().unwrap_or_default();
            let tab_label = first(&[tab.get("label")]);
            let tab_number = tab_number_of(tab);
            let cwd = shorten_path(&first(&[pane.get("foreground_cwd"), pane.get("cwd")]));
            let terminal_title = first(&[pane.get("terminal_title_stripped"), pane.get("title")]);
            let title = pane_label;
            let detail = if !tab_label.is_empty() && pystr::lower(&tab_label) != pystr::lower(&title) {
                tab_label
            } else {
                String::new()
            };
            let agent = first(&[pane.get("display_agent"), pane.get("agent")]);
            items.push(Item::new(ItemInit {
                kind: KIND_PANE,
                key: format!("pane:{}", pyjson::py_str(pane_id)),
                title,
                detail,
                subtitle: cwd,
                workspace_id: pyjson::py_str(&workspace_value),
                workspace_label,
                tab_id: Some(pyjson::py_str(tab_id)),
                pane_id: Some(pyjson::py_str(pane_id)),
                agent: if agent.is_empty() { None } else { Some(agent) },
                agent_name: None,
                status: status_of(pane),
                tab_number,
                focused: pyjson::py_eq(pane_id, focused_pane_id),
                extra_terms: vec!["pane".into(), pyjson::py_str(pane_id), terminal_title],
            }));
        }
        items
    })
}

/// One row per plugin action the bar can run.
///
/// `actions` is what `plugin.action.list` returned and `enabled` the plugin
/// ids `plugin.list` reports enabled, or None when it could not say. Herdr
/// refuses actions of disabled plugins and of other platforms, so those stay
/// out; so do the bar's own open actions, since the bar is already open, and
/// actions offered only on a text selection, which the bar never has.
pub fn build_action_items(actions: &[Value], enabled: Option<&[String]>, platform: &str) -> Vec<Item> {
    let mut items = Vec::new();
    for action in actions {
        let plugin_id = clean(action.get("plugin_id"));
        let action_id = clean(action.get("action_id"));
        let title = clean(action.get("title"));
        if plugin_id.is_empty() || action_id.is_empty() || title.is_empty() {
            continue;
        }
        if enabled.is_some_and(|enabled| !enabled.contains(&plugin_id)) {
            continue;
        }
        if plugin_id == "herdr-bar" && (action_id == "open" || action_id == "open-windows") {
            continue;
        }
        // No list means every platform the plugin runs on, as in Herdr.
        if let Value::List(platforms) = action.get("platforms") {
            if !platforms.iter().any(|p| p.as_str() == Some(platform)) {
                continue;
            }
        }
        let contexts = action.get("contexts").list_or_empty();
        if !contexts.is_empty() && contexts.iter().all(|c| c.as_str() == Some("selection")) {
            continue;
        }
        let qualified = format!("{}.{}", plugin_id, action_id);
        let mut item = Item::new(ItemInit {
            kind: KIND_ACTION,
            key: format!("action:{}", qualified),
            title,
            detail: clean(action.get("description")),
            subtitle: plugin_id.clone(),
            workspace_id: String::new(),
            workspace_label: String::new(),
            tab_id: None,
            pane_id: None,
            agent: None,
            agent_name: None,
            status: KIND_ACTION.into(),
            tab_number: None,
            focused: false,
            extra_terms: vec![qualified],
        });
        item.plugin_action = Some((plugin_id, action_id));
        items.push(item);
    }
    items
}

#[cfg(test)]
mod tests {
    // Ported from the Python release's tests/test_items.py.
    use super::*;
    use crate::testutil;

    fn items() -> Vec<Item> {
        build_items(&testutil::snapshot())
    }

    fn by_kind(items: &[Item], kind: &str) -> Vec<Item> {
        items.iter().filter(|i| i.kind == kind).cloned().collect()
    }

    fn pane(items: &[Item], id: &str) -> Item {
        items.iter().find(|i| i.pane_id.as_deref() == Some(id)).cloned().expect("pane row")
    }

    /// `snapshot[list][index][key] = value`.
    fn set(snapshot: &mut Value, list: &str, index: usize, key: &str, value: Value) {
        let Some(Value::List(rows)) = snapshot.as_dict_mut().unwrap().get_mut(list) else {
            panic!("no list {list}");
        };
        rows[index].as_dict_mut().unwrap().insert(key, value);
    }

    #[test]
    fn one_row_per_agent() {
        assert_eq!(by_kind(&items(), KIND_AGENT).len(), 6);
    }

    #[test]
    fn only_tabs_without_agents_become_tab_rows() {
        let mut titles: Vec<String> = by_kind(&items(), KIND_TAB).into_iter().map(|i| i.title).collect();
        titles.sort();
        assert_eq!(titles, vec!["logs", "server"]);
    }

    #[test]
    fn workspaces_appear_when_there_is_more_than_one() {
        assert_eq!(by_kind(&items(), KIND_SPACE).len(), 2);
    }

    #[test]
    fn single_workspace_sessions_have_no_space_rows() {
        let mut snapshot = testutil::snapshot();
        if let Some(Value::List(spaces)) = snapshot.as_dict_mut().unwrap().get_mut("workspaces") {
            spaces.truncate(1);
        }
        assert!(by_kind(&build_items(&snapshot), KIND_SPACE).is_empty());
    }

    #[test]
    fn agent_rows_carry_pane_and_tab_ids() {
        let agent = items().into_iter().find(|i| i.title == "cards meaningless").unwrap();
        assert_eq!(agent.pane_id.as_deref(), Some("w1:p4"));
        assert_eq!(agent.tab_id.as_deref(), Some("w1:t4"));
        assert_eq!(agent.agent.as_deref(), Some("codex"));
        assert_eq!(agent.status, "blocked");
        assert_eq!(agent.tab_number, Some(4));
    }

    #[test]
    fn agent_rows_lead_with_the_tab_name_and_trail_the_summary() {
        let agent = pane(&items(), "w1:p2");
        assert_eq!(agent.title, "Redesign week loading screen");
        assert_eq!(agent.detail, "Start server for viewing");
        assert!(agent.fields.iter().any(|f| f == "Start server for viewing"));
    }

    #[test]
    fn summary_stands_alone_when_the_tab_has_no_name() {
        let mut snapshot = testutil::snapshot();
        set(&mut snapshot, "tabs", 1, "label", Value::str(""));
        let agent = pane(&build_items(&snapshot), "w1:p2");
        assert_eq!(agent.title, "Start server for viewing");
        assert_eq!(agent.detail, "");
    }

    #[test]
    fn summary_is_dropped_when_it_repeats_the_tab_name() {
        let agent = pane(&items(), "w1:p4");
        assert_eq!(agent.title, "cards meaningless");
        assert_eq!(agent.detail, "");
    }

    #[test]
    fn focused_pane_marks_exactly_one_row() {
        let focused: Vec<Item> = items().into_iter().filter(|i| i.focused && i.kind != KIND_SPACE).collect();
        assert_eq!(focused.len(), 1);
        assert_eq!(focused[0].pane_id.as_deref(), Some("w1:p3"));
    }

    #[test]
    fn project_suffix_does_not_repeat_title() {
        let mut snapshot = pyjson::loads(
            r#"{"workspaces": [{"workspace_id": "w1", "label": "Tools"}],
                "tabs": [{"tab_id": "t1", "workspace_id": "w1", "label": "Fix search"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1", "cwd": "/work/herdr-bar/"}],
                "agents": [{"pane_id": "p1", "tab_id": "t1", "agent": "codex"}]}"#,
        )
        .unwrap();
        for (summary, expected) in [
            ("Fix search | herdr-bar", ""),
            ("FIX  SEARCH | Tools", ""),
            ("Fix search | investigate indexing", "Fix search | investigate indexing"),
            ("Fix search results | herdr-bar", "Fix search results | herdr-bar"),
            ("Investigate indexing | herdr-bar", "Investigate indexing | herdr-bar"),
        ] {
            set(&mut snapshot, "panes", 0, "title", Value::str(summary));
            let item = by_kind(&build_items(&snapshot), KIND_AGENT).remove(0);
            assert_eq!(item.detail, expected, "summary {summary:?}");
            assert_eq!(item.title, "Fix search", "summary {summary:?}");
        }
    }

    #[test]
    fn pi_summary_drops_the_app_word_and_folder() {
        let mut snapshot = pyjson::loads(
            r#"{"workspaces": [{"workspace_id": "w1", "label": "Tools"}],
                "tabs": [{"tab_id": "t1", "workspace_id": "w1", "label": "Fix search"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1", "cwd": "/work/app"}],
                "agents": [{"pane_id": "p1", "tab_id": "t1", "agent": "pi", "cwd": "/work/app"}]}"#,
        )
        .unwrap();
        for (terminal_title, expected) in [
            ("π - Fix search - app", ""),
            ("π - Ship CLI changes - app", "Ship CLI changes"),
        ] {
            set(&mut snapshot, "agents", 0, "terminal_title_stripped", Value::str(terminal_title));
            let item = by_kind(&build_items(&snapshot), KIND_AGENT).remove(0);
            assert_eq!(item.detail, expected, "terminal title {terminal_title:?}");
            assert_eq!(item.title, "Fix search", "terminal title {terminal_title:?}");
        }
    }

    #[test]
    fn agy_rows_ignore_the_shell_terminal_title() {
        let mut snapshot = pyjson::loads(
            r#"{"workspaces": [{"workspace_id": "w1", "label": "Tools"}],
                "tabs": [{"tab_id": "t1", "workspace_id": "w1", "label": "Fix search"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1", "cwd": "/work/app",
                           "terminal_title_stripped": "me@host: ~/work/app"}],
                "agents": [{"pane_id": "p1", "tab_id": "t1", "agent": "agy", "cwd": "/work/app",
                            "terminal_title_stripped": "me@host: ~/work/app"}]}"#,
        )
        .unwrap();
        let item = by_kind(&build_items(&snapshot), KIND_AGENT).remove(0);
        assert_eq!(item.title, "Fix search");
        assert_eq!(item.detail, "");
        set(&mut snapshot, "tabs", 0, "label", Value::str(""));
        let item = by_kind(&build_items(&snapshot), KIND_AGENT).remove(0);
        assert_eq!(item.title, "app");
        assert_eq!(item.detail, "");
    }

    #[test]
    fn omo_reported_as_pi_keeps_its_activity() {
        // The tab title comes from `omo_task`; the summary stays omo's
        // activity, whichever kind Herdr reports the pane as.
        for kind in ["pi", "omo"] {
            let snapshot = pyjson::loads(&format!(
                r#"{{"workspaces": [{{"workspace_id": "w1", "label": "Tools"}}],
                    "tabs": [{{"tab_id": "t1", "workspace_id": "w1", "label": "Repair login"}}],
                    "panes": [{{"pane_id": "p1", "tab_id": "t1", "cwd": "/work/app"}}],
                    "agents": [{{"pane_id": "p1", "tab_id": "t1", "agent": "{kind}", "cwd": "/work/app",
                                 "terminal_title_stripped": "OmO - Running eval",
                                 "tokens": {{"omo_task": "Repair login"}}}}]}}"#
            ))
            .unwrap();
            let item = by_kind(&build_items(&snapshot), KIND_AGENT).remove(0);
            assert_eq!(item.detail, "OmO - Running eval", "reported as {kind}");
        }
    }

    #[test]
    fn named_agents_keep_their_name() {
        let named = items().into_iter().find(|i| i.agent_name.as_deref().is_some_and(|n| !n.is_empty())).unwrap();
        assert_eq!(named.agent_name.as_deref(), Some("battery"));
        assert!(named.fields.iter().any(|f| f == "battery"));
    }

    #[test]
    fn fields_start_with_the_title_and_have_no_blanks() {
        for item in items() {
            assert_eq!(item.fields[0], item.title);
            assert!(item.fields.iter().all(|f| !f.is_empty()), "{:?}", item.fields);
        }
    }

    #[test]
    fn status_ranking_puts_blocked_first() {
        let rank = |status: &str| items().into_iter().find(|i| i.status == status).unwrap().status_rank();
        assert!(rank("blocked") < rank("done"));
        assert!(rank("done") < rank("working"));
        assert!(rank("working") < rank("idle"));
    }

    #[test]
    fn missing_records_do_not_raise() {
        assert!(build_items(&pyjson::loads("{}").unwrap()).is_empty());
        assert!(build_items(&pyjson::loads(r#"{"tabs": [{}], "agents": [{}]}"#).unwrap()).is_empty());
    }

    #[test]
    fn pane_rows_lead_with_their_names() {
        let named = pane(&build_pane_items(&testutil::snapshot()), "w1:p7");
        assert_eq!(named.kind, KIND_PANE);
        assert_eq!(named.title, "server logs");
        assert_eq!(named.detail, "server");
        assert_eq!(named.key, "pane:w1:p7");
    }

    #[test]
    fn unnamed_panes_do_not_get_rows() {
        let mut ids: Vec<String> =
            build_pane_items(&testutil::snapshot()).into_iter().filter_map(|i| i.pane_id).collect();
        ids.sort();
        assert_eq!(ids, vec!["w1:p1", "w1:p7"]);
    }

    #[test]
    fn a_named_focused_pane_is_marked() {
        let mut snapshot = testutil::snapshot();
        snapshot.as_dict_mut().unwrap().insert("focused_pane_id", Value::str("w1:p7"));
        let focused: Vec<String> =
            build_pane_items(&snapshot).into_iter().filter(|i| i.focused).filter_map(|i| i.pane_id).collect();
        assert_eq!(focused, vec!["w1:p7"]);
    }

    #[test]
    fn control_characters_are_stripped_from_titles() {
        let mut snapshot = testutil::snapshot();
        set(&mut snapshot, "tabs", 0, "label", Value::str("ser\x1b[31mver\x07"));
        let title = build_items(&snapshot).into_iter().find(|i| i.tab_id.as_deref() == Some("w1:t1")).unwrap().title;
        assert!(!title.contains('\x1b'));
        assert!(!title.contains('\x07'));
    }

    #[test]
    fn replaces_home_with_tilde() {
        let _env = testutil::env_lock();
        let home = pyos::home().expect("home");
        assert_eq!(shorten_path(&format!("{home}/code")), "~/code");
    }

    #[test]
    fn leaves_other_paths_alone() {
        let _env = testutil::env_lock();
        assert_eq!(shorten_path("/etc/hosts"), "/etc/hosts");
    }

    #[test]
    fn handles_missing_values() {
        let _env = testutil::env_lock();
        assert_eq!(shorten_path(""), "");
    }

    fn actions() -> Vec<Value> {
        pyjson::loads(
            r#"[
                {"plugin_id": "herdr-servers", "action_id": "open", "title": "Open servers",
                 "description": "Every dev server running", "contexts": ["global"], "platforms": ["linux", "macos", "windows"]},
                {"plugin_id": "herdr-servers", "action_id": "doctor", "title": "Check servers setup"},
                {"plugin_id": "viewer", "action_id": "open-elsewhere", "title": "Open file viewer", "platforms": ["plan9"]},
                {"plugin_id": "herdr-bar", "action_id": "open", "title": "Open bar"},
                {"plugin_id": "herdr-bar", "action_id": "open-windows", "title": "Open bar", "platforms": ["windows"]},
                {"plugin_id": "herdr-bar", "action_id": "stop-titles", "title": "Stop tab watcher"},
                {"plugin_id": "herdr-swarm", "action_id": "session", "title": "Swarm session", "contexts": ["pane"]},
                {"plugin_id": "clipper", "action_id": "copy", "title": "Copy selection", "contexts": ["selection"]},
                {"plugin_id": "clipper", "action_id": "paste", "title": "Paste", "contexts": ["selection", "global"]},
                {"plugin_id": "off", "action_id": "go", "title": "Disabled thing"},
                {"plugin_id": "untitled", "action_id": "x", "title": "  "}
            ]"#,
        )
        .unwrap()
        .list_or_empty()
        .to_vec()
    }

    fn action_keys(enabled: Option<&[String]>) -> Vec<String> {
        build_action_items(&actions(), enabled, PLATFORM).into_iter().map(|item| item.key).collect()
    }

    #[test]
    fn action_rows_keep_what_herdr_would_run_from_here() {
        let mut enabled: Vec<String> = ["herdr-servers", "viewer", "herdr-bar", "herdr-swarm", "clipper", "untitled"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let expected = vec![
            "action:herdr-servers.open",
            "action:herdr-servers.doctor",
            "action:herdr-bar.stop-titles",
            "action:herdr-swarm.session",
            "action:clipper.paste",
        ];
        assert_eq!(action_keys(Some(&enabled)), expected);
        // Nothing to say which plugins are enabled: keep them all.
        let mut all = expected.clone();
        all.push("action:off.go");
        assert_eq!(action_keys(None), all);
        enabled.clear();
        assert!(action_keys(Some(&enabled)).is_empty());
    }

    #[test]
    fn an_action_row_names_its_action_and_plugin() {
        let item = build_action_items(&actions(), None, PLATFORM).remove(0);
        assert_eq!(item.kind, KIND_ACTION);
        assert_eq!(item.title, "Open servers");
        assert_eq!(item.detail, "Every dev server running");
        assert_eq!(item.subtitle, "herdr-servers");
        assert_eq!(item.plugin_action, Some(("herdr-servers".into(), "open".into())));
        assert_eq!((item.tab_id.as_deref(), item.pane_id.as_deref()), (None, None));
        assert!(item.fields.contains(&"herdr-servers.open".to_string()));
        assert!(item.fields.contains(&"action".to_string()));
    }

    #[test]
    fn session_rows_carry_no_action() {
        assert!(items().iter().all(|item| item.plugin_action.is_none()));
    }

    #[test]
    fn the_platform_is_one_herdr_names() {
        assert!(["linux", "macos", "windows"].contains(&PLATFORM));
    }
}
