//! Follow agent task titles while preserving names supplied by the user.

use crate::client::Result;
use crate::config::Config;
use crate::host;
use crate::pyjson::{self, py_eq, Map, Value, NULL};
use crate::pyos;
use crate::pystr;
use crate::tab_status::{Shared, TabStatus};
use crate::textutil::{sanitize, truncate};
use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const MAX_SCAN: u64 = 2 * 1024 * 1024;

/// `[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}`, whole string.
pub fn is_session_id(text: &str) -> bool {
    let groups: Vec<&str> = text.split('-').collect();
    let sizes = [8, 4, 4, 4, 12];
    groups.len() == 5
        && groups.iter().zip(sizes).all(|(g, n)| g.len() == n && g.bytes().all(|b| b.is_ascii_hexdigit()))
}

pub fn title_state_dir() -> Option<PathBuf> {
    let directory = std::env::var_os("HERDR_PLUGIN_STATE_DIR").filter(|v| !v.is_empty())?;
    let socket_path = std::env::var_os("HERDR_SOCKET_PATH").filter(|v| !v.is_empty());
    let Some(socket_path) = socket_path else { return Some(PathBuf::from(directory)) };
    let absolute = pyos::abspath(&socket_path.to_string_lossy());
    let key = crate::sha256::hexdigest(absolute.as_bytes());
    Some(PathBuf::from(directory).join("title-servers").join(&key[..24]))
}

fn title_str(value: &str) -> String {
    let cleaned = sanitize(value, " ");
    truncate(&pystr::split_whitespace(&cleaned).join(" "), 80)
}

fn title_of(value: &Value) -> String {
    match value {
        Value::Str(text) => title_str(text),
        _ => String::new(),
    }
}

fn find_from(chars: &[char], from: usize, word: &str) -> Option<usize> {
    let needle: Vec<char> = word.chars().collect();
    if chars.len() < needle.len() {
        return None;
    }
    (from..=chars.len() - needle.len()).find(|&i| chars[i..i + needle.len()] == needle[..])
}

fn starts_at(chars: &[char], at: usize, word: &str) -> bool {
    let needle: Vec<char> = word.chars().collect();
    chars.get(at..at + needle.len()) == Some(&needle[..])
}

/// `<command-name>\s*/?([^<\s]+)\s*</command-name>`: the command's group.
fn command_name(chars: &[char]) -> Option<String> {
    const OPEN: &str = "<command-name>";
    const CLOSE: &str = "</command-name>";
    let name_char = |c: char| c != '<' && !pystr::is_space(c);
    let mut from = 0;
    while let Some(start) = find_from(chars, from, OPEN) {
        from = start + 1;
        let mut at = start + OPEN.chars().count();
        while at < chars.len() && pystr::is_space(chars[at]) {
            at += 1;
        }
        let mut group_start = at;
        if at < chars.len() && chars[at] == '/' && at + 1 < chars.len() && name_char(chars[at + 1]) {
            group_start = at + 1;
        }
        let mut end = group_start;
        while end < chars.len() && name_char(chars[end]) {
            end += 1;
        }
        if end == group_start {
            continue;
        }
        let mut close = end;
        while close < chars.len() && pystr::is_space(chars[close]) {
            close += 1;
        }
        if starts_at(chars, close, CLOSE) {
            return Some(chars[group_start..end].iter().collect());
        }
    }
    None
}

/// `<command-args>(.*?)</command-args>` with DOTALL: the group.
fn command_args(chars: &[char]) -> Option<String> {
    const OPEN: &str = "<command-args>";
    let start = find_from(chars, 0, OPEN)? + OPEN.chars().count();
    let end = find_from(chars, start, "</command-args>")?;
    Some(chars[start..end].iter().collect())
}

/// The end of `<[a-z][a-z-]*>` (or `</...>` when `closing`) starting at `at`.
fn tag_end(chars: &[char], at: usize, closing: bool) -> Option<usize> {
    let mut index = at;
    if chars.get(index) != Some(&'<') {
        return None;
    }
    index += 1;
    if closing {
        if chars.get(index) != Some(&'/') {
            return None;
        }
        index += 1;
    }
    if !chars.get(index).map(|c| c.is_ascii_lowercase()).unwrap_or(false) {
        return None;
    }
    index += 1;
    while index < chars.len() && (chars[index].is_ascii_lowercase() || chars[index] == '-') {
        index += 1;
    }
    if chars.get(index) == Some(&'>') {
        Some(index + 1)
    } else {
        None
    }
}

/// `re.sub(r"<[a-z][a-z-]*>.*?</[a-z][a-z-]*>", " ", text, flags=re.S)`
fn drop_tagged(chars: &[char]) -> String {
    let mut out = String::new();
    let mut index = 0;
    'scan: while index < chars.len() {
        if let Some(open_end) = tag_end(chars, index, false) {
            for close in open_end..chars.len() {
                if let Some(close_end) = tag_end(chars, close, true) {
                    out.push(' ');
                    index = close_end;
                    continue 'scan;
                }
            }
        }
        out.push(chars[index]);
        index += 1;
    }
    out
}

fn first_line(text: &str) -> &str {
    text.split('\n').next().unwrap_or("")
}

pub fn opening(content: &Value) -> String {
    let joined;
    let content: &str = match content {
        Value::List(blocks) => {
            let texts: Vec<&str> = blocks
                .iter()
                .filter(|b| matches!(b, Value::Dict(_)) && b.get("type").as_str() == Some("text"))
                .filter_map(|b| b.get("text").as_str())
                .collect();
            joined = texts.join(" ");
            &joined
        }
        Value::Str(text) => text,
        _ => return String::new(),
    };
    let chars: Vec<char> = content.chars().collect();
    if let Some(command) = command_name(&chars) {
        let args = command_args(&chars).map(|a| first_line(pystr::strip(&a)).to_string()).unwrap_or_default();
        return title_str(&format!("{} {}", command, args));
    }
    let content = drop_tagged(&chars);
    title_str(first_line(pystr::strip(&content)))
}

/// Claude config directories to search, the server's own first.
pub fn claude_roots() -> Vec<PathBuf> {
    let first = std::env::var("CLAUDE_CONFIG_DIR").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "~/.claude".into());
    let mut roots: Vec<PathBuf> = Vec::new();
    for value in std::iter::once(first).chain(Config::load().claude_config_dirs) {
        let root = PathBuf::from(pyos::expanduser(&value));
        if !roots.contains(&root) {
            roots.push(root);
        }
    }
    roots
}

#[derive(Clone, Debug, Default)]
struct Session {
    path: Option<PathBuf>,
    offset: u64,
    identity: Option<(u64, u64)>,
    title: String,
    opening: String,
    searched: Option<Instant>,
    boundary: Vec<u8>,
}

#[cfg(unix)]
fn file_identity(file: &File) -> std::io::Result<((u64, u64), u64)> {
    use std::os::unix::fs::MetadataExt;
    let meta = file.metadata()?;
    Ok(((meta.dev(), meta.ino()), meta.size()))
}

#[cfg(windows)]
fn file_identity(file: &File) -> std::io::Result<((u64, u64), u64)> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION};
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let index = ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64;
    let size = ((info.nFileSizeHigh as u64) << 32) | info.nFileSizeLow as u64;
    Ok(((info.dwVolumeSerialNumber as u64, index), size))
}

fn read_at(file: &mut File, at: u64, size: u64) -> std::io::Result<Vec<u8>> {
    file.seek(SeekFrom::Start(at))?;
    let mut data = Vec::new();
    file.take(size).read_to_end(&mut data)?;
    Ok(data)
}

pub struct TranscriptReader {
    /// None re-reads the config on each search, so edits apply without a restart.
    pub roots: Option<Vec<PathBuf>>,
    sessions: HashMap<String, Session>,
}

impl Default for TranscriptReader {
    fn default() -> Self {
        TranscriptReader::new(None)
    }
}

impl TranscriptReader {
    pub fn new(roots: Option<Vec<PathBuf>>) -> TranscriptReader {
        TranscriptReader { roots, sessions: HashMap::new() }
    }

    pub fn retain(&mut self, live: &[String]) {
        self.sessions.retain(|key, _| live.contains(key));
    }

    fn find(&self, session_id: &str, cwd: &str) -> Option<PathBuf> {
        let roots = match &self.roots {
            Some(roots) => roots.clone(),
            None => claude_roots(),
        };
        let name = format!("{}.jsonl", session_id);
        let folder: String = cwd.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
        // Stat the expected project path in every root before listing any
        // projects; the listing covers sessions started in another directory.
        for root in &roots {
            let candidate = root.join("projects").join(&folder).join(&name);
            if std::fs::metadata(&candidate).map(|m| m.is_file()).unwrap_or(false) {
                return Some(candidate);
            }
        }
        for root in &roots {
            let Ok(entries) = std::fs::read_dir(root.join("projects")) else { continue };
            for entry in entries.flatten() {
                let directory = entry.path();
                if !std::fs::metadata(&directory).map(|m| m.is_dir()).unwrap_or(false) {
                    continue;
                }
                let found = directory.join(&name);
                if std::fs::symlink_metadata(&found).is_ok() {
                    return Some(found);
                }
            }
        }
        None
    }

    pub fn title(&mut self, session_id: &str, cwd: &str) -> String {
        if !is_session_id(session_id) {
            return String::new();
        }
        if !self.sessions.contains_key(session_id) {
            self.sessions.insert(session_id.to_string(), Session::default());
        }
        let needs_path = self.sessions[session_id].path.is_none();
        if needs_path {
            let state = self.sessions.get_mut(session_id).unwrap();
            if state.searched.map(|at| at.elapsed() < Duration::from_secs(10)).unwrap_or(false) {
                return String::new();
            }
            state.searched = Some(Instant::now());
            let found = self.find(session_id, cwd);
            let state = self.sessions.get_mut(session_id).unwrap();
            state.path = found;
            if state.path.is_none() {
                return String::new();
            }
        }
        let state = self.sessions.get_mut(session_id).unwrap();
        let read = (|| -> std::io::Result<(u64, Vec<u8>, Option<usize>)> {
            let mut handle = File::open(state.path.as_ref().unwrap())?;
            let (identity, size) = file_identity(&handle)?;
            // Inodes can be reused after unlink, and a transcript may be
            // rewritten in place. Check the bytes behind our cursor too.
            let boundary = read_at(&mut handle, state.offset.saturating_sub(64), state.offset.min(64))?;
            if state.identity != Some(identity) || size < state.offset || boundary != state.boundary {
                state.offset = 0;
                state.title.clear();
                state.opening.clear();
                state.identity = Some(identity);
            }
            let start = state.offset.max(size.saturating_sub(MAX_SCAN));
            let data = read_at(&mut handle, start, MAX_SCAN)?;
            let end = data.iter().rposition(|&b| b == b'\n');
            if let Some(end) = end {
                let next_offset = start + end as u64 + 1;
                state.boundary = read_at(&mut handle, next_offset.saturating_sub(64), next_offset.min(64))?;
            }
            Ok((start, data, end))
        })();
        let (start, data, end) = match read {
            Ok(found) => found,
            Err(_) => {
                state.path = None;
                state.offset = 0;
                state.title.clear();
                state.opening.clear();
                return String::new();
            }
        };
        if let Some(end) = end {
            let mut lines: Vec<&[u8]> = data[..end].split(|&b| b == b'\n').collect();
            if start > state.offset {
                lines.remove(0); // tail starts inside a record
            }
            state.offset = start + end as u64 + 1;
            for line in lines {
                let Ok(Value::Dict(record)) = pyjson::loads_bytes(line) else { continue };
                let kind = record.get("type").unwrap_or(&NULL);
                if kind.as_str() == Some("ai-title") {
                    let title = title_of(record.get("aiTitle").unwrap_or(&NULL));
                    if !title.is_empty() {
                        state.title = title;
                    }
                } else if kind.as_str() == Some("user") && state.opening.is_empty() {
                    let origin = record.get("origin").unwrap_or(&NULL);
                    let message = record.get("message").unwrap_or(&NULL);
                    if matches!(origin, Value::Dict(_))
                        && origin.get("kind").as_str() == Some("human")
                        && matches!(message, Value::Dict(_))
                    {
                        state.opening = opening(message.get("content"));
                    }
                }
            }
        }
        if state.title.is_empty() {
            state.opening.clone()
        } else {
            state.title.clone()
        }
    }
}

/// Case-insensitive match against an ASCII letter, as Python's re does it:
/// simple case folding, plus the letters Unicode folds onto i, k and s.
fn ci(c: char, lower: char) -> bool {
    if c.to_ascii_lowercase() == lower {
        return true;
    }
    matches!((lower, c), ('i', '\u{130}') | ('i', '\u{131}') | ('k', '\u{212A}') | ('s', '\u{17F}'))
}

fn ci_word(chars: &[char], at: usize, word: &str) -> Option<usize> {
    let mut index = at;
    for lower in word.chars() {
        let c = *chars.get(index)?;
        let ok = if lower.is_ascii_alphabetic() { ci(c, lower) } else { c == lower };
        if !ok {
            return None;
        }
        index += 1;
    }
    Some(index)
}

/// `(?:administrator: )?(?:[a-z]:\\|\\\\)[^\r\n]*\.(?:exe|com|bat|cmd)`,
/// case-insensitive, whole string.
fn is_program_path(text: &str) -> bool {
    let chars: Vec<char> = text.chars().collect();
    let letter = |c: char| c.is_ascii_alphabetic() || matches!(c, '\u{130}' | '\u{131}' | '\u{212A}' | '\u{17F}');
    let starts = [ci_word(&chars, 0, "administrator: "), Some(0)];
    for start in starts.into_iter().flatten() {
        let path_start = if chars.len() >= start + 3 && letter(chars[start]) && chars[start + 1] == ':' && chars[start + 2] == '\\' {
            Some(start + 3)
        } else if chars.len() >= start + 2 && chars[start] == '\\' && chars[start + 1] == '\\' {
            Some(start + 2)
        } else {
            None
        };
        let Some(path_start) = path_start else { continue };
        if chars.len() < path_start + 4 {
            continue;
        }
        let tail = chars.len() - 4;
        if chars[path_start..tail].iter().any(|&c| c == '\r' || c == '\n') {
            continue;
        }
        if chars[tail] != '.' {
            continue;
        }
        if ["exe", "com", "bat", "cmd"].iter().any(|ext| ci_word(&chars, tail + 1, ext).is_some()) {
            return true;
        }
    }
    false
}

fn str_or_empty(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

fn first_truthy<'a>(values: &[&'a Value]) -> &'a Value {
    values.iter().find(|v| v.truthy()).copied().unwrap_or(&NULL)
}

pub(crate) fn agent_title(agent: &Value, pane: &Value) -> String {
    let mut title = title_of(first_truthy(&[agent.get("terminal_title_stripped"), pane.get("terminal_title_stripped")]));
    let cwd = str_or_empty(first_truthy(&[agent.get("cwd"), pane.get("cwd")]));
    let folder = pyos::path_name(cwd);
    let suffix = format!(" | {}", folder);
    if !folder.is_empty() && title.ends_with(&suffix) {
        let keep = title.chars().count() - (folder.chars().count() + 3);
        title = pystr::strip(&title.chars().take(keep).collect::<String>()).to_string();
    }
    let folded = pystr::casefold(&title);
    let generic = ["", "codex", "claude", "claude code"];
    if generic.contains(&folded.as_str())
        || folded == pystr::casefold(&folder)
        || folded == pystr::casefold(cwd)
        || is_program_path(&title)
    {
        return String::new();
    }
    title
}

/// `re.sub(r"[^a-z0-9_-]+", "-", text)`
fn name_strip(text: &str) -> String {
    let mut out = String::new();
    let mut in_run = false;
    for c in text.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-' {
            out.push(c);
            in_run = false;
        } else {
            if !in_run {
                out.push('-');
            }
            in_run = true;
        }
    }
    out
}

fn strip_dash_underscore(text: &str) -> &str {
    text.trim_matches(|c| c == '-' || c == '_')
}

/// Whether an agent name is just the one derived from its folder.
///
/// `herdr agent start` requires a name, so launchers such as
/// herdr-newtab-plus fill it from the folder (`herdr-bar`, then
/// `herdr-bar-2`...). That is a default, not a name somebody chose.
pub(crate) fn default_agent_name(name: &Value, kind: &Value, cwd: &str) -> bool {
    let Some(name) = name.as_str().filter(|n| !n.is_empty()) else { return false };
    let trimmed = cwd.trim_end_matches('/');
    let mut base = strip_dash_underscore(&name_strip(&pystr::lower(&pyos::path_name(trimmed)))).to_string();
    if base.is_empty() || !base.chars().next().map(pystr::is_alpha).unwrap_or(false) {
        base = if base.is_empty() { pyjson::py_str(kind) } else { format!("{}-{}", pyjson::py_str(kind), base) };
    }
    let base: String = strip_dash_underscore(&name_strip(&base)).chars().take(32).collect();
    if base.is_empty() {
        return false;
    }
    if name == base {
        return true;
    }
    // re.fullmatch(r"(.+)(-\d{1,2})", name)
    let chars: Vec<char> = name.chars().collect();
    let n = chars.len();
    let suffix = if n >= 3 && chars[n - 2] == '-' && pystr::is_decimal(chars[n - 1]) {
        2
    } else if n >= 4 && chars[n - 3] == '-' && pystr::is_decimal(chars[n - 2]) && pystr::is_decimal(chars[n - 1]) {
        3
    } else {
        return false;
    };
    let head = &chars[..n - suffix];
    if head.contains(&'\n') {
        return false;
    }
    let digits: String = chars[n - suffix + 1..].iter().collect();
    let number = pystr::parse_int(&digits).unwrap_or(0);
    let head: String = head.iter().collect();
    let budget = 32usize.saturating_sub(suffix);
    number >= 2 && head == base.chars().take(budget).collect::<String>()
}

#[derive(Clone, Debug, PartialEq)]
pub struct Candidate {
    pub identity: Value,
    pub agent: String,
    pub suggested: String,
    pub session: Option<String>,
    pub pane: Value,
    pub terminal: Value,
    pub label: Value,
    pub cwd: Value,
}

struct Lookup<'a> {
    entries: Vec<(&'a Value, Vec<&'a Value>)>,
}

impl<'a> Lookup<'a> {
    fn find(&self, key: &Value) -> Option<&Vec<&'a Value>> {
        self.entries.iter().find(|(k, _)| py_eq(k, key)).map(|(_, v)| v)
    }
}

/// Default or plugin-owned tabs with one unambiguously identified agent.
///
/// With `follow_new_sessions`, a name this plugin wrote also passes to the
/// next agent in the same pane: a new session, or a new terminal.
pub fn candidates(snapshot: &Value, owned: &Map, follow_new_sessions: bool) -> Vec<(String, Candidate)> {
    let mut panes: Lookup = Lookup { entries: Vec::new() };
    for pane in snapshot.get("panes").list_or_empty() {
        let key = pane.get("pane_id");
        match panes.entries.iter_mut().find(|(k, _)| py_eq(k, key)) {
            Some(slot) => slot.1 = vec![pane],
            None => panes.entries.push((key, vec![pane])),
        }
    }
    let mut agents_by_tab: Lookup = Lookup { entries: Vec::new() };
    for agent in snapshot.get("agents").list_or_empty() {
        let key = agent.get("tab_id");
        match agents_by_tab.entries.iter_mut().find(|(k, _)| py_eq(k, key)) {
            Some(slot) => slot.1.push(agent),
            None => agents_by_tab.entries.push((key, vec![agent])),
        }
    }
    let empty = Value::Dict(Map::new());
    let mut positions: Vec<(Value, i64)> = Vec::new();
    let mut out: Vec<(String, Candidate)> = Vec::new();
    for tab in snapshot.get("tabs").list_or_empty() {
        let workspace = tab.as_dict().and_then(|t| t.get("workspace_id")).cloned().unwrap_or_else(|| Value::str(""));
        let position = match positions.iter_mut().find(|(k, _)| py_eq(k, &workspace)) {
            Some(slot) => {
                slot.1 += 1;
                slot.1
            }
            None => {
                positions.push((workspace, 1));
                1
            }
        };
        if tab.get("title_conflict").truthy() {
            continue;
        }
        let Some(agents) = agents_by_tab.find(tab.get("tab_id")) else { continue };
        if agents.len() != 1 {
            continue;
        }
        let agent = agents[0];
        let pane = panes.find(agent.get("pane_id")).map(|p| p[0]).unwrap_or(&empty);
        // A pane label or agent name is custom even if it happens to say
        // "claude". Never infer ownership from equality to a generated title.
        // The one exception is the folder-derived name a launcher had to pass.
        let cwd_value = first_truthy(&[agent.get("cwd"), pane.get("cwd")]);
        let cwd = str_or_empty(cwd_value);
        let name = agent.get("name");
        if !pane.truthy() || pane.get("label").truthy() || (name.truthy() && !default_agent_name(name, agent.get("agent"), cwd)) {
            continue;
        }
        let session = first_truthy(&[agent.get("agent_session"), pane.get("agent_session")]);
        let kind = agent.get("agent");
        let Some(kind_text) = kind.as_str().filter(|k| *k == "claude" || *k == "codex") else { continue };
        let suggested = agent_title(agent, pane);
        let mut session_id: Option<String> = None;
        if session.truthy() {
            if !matches!(session, Value::Dict(_)) || !py_eq(session.get("agent"), kind) || session.get("kind").as_str() != Some("id") {
                continue;
            }
            match session.get("value").as_str() {
                Some(value) if is_session_id(value) => session_id = Some(value.to_string()),
                _ => continue,
            }
        } else if suggested.is_empty() || pane.get("terminal_id").as_str().is_none_or(|t| t.is_empty()) {
            // Terminal titles are available even when Herdr has no session ID.
            // Only transcripts need that ID; terminal identity still prevents
            // a replacement pane from claiming a name we previously wrote.
            continue;
        }
        let session_value = session_id.clone().map(Value::str).unwrap_or(Value::Null);
        let identity = Value::List(vec![kind.clone(), session_value, agent.get("pane_id").clone(), pane.get("terminal_id").clone()]);
        let Some(tab_id) = tab.get("tab_id").as_str() else { continue };
        let previous = match owned.get(tab_id) {
            Some(previous @ Value::Dict(_)) => previous,
            _ => &empty,
        };
        if previous.get("yielded").truthy() {
            continue;
        }
        let label = tab.get("label");
        let unnamed = label.is_null() || label.as_str() == Some("") || label.as_str() == Some(position.to_string().as_str());
        if !unnamed {
            let old_identity = previous.get("identity");
            // Hooks may report the session ID after the first terminal title.
            let earlier = Value::List(vec![kind.clone(), Value::Null, agent.get("pane_id").clone(), pane.get("terminal_id").clone()]);
            let identified_later = session_id.is_some() && py_eq(old_identity, &earlier);
            // `/clear`, a restarted agent and a respawned terminal all keep the pane.
            let pane_id = agent.get("pane_id");
            let same_pane = follow_new_sessions
                && pane_id.as_str().is_some_and(|id| !id.is_empty())
                && old_identity.list_or_empty().get(2).is_some_and(|old| py_eq(old, pane_id));
            if (!py_eq(old_identity, &identity) && !identified_later && !same_pane) || !py_eq(previous.get("title"), label) {
                continue;
            }
        }
        let candidate = Candidate {
            identity,
            agent: kind_text.to_string(),
            suggested,
            session: session_id,
            pane: agent.get("pane_id").clone(),
            terminal: pane.get("terminal_id").clone(),
            label: label.clone(),
            cwd: if cwd_value.truthy() { cwd_value.clone() } else { Value::str("") },
        };
        match out.iter_mut().find(|(id, _)| id == tab_id) {
            Some(slot) => slot.1 = candidate,
            None => out.push((tab_id.to_string(), candidate)),
        }
    }
    out
}

pub struct AutoTitles {
    pub enabled: bool,
    pub lowercase_titles: bool,
    pub follow_new_sessions: bool,
    pub reader: TranscriptReader,
    pub owned: Map,
    pub state_dir: Option<PathBuf>,
}

impl Default for AutoTitles {
    fn default() -> Self {
        AutoTitles::new()
    }
}

impl AutoTitles {
    pub fn new() -> AutoTitles {
        let mut titles = AutoTitles {
            enabled: false,
            lowercase_titles: false,
            follow_new_sessions: false,
            reader: TranscriptReader::default(),
            owned: Map::new(),
            state_dir: title_state_dir(),
        };
        titles.configure(&Config::load());
        titles
    }

    pub fn configure(&mut self, config: &Config) {
        let transcript = std::env::var("HERDR_AUTO_TITLE_TRANSCRIPT").unwrap_or_else(|_| "true".into());
        let transcript = pystr::lower(&transcript);
        self.enabled = config.auto_titles && !["0", "false", "no", "off"].contains(&transcript.as_str());
        self.lowercase_titles = config.lowercase_titles;
        self.follow_new_sessions = config.follow_new_sessions;
    }

    /// Name tabs after their agents' tasks; errors only for local failures.
    pub fn apply(&mut self, client: &mut TabStatus, snapshot: Shared) -> Result<Shared> {
        if !self.enabled {
            return Ok(snapshot);
        }
        let Some(state_dir) = self.state_dir.clone() else { return self.apply_inner(client, snapshot, None) };
        match self.persisted_apply(client, snapshot.clone(), &state_dir) {
            Ok(result) => Ok(result),
            // Optional naming must not take down its caller. A later tick can
            // retry when permissions, disk space, or the state directory recover.
            Err(error) if !error.is_herdr() => Ok(snapshot),
            Err(error) => Err(error),
        }
    }

    fn persisted_apply(&mut self, client: &mut TabStatus, snapshot: Shared, state_dir: &Path) -> Result<Shared> {
        std::fs::create_dir_all(state_dir)?;
        let lock = host::open_append(&state_dir.join("auto-titles.lock"))?;
        if let Err(error) = host::lock(&lock, false) {
            if host::is_busy(&error) {
                return Ok(snapshot);
            }
            return Err(error.into());
        }
        let path = state_dir.join("auto-titles.json");
        self.owned = match std::fs::read(&path) {
            Ok(data) => match String::from_utf8(data).ok().map(|t| pyjson::loads(&t)) {
                Some(Ok(Value::Dict(saved))) => saved,
                _ => Map::new(),
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Map::new(),
            Err(error) => return Err(error.into()),
        };
        let before = self.owned.clone();
        // Verify write access before renaming any tabs. The same staging
        // file is committed only if ownership changes, avoiding idle writes.
        let staging = path.with_extension("tmp");
        let result = self.apply_inner(client, snapshot, Some(&staging))?;
        if self.owned != before {
            std::fs::write(&staging, pyjson::dumps(&Value::Dict(self.owned.clone()), false))?;
            std::fs::rename(&staging, &path)?;
        }
        drop(lock);
        Ok(result)
    }

    fn apply_inner(&mut self, client: &mut TabStatus, mut snapshot: Shared, staging: Option<&Path>) -> Result<Shared> {
        {
            let current = snapshot.borrow();
            let tabs = current.get("tabs").list_or_empty();
            let tab_for = |key: &str| tabs.iter().rev().find(|tab| py_eq(tab.get("tab_id"), &Value::str(key)));
            self.owned.retain(|key, value| tab_for(key).is_some() && matches!(value, Value::Dict(_)));
            let keys: Vec<String> = self.owned.keys().cloned().collect();
            for tab_id in keys {
                let tab = tab_for(&tab_id).unwrap();
                let record = self.owned.get(&tab_id).unwrap();
                if record.get("yielded").truthy() || !py_eq(record.get("title"), tab.get("label")) {
                    let mut yielded = Map::new();
                    yielded.insert("yielded", Value::Bool(true));
                    self.owned.insert(tab_id, Value::Dict(yielded));
                }
            }
        }
        let found = candidates(&snapshot.borrow(), &self.owned, self.follow_new_sessions);
        let live: Vec<String> = found.iter().filter_map(|(_, c)| c.session.clone()).collect();
        self.reader.retain(&live);
        for (tab_id, candidate) in found {
            let mut title = candidate.suggested.clone();
            if title.is_empty() && candidate.agent == "claude" {
                if let Some(session) = &candidate.session {
                    title = self.reader.title(session, str_or_empty(&candidate.cwd));
                }
            }
            if self.lowercase_titles {
                title = pystr::lower(&title);
            }
            let title_value = Value::str(title.clone());
            let record = || {
                let mut record = Map::new();
                record.insert("identity", candidate.identity.clone());
                record.insert("title", Value::str(title.clone()));
                Value::Dict(record)
            };
            if py_eq(&title_value, &candidate.label) && self.owned.contains_key(&tab_id) {
                // Bind a newly reported session even when its title is unchanged.
                self.owned.insert(tab_id.clone(), record());
            }
            if title.is_empty() || py_eq(&title_value, &candidate.label) {
                continue;
            }
            // Disk reads can take time: check names and session identity
            // again immediately before writing, including custom pane names.
            let attempt = (|| -> Result<bool> {
                snapshot = client.snapshot()?;
                let fresh = candidates(&snapshot.borrow(), &self.owned, self.follow_new_sessions);
                if fresh.iter().find(|(id, _)| *id == tab_id).map(|(_, c)| c) != Some(&candidate) {
                    return Ok(false);
                }
                if let Some(staging) = staging {
                    std::fs::write(staging, pyjson::dumps(&Value::Dict(self.owned.clone()), false))?;
                }
                client.rename_tab(&tab_id, &title)?;
                self.owned.insert(tab_id.clone(), record());
                Ok(true)
            })();
            match attempt {
                Ok(true) => {}
                Ok(false) => continue,
                // Optional naming must never prevent opening the bar.
                Err(error) if error.is_herdr() => continue,
                Err(error) => return Err(error),
            }
            if let Some(Value::List(tabs)) = snapshot.borrow_mut().as_dict_mut().and_then(|s| s.get_mut("tabs")) {
                for tab in tabs.iter_mut() {
                    if tab.get("tab_id").as_str() == Some(tab_id.as_str()) {
                        if let Value::Dict(tab) = tab {
                            tab.insert("label", title_value.clone());
                        }
                    }
                }
            }
        }
        Ok(snapshot)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_ids() {
        assert!(is_session_id("0123abcd-0123-4567-89ab-0123456789AB"));
        assert!(!is_session_id("0123abcd-0123-4567-89ab-0123456789A"));
        assert!(!is_session_id("0123abcd-0123-4567-89ab"));
    }

    #[test]
    fn openings() {
        let text = Value::str("<command-name>/review</command-name><command-args>  fix it\nmore</command-args>");
        assert_eq!(opening(&text), "review fix it");
        let text = Value::str("<x-y>drop\nme</a> keep this\nnot this");
        assert_eq!(opening(&text), "keep this");
        assert_eq!(opening(&Value::str("<command-name>/</command-name>")), "/");
    }

    #[test]
    fn program_paths() {
        assert!(is_program_path("C:\\Program Files\\PowerShell\\7\\pwsh.EXE"));
        assert!(is_program_path("Administrator: C:\\Windows\\system32\\cmd.exe"));
        assert!(is_program_path("\\\\server\\share\\x.bat"));
        assert!(!is_program_path("C:\\x.exe extra"));
    }

    #[test]
    fn default_names() {
        let kind = Value::str("claude");
        assert!(default_agent_name(&Value::str("herdr-bar"), &kind, "/src/herdr-bar/"));
        assert!(default_agent_name(&Value::str("herdr-bar-2"), &kind, "/src/herdr-bar"));
        assert!(!default_agent_name(&Value::str("herdr-bar-1"), &kind, "/src/herdr-bar"));
        assert!(default_agent_name(&Value::str("claude-1x"), &kind, "/src/1x"));
        assert!(!default_agent_name(&Value::str("mine"), &kind, "/src/herdr-bar"));
    }

    // -- ports of tests/test_auto_title.py ------------------------------------

    use crate::testutil::{env_lock, EnvVars, TempDir};

    const SESSION: &str = "12345678-1234-1234-1234-123456789abc";
    #[cfg(unix)]
    const OTHER_SESSION: &str = "aaaaaaaa-1234-1234-1234-123456789abc";

    #[cfg(unix)]
    fn json(text: &str) -> Value {
        pyjson::loads(text).expect("test json parses")
    }

    #[cfg(unix)]
    fn config(text: &str) -> Config {
        match json(text) {
            Value::Dict(map) => Config::from_values(&map),
            _ => unreachable!(),
        }
    }

    fn ai_title(title: &str) -> String {
        // json.dumps's default separators, as the Python tests wrote them.
        format!("{{\"type\": \"ai-title\", \"aiTitle\": {}}}\n", pyjson::dumps(&Value::str(title), false))
    }

    fn append(path: &Path, text: &str) {
        use std::io::Write;
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mut file = std::fs::OpenOptions::new().create(true).append(true).open(path).unwrap();
        file.write_all(text.as_bytes()).unwrap();
    }

    /// Back-date the last transcript search, as patching `time.monotonic` did.
    fn age_search(reader: &mut TranscriptReader) {
        for state in reader.sessions.values_mut() {
            state.searched = state.searched.and_then(|at| at.checked_sub(Duration::from_secs(11)));
        }
    }

    // -- TranscriptTest --

    struct Transcript {
        _temp: TempDir,
        root: PathBuf,
        path: PathBuf,
        reader: TranscriptReader,
    }

    impl Transcript {
        fn new() -> Transcript {
            let temp = TempDir::new();
            let root = temp.path().to_path_buf();
            let path = root.join("projects").join("-work-app").join(format!("{}.jsonl", SESSION));
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            let reader = TranscriptReader::new(Some(vec![root.clone()]));
            Transcript { _temp: temp, root, path, reader }
        }

        fn write(&self, records: &[&str]) {
            let text: String = records.iter().map(|r| format!("{}\n", r)).collect();
            append(&self.path, &text);
        }

        fn title(&mut self) -> String {
            self.reader.title(SESSION, "/work/app")
        }
    }

    #[test]
    fn latest_ai_title_wins_over_opening() {
        let mut t = Transcript::new();
        t.write(&[
            r#"{"type": "user", "origin": {"kind": "human"}, "message": {"content": "Fix login\nHere is context"}}"#,
            r#"{"type": "ai-title", "aiTitle": "Login repair"}"#,
        ]);
        assert_eq!(t.title(), "Login repair");
        t.write(&[r#"{"type": "ai-title", "aiTitle": "Repair OAuth callbacks"}"#]);
        assert_eq!(t.title(), "Repair OAuth callbacks");
    }

    #[test]
    fn opening_ignores_tool_output_and_uses_human_text_blocks() {
        let mut t = Transcript::new();
        t.write(&[
            r#"{"type": "user", "message": {"content": "tool output"}}"#,
            r#"{"type": "user", "origin": {"kind": "human"}, "message": {"content": [{"type": "tool_result", "content": "secret output"}, {"type": "text", "text": "<system-reminder>context</system-reminder>"}, {"type": "text", "text": "Fix login\nMore detail"}]}}"#,
        ]);
        assert_eq!(t.title(), "Fix login");
    }

    #[test]
    fn slash_command_and_arguments() {
        let mut t = Transcript::new();
        t.write(&[r#"{"type": "user", "origin": {"kind": "human"}, "message": {"content": "<command-name>/review</command-name><command-args>spec.md</command-args>"}}"#]);
        assert_eq!(t.title(), "review spec.md");
    }

    #[test]
    fn partial_line_waits_until_complete_and_bad_records_are_ignored() {
        let mut t = Transcript::new();
        std::fs::write(&t.path, b"not json\n[]\n{\"type\":\"ai-title\",\"aiTitle\":\"OAuth").unwrap();
        assert_eq!(t.title(), "");
        append(&t.path, " repair\"}\n");
        assert_eq!(t.title(), "OAuth repair");
    }

    #[test]
    fn truncated_or_replaced_file_does_not_reuse_old_title() {
        let mut t = Transcript::new();
        append(&t.path, &ai_title("Old title"));
        assert_eq!(t.title(), "Old title");
        std::fs::write(&t.path, "{}\n").unwrap();
        assert_eq!(t.title(), "");
        std::fs::remove_file(&t.path).unwrap();
        append(&t.path, &ai_title("New title"));
        assert_eq!(t.title(), "New title");
    }

    #[test]
    fn missing_transcript_retried_and_can_be_found_under_old_cwd() {
        let mut t = Transcript::new();
        assert_eq!(t.reader.title(SESSION, "/work/new"), "");
        append(&t.path, &ai_title("Moved project"));
        age_search(&mut t.reader);
        assert_eq!(t.reader.title(SESSION, "/work/new"), "Moved project");
    }

    #[test]
    fn same_inode_and_size_rewrite_is_detected() {
        let mut t = Transcript::new();
        append(&t.path, &ai_title("Old title"));
        assert_eq!(t.title(), "Old title");
        #[cfg(unix)]
        let identity = {
            use std::os::unix::fs::MetadataExt;
            std::fs::metadata(&t.path).unwrap().ino()
        };
        let size = std::fs::metadata(&t.path).unwrap().len();
        std::fs::write(&t.path, ai_title("New title")).unwrap();
        assert_eq!(std::fs::metadata(&t.path).unwrap().len(), size);
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(std::fs::metadata(&t.path).unwrap().ino(), identity);
        }
        assert_eq!(t.title(), "New title");
    }

    #[test]
    fn scan_is_bounded_and_terminal_controls_removed() {
        let mut t = Transcript::new();
        let mut big = vec![b'x'; MAX_SCAN as usize + 100];
        big.push(b'\n');
        std::fs::write(&t.path, big).unwrap();
        append(&t.path, &ai_title("\x1b[31mFix\x1b[0m\nlogin\x07"));
        assert_eq!(t.title(), "Fix login");
    }

    #[test]
    fn invalid_session_id_cannot_escape_projects() {
        let mut t = Transcript::new();
        assert_eq!(t.reader.title("../../private", "/work/app"), "");
        assert!(t.reader.sessions.is_empty());
    }

    #[test]
    fn transcript_found_in_any_config_root() {
        let t = Transcript::new();
        append(&t.path, &ai_title("Second account"));
        let empty = t.root.join("missing-root");
        let mut reader = TranscriptReader::new(Some(vec![empty.clone(), t.root.clone()]));
        assert_eq!(reader.title(SESSION, "/work/app"), "Second account");
        let mut moved = TranscriptReader::new(Some(vec![empty, t.root.clone()]));
        assert_eq!(moved.title(SESSION, "/elsewhere"), "Second account");
    }

    // -- ClaudeRootsTest --

    struct Roots {
        // Field order is drop order: restore the variables, then unlock.
        vars: EnvVars,
        _lock: std::sync::MutexGuard<'static, ()>,
        temp: TempDir,
        config: PathBuf,
    }

    impl Roots {
        fn new() -> Roots {
            let lock = env_lock();
            let temp = TempDir::new();
            let config = temp.path().join("config");
            std::fs::create_dir_all(&config).unwrap();
            let home = temp.path().join("home");
            let mut vars = EnvVars::new();
            vars.set("HERDR_PLUGIN_CONFIG_DIR", &config)
                .set("CLAUDE_CONFIG_DIR", "/srv/claude")
                .set("HOME", &home)
                .set("USERPROFILE", &home);
            Roots { vars, _lock: lock, temp, config }
        }

        fn configure(&self, dirs: &str) {
            std::fs::write(self.config.join("config.json"), format!("{{\"claude_config_dirs\": {}}}", dirs)).unwrap();
        }
    }

    fn expanded(path: &str) -> PathBuf {
        PathBuf::from(pyos::expanduser(path))
    }

    #[test]
    fn server_root_first_then_configured_roots_deduplicated() {
        let r = Roots::new();
        r.configure(r#"["~/.claude-work", "/srv/claude", "", 7, "/opt/claude"]"#);
        assert_eq!(
            claude_roots(),
            vec![PathBuf::from("/srv/claude"), expanded("~/.claude-work"), PathBuf::from("/opt/claude")]
        );
    }

    #[test]
    fn default_root_without_config() {
        let mut r = Roots::new();
        r.vars.set("CLAUDE_CONFIG_DIR", "");
        assert_eq!(claude_roots(), vec![expanded("~/.claude")]);
    }

    #[test]
    fn roots_added_later_are_searched_without_restart() {
        let r = Roots::new();
        let extra = r.temp.path().join("work");
        let path = extra.join("projects").join("-work-app").join(format!("{}.jsonl", SESSION));
        append(&path, &ai_title("Work task"));
        let mut reader = TranscriptReader::new(None);
        assert_eq!(reader.title(SESSION, "/work/app"), "");
        r.configure(&pyjson::dumps(&Value::List(vec![Value::str(extra.to_string_lossy())]), false));
        age_search(&mut reader);
        assert_eq!(reader.title(SESSION, "/work/app"), "Work task");
    }

    // -- AutoTitlesTest / NamingPersistenceTest: a fake Herdr server --

    #[cfg(unix)]
    mod fake {
        use crate::pyjson::{self, Map, Value};
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::{UnixListener, UnixStream};
        use std::path::PathBuf;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::{Arc, Mutex};

        type Edit = Box<dyn FnOnce(&mut Value) + Send>;

        pub struct State {
            pub snapshot: Value,
            /// `(tab_id, label)` per `tab.rename`, as FakeClient.calls.
            pub calls: Vec<(String, String)>,
            pub rename_error: Option<String>,
            /// `session.snapshot` requests answered.
            pub reads: usize,
            /// Change the session just before answering this read.
            pub edit_at: Option<(usize, Edit)>,
        }

        pub struct Fake {
            pub path: PathBuf,
            pub state: Arc<Mutex<State>>,
            stop: Arc<AtomicBool>,
            thread: Option<std::thread::JoinHandle<()>>,
        }

        fn result(entries: Vec<(&str, Value)>) -> Value {
            let mut inner = Map::new();
            for (key, value) in entries {
                inner.insert(key, value);
            }
            let mut out = Map::new();
            out.insert("id", Value::str("bar"));
            out.insert("result", Value::Dict(inner));
            Value::Dict(out)
        }

        fn answer(state: &Mutex<State>, request: &Value) -> Value {
            let mut state = state.lock().unwrap();
            match request.get("method").as_str().unwrap_or("") {
                "session.snapshot" => {
                    state.reads += 1;
                    if state.edit_at.as_ref().map(|(at, _)| *at == state.reads).unwrap_or(false) {
                        let (_, edit) = state.edit_at.take().unwrap();
                        edit(&mut state.snapshot);
                    }
                    result(vec![("snapshot", state.snapshot.clone())])
                }
                "plugin.list" => result(vec![("plugins", Value::List(Vec::new()))]),
                "tab.rename" => {
                    let params = request.get("params");
                    let tab_id = params.get("tab_id").as_str().unwrap_or("").to_string();
                    let label = params.get("label").as_str().unwrap_or("").to_string();
                    state.calls.push((tab_id.clone(), label.clone()));
                    if let Some(message) = state.rename_error.clone() {
                        return pyjson::loads(&format!(
                            "{{\"id\": \"bar\", \"error\": {{\"message\": {}}}}}",
                            pyjson::dumps(&Value::str(message), false)
                        ))
                        .unwrap();
                    }
                    if let Some(Value::List(tabs)) = state.snapshot.as_dict_mut().and_then(|s| s.get_mut("tabs")) {
                        for tab in tabs.iter_mut() {
                            if tab.get("tab_id").as_str() == Some(tab_id.as_str()) {
                                tab.as_dict_mut().unwrap().insert("label", Value::str(label.clone()));
                            }
                        }
                    }
                    result(vec![])
                }
                other => pyjson::loads(&format!("{{\"id\": \"bar\", \"error\": {{\"message\": \"unknown {}\"}}}}", other)).unwrap(),
            }
        }

        impl Fake {
            pub fn start(snapshot: Value) -> Fake {
                let path = crate::testutil::socket_path();
                let listener = UnixListener::bind(&path).unwrap();
                let state = Arc::new(Mutex::new(State { snapshot, calls: Vec::new(), rename_error: None, reads: 0, edit_at: None }));
                let stop = Arc::new(AtomicBool::new(false));
                let (shared, stopping) = (state.clone(), stop.clone());
                let thread = std::thread::spawn(move || {
                    for conn in listener.incoming() {
                        if stopping.load(Ordering::SeqCst) {
                            break;
                        }
                        let Ok(mut conn) = conn else { continue };
                        let mut line = String::new();
                        if BufReader::new(&conn).read_line(&mut line).is_err() {
                            continue;
                        }
                        let Ok(request) = pyjson::loads(&line) else { continue };
                        let mut reply = pyjson::dumps(&answer(&shared, &request), false);
                        reply.push('\n');
                        let _ = conn.write_all(reply.as_bytes());
                    }
                });
                Fake { path, state, stop, thread: Some(thread) }
            }
        }

        impl Drop for Fake {
            fn drop(&mut self) {
                self.stop.store(true, Ordering::SeqCst);
                let _ = UnixStream::connect(&self.path);
                if let Some(thread) = self.thread.take() {
                    let _ = thread.join();
                }
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }

    #[cfg(unix)]
    fn title_snapshot() -> Value {
        json(&format!(
            r#"{{"tabs": [{{"tab_id": "w1:t9", "workspace_id": "w1", "number": 9, "label": "1"}}],
                "panes": [{{"pane_id": "w1:p9", "tab_id": "w1:t9", "terminal_id": "term9"}}],
                "agents": [{{"pane_id": "w1:p9", "tab_id": "w1:t9", "agent": "claude", "cwd": "/work/app",
                             "agent_session": {{"agent": "claude", "kind": "id", "value": "{}"}}}}]}}"#,
            SESSION
        ))
    }

    /// `snapshot[group][0]`, as a dict.
    #[cfg(unix)]
    fn first<'a>(snapshot: &'a mut Value, group: &str) -> &'a mut Map {
        match snapshot.as_dict_mut().unwrap().get_mut(group).unwrap() {
            Value::List(items) => items[0].as_dict_mut().unwrap(),
            _ => unreachable!(),
        }
    }

    /// The fixture of AutoTitlesTest: a fake server, titles that read the
    /// transcript `set_title` writes, and the plugin's tab client.
    #[cfg(unix)]
    struct Titles {
        titles: AutoTitles,
        client: TabStatus,
        fake: fake::Fake,
        transcript: PathBuf,
        root: PathBuf,
        temp: TempDir,
        vars: EnvVars,
        _lock: std::sync::MutexGuard<'static, ()>,
    }

    #[cfg(unix)]
    impl Titles {
        fn new() -> Titles {
            let lock = env_lock();
            let temp = TempDir::new();
            let config = temp.path().join("config");
            std::fs::create_dir_all(&config).unwrap();
            let root = temp.path().join("claude");
            let transcript = root.join("projects").join("-work-app").join(format!("{}.jsonl", SESSION));
            let home = temp.path().join("home");
            let mut vars = EnvVars::new();
            vars.set("HERDR_AUTO_TITLE_TRANSCRIPT", "true")
                .set("HERDR_PLUGIN_CONFIG_DIR", &config)
                .set("CLAUDE_CONFIG_DIR", &root)
                .set("HOME", &home)
                .remove("HERDR_PLUGIN_STATE_DIR")
                .remove("HERDR_SOCKET_PATH");
            let fake = fake::Fake::start(title_snapshot());
            let herdr = crate::client::HerdrClient::new(Some(fake.path.to_string_lossy().into_owned()), 5.0, true);
            let client = TabStatus::new(crate::renaming_guard::AutomaticTabClient::new(herdr), None);
            let mut out = Titles { titles: AutoTitles::new(), client, fake, transcript, root, temp, vars, _lock: lock };
            out.restart();
            out.set_title("Repair login");
            out
        }

        /// A new `AutoTitles`, as a watcher restart makes.
        fn restart(&mut self) {
            self.titles = AutoTitles::new();
            self.titles.reader = TranscriptReader::new(Some(vec![self.root.clone()]));
        }

        /// What the transcript reader returns from now on.
        fn set_title(&self, title: &str) {
            append(&self.transcript, &ai_title(title));
        }

        fn reader_called(&self) -> bool {
            !self.titles.reader.sessions.is_empty()
        }

        fn apply(&mut self) -> Value {
            let snapshot = self.client.snapshot().unwrap();
            let result = self.titles.apply(&mut self.client, snapshot).unwrap();
            let out = result.borrow().clone();
            out
        }

        fn edit(&self, edit: impl FnOnce(&mut Value)) {
            edit(&mut self.fake.state.lock().unwrap().snapshot);
        }

        /// Replace the session and forget earlier renames, as a new FakeClient.
        fn reset(&self, snapshot: Value) {
            let mut state = self.fake.state.lock().unwrap();
            state.snapshot = snapshot;
            state.calls.clear();
        }

        fn calls(&self) -> Vec<String> {
            self.fake.state.lock().unwrap().calls.iter().map(|(_, label)| label.clone()).collect()
        }

        fn rename_calls(&self) -> Vec<(String, String)> {
            self.fake.state.lock().unwrap().calls.clone()
        }

        fn reads(&self) -> usize {
            self.fake.state.lock().unwrap().reads
        }

        fn label(&self) -> Value {
            let mut state = self.fake.state.lock().unwrap();
            first(&mut state.snapshot, "tabs").get("label").cloned().unwrap_or(Value::Null)
        }

        fn agent(&self, key: &str, value: Value) {
            self.edit(|s| first(s, "agents").insert(key, value));
        }

        fn drop_session(&self) {
            self.edit(|s| {
                first(s, "agents").remove("agent_session");
            });
        }

        fn codex(&self) {
            self.edit(|s| {
                let agent = first(s, "agents");
                agent.insert("agent", Value::str("codex"));
                if let Some(Value::Dict(session)) = agent.get_mut("agent_session") {
                    session.insert("agent", Value::str("codex"));
                }
            });
        }

        fn use_state_dir(&mut self) -> PathBuf {
            let state = self.temp.path().join("state");
            self.vars.set("HERDR_PLUGIN_STATE_DIR", &state);
            state
        }
    }

    #[cfg(unix)]
    fn session_value(value: &str) -> Value {
        json(&format!(r#"{{"agent": "claude", "kind": "id", "value": "{}"}}"#, value))
    }

    #[cfg(unix)]
    #[test]
    fn default_is_display_position_not_monotonic_tab_number() {
        let mut t = Titles::new();
        let result = t.apply();
        assert_eq!(first(&mut result.clone(), "tabs").get("label"), Some(&Value::str("Repair login")));
        assert_eq!(t.rename_calls(), vec![("w1:t9".to_string(), "Repair login".to_string())]);
    }

    #[cfg(unix)]
    #[test]
    fn lowercase_titles_cover_terminal_and_transcript_sources() {
        let mut t = Titles::new();
        t.titles.configure(&config(r#"{"lowercase_titles": true}"#));
        t.set_title("Repair ÉTÉ API");
        for source in ["claude_transcript", "claude_terminal", "codex_terminal"] {
            t.titles.owned = Map::new();
            t.reset(title_snapshot());
            if source.ends_with("terminal") {
                t.agent("terminal_title_stripped", Value::str("Repair ÉTÉ API | app"));
            }
            if source == "codex_terminal" {
                t.codex();
            }
            t.apply();
            t.apply();
            assert_eq!(t.rename_calls(), vec![("w1:t9".to_string(), "repair été api".to_string())], "{}", source);
        }
    }

    #[cfg(unix)]
    #[test]
    fn lowercase_config_changes_preserve_ownership_and_custom_names() {
        let mut t = Titles::new();
        t.apply();
        t.titles.configure(&config(r#"{"lowercase_titles": true}"#));
        t.apply();
        t.apply();
        t.titles.configure(&config(r#"{"lowercase_titles": false}"#));
        t.apply();
        t.edit(|s| first(s, "tabs").insert("label", Value::str("My API work")));
        t.titles.configure(&config(r#"{"lowercase_titles": true}"#));
        t.set_title("New task");
        t.apply();
        assert_eq!(t.calls(), vec!["Repair login", "repair login", "Repair login"]);
        assert_eq!(t.label(), Value::str("My API work"));
    }

    #[cfg(unix)]
    #[test]
    fn owned_names_follow_transcript_changes() {
        let mut t = Titles::new();
        t.apply();
        t.set_title("New task");
        t.apply();
        t.restart();
        t.apply();
        assert_eq!(t.calls().len(), 2);
        assert_eq!(t.calls().last().unwrap(), "New task");
    }

    #[cfg(unix)]
    #[test]
    fn codex_title_updates_without_transcripts_and_ignores_folder() {
        let mut t = Titles::new();
        t.codex();
        t.agent("terminal_title_stripped", Value::str("app"));
        t.apply();
        assert!(t.calls().is_empty());
        t.agent("terminal_title_stripped", Value::str("Repair login | app"));
        t.apply();
        t.apply();
        assert_eq!(t.calls().len(), 1);
        t.agent("terminal_title_stripped", Value::str("Repair OAuth | app"));
        t.apply();
        assert_eq!(t.calls().last().unwrap(), "Repair OAuth");
        assert!(!t.reader_called());
    }

    #[cfg(unix)]
    #[test]
    fn windows_console_default_title_is_not_a_task() {
        let mut t = Titles::new();
        t.codex();
        for title in [
            r"C:\Program Files\PowerShell\7\pwsh.EXE",
            r"Administrator: C:\WINDOWS\system32\cmd.exe",
            r"\\server\tools\codex.cmd",
        ] {
            t.agent("terminal_title_stripped", Value::str(title));
            t.apply();
        }
        assert!(t.calls().is_empty());
        t.agent("terminal_title_stripped", Value::str(r"Fix C:\temp\build.cmd quoting"));
        t.apply();
        assert_eq!(t.calls().last().unwrap(), r"Fix C:\temp\build.cmd quoting");
    }

    #[cfg(unix)]
    #[test]
    fn ownership_survives_restart_and_manual_rename_relinquishes_it() {
        let mut t = Titles::new();
        t.use_state_dir();
        t.restart();
        t.set_title("Initial task");
        t.apply();
        t.restart();
        t.set_title("Better task");
        t.apply();
        assert_eq!(t.calls().last().unwrap(), "Better task");
        t.edit(|s| first(s, "tabs").insert("label", Value::str("My choice")));
        t.apply();
        assert_eq!(Value::Dict(t.titles.owned.clone()), json(r#"{"w1:t9": {"yielded": true}}"#));
        assert_eq!(t.label(), Value::str("My choice"));
        assert_eq!(t.calls().len(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn terminal_titles_without_session_ids_follow_updates() {
        let mut t = Titles::new();
        for kind in ["codex", "claude"] {
            t.titles.owned = Map::new();
            t.reset(title_snapshot());
            t.drop_session();
            t.agent("agent", Value::str(kind));
            t.agent("terminal_title_stripped", Value::str("Repair login | app"));
            t.apply();
            assert_eq!(t.calls().last().unwrap(), "Repair login", "{}", kind);
            t.agent("terminal_title_stripped", Value::str("Updated task | app"));
            t.apply();
            assert_eq!(t.calls().last().unwrap(), "Updated task", "{}", kind);
        }
        assert!(!t.reader_called());
    }

    #[cfg(unix)]
    #[test]
    fn sessionless_titles_require_terminal_identity_and_preserve_custom_names() {
        let mut t = Titles::new();
        let cases = [
            ("panes", "terminal_id", Value::Null),
            ("tabs", "label", Value::str("My name")),
            ("panes", "label", Value::str("My pane")),
            ("agents", "name", Value::str("My agent")),
            ("agents", "terminal_title_stripped", Value::str("app")),
        ];
        for (group, key, value) in cases {
            t.reset(title_snapshot());
            t.drop_session();
            t.agent("terminal_title_stripped", Value::str("Task | app"));
            t.edit(|s| first(s, group).insert(key, value));
            t.apply();
            assert!(t.calls().is_empty(), "{}.{}", group, key);
        }
        assert!(!t.reader_called());
    }

    #[cfg(unix)]
    #[test]
    fn sessionless_ownership_survives_restart_and_yields_to_manual_rename() {
        let mut t = Titles::new();
        t.use_state_dir();
        t.drop_session();
        t.agent("terminal_title_stripped", Value::str("First task"));
        t.restart();
        t.apply();
        t.restart();
        t.agent("terminal_title_stripped", Value::str("Second task"));
        t.apply();
        assert_eq!(t.calls().last().unwrap(), "Second task");
        t.edit(|s| first(s, "tabs").insert("label", Value::str("Manual")));
        t.apply();
        assert_eq!(t.calls().len(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn sessionless_ownership_rejects_replacement_terminal() {
        let mut t = Titles::new();
        t.drop_session();
        t.agent("terminal_title_stripped", Value::str("First task"));
        t.apply();
        t.edit(|s| first(s, "panes").insert("terminal_id", Value::str("replacement")));
        t.agent("terminal_title_stripped", Value::str("Different task"));
        t.apply();
        assert_eq!(t.calls().len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn session_id_arriving_later_keeps_ownership() {
        let mut t = Titles::new();
        t.drop_session();
        t.agent("terminal_title_stripped", Value::str("First task"));
        t.apply();
        t.agent("agent_session", session_value(SESSION));
        t.agent("terminal_title_stripped", Value::str("Second task"));
        t.apply();
        assert_eq!(t.calls().last().unwrap(), "Second task");
        t.agent("agent_session", session_value(OTHER_SESSION));
        t.agent("terminal_title_stripped", Value::str("Different session"));
        t.apply();
        assert_eq!(t.calls().len(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn replacement_session_cannot_claim_a_previously_owned_name() {
        let mut t = Titles::new();
        t.apply();
        t.agent("agent_session", session_value(OTHER_SESSION));
        let other = t.root.join("projects").join("-work-app").join(format!("{}.jsonl", OTHER_SESSION));
        append(&other, &ai_title("Different session"));
        t.apply();
        assert_eq!(t.calls().len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn session_id_arriving_with_unchanged_title_blocks_later_replacement() {
        let mut t = Titles::new();
        t.drop_session();
        t.agent("terminal_title_stripped", Value::str("First task"));
        t.apply();
        t.agent("agent_session", session_value(SESSION));
        t.apply();
        t.agent("agent_session", session_value(OTHER_SESSION));
        t.agent("terminal_title_stripped", Value::str("Different session"));
        t.apply();
        assert_eq!(t.calls().len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn following_lets_a_new_session_in_the_pane_take_over_the_name() {
        let mut t = Titles::new();
        t.titles.configure(&config(r#"{"follow_new_sessions": true}"#));
        t.apply();
        t.agent("agent_session", session_value(OTHER_SESSION));
        let other = t.root.join("projects").join("-work-app").join(format!("{}.jsonl", OTHER_SESSION));
        append(&other, &ai_title("Different session"));
        t.apply();
        assert_eq!(t.calls(), vec!["Repair login", "Different session"]);
        // The name now belongs to the new session, even with following off.
        t.titles.configure(&config(r#"{"follow_new_sessions": false}"#));
        append(&other, &ai_title("Later task"));
        t.apply();
        assert_eq!(t.calls(), vec!["Repair login", "Different session", "Later task"]);
    }

    #[cfg(unix)]
    #[test]
    fn following_lets_a_new_terminal_in_the_pane_take_over_the_name() {
        let mut t = Titles::new();
        t.titles.configure(&config(r#"{"follow_new_sessions": true}"#));
        t.drop_session();
        t.agent("terminal_title_stripped", Value::str("First task"));
        t.apply();
        t.edit(|s| first(s, "panes").insert("terminal_id", Value::str("replacement")));
        t.agent("terminal_title_stripped", Value::str("Different task"));
        t.apply();
        assert_eq!(t.calls(), vec!["First task", "Different task"]);
    }

    #[cfg(unix)]
    #[test]
    fn following_keeps_your_names_and_other_panes_out() {
        let mut t = Titles::new();
        t.titles.configure(&config(r#"{"follow_new_sessions": true}"#));
        let other = t.root.join("projects").join("-work-app").join(format!("{}.jsonl", OTHER_SESSION));
        append(&other, &ai_title("Different session"));
        // A tab you renamed stays yours.
        t.apply();
        t.edit(|s| first(s, "tabs").insert("label", Value::str("My choice")));
        t.agent("agent_session", session_value(OTHER_SESSION));
        t.apply();
        assert_eq!(t.calls(), vec!["Repair login"]);
        assert_eq!(t.label(), Value::str("My choice"));
        // An agent in another pane does not inherit the name.
        t.titles.owned = Map::new();
        t.reset(title_snapshot());
        t.apply();
        t.edit(|s| {
            first(s, "panes").insert("pane_id", Value::str("w1:p10"));
            first(s, "agents").insert("pane_id", Value::str("w1:p10"));
        });
        t.agent("agent_session", session_value(OTHER_SESSION));
        t.apply();
        assert_eq!(t.calls(), vec!["Repair login"]);
    }

    #[cfg(unix)]
    #[test]
    fn existing_custom_names_are_preserved_on_first_open() {
        let mut t = Titles::new();
        for lowercase in [false, true] {
            t.titles.configure(&config(&format!("{{\"lowercase_titles\": {}}}", lowercase)));
            for label in ["My work", "claude", "9", "1 · app › claude › Repair login", "Repair login"] {
                t.edit(|s| first(s, "tabs").insert("label", Value::str(label)));
                t.apply();
            }
        }
        assert!(t.calls().is_empty());
        assert!(!t.reader_called());
    }

    #[cfg(unix)]
    #[test]
    fn custom_pane_and_agent_names_are_preserved() {
        let mut t = Titles::new();
        for (group, key) in [("panes", "label"), ("agents", "name")] {
            for label in ["My work", "claude"] {
                t.edit(|s| {
                    *s = title_snapshot();
                    first(s, group).insert(key, Value::str(label));
                });
                t.apply();
            }
        }
        assert!(t.calls().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn folder_derived_agent_names_are_defaults() {
        // herdr agent start needs a name, so launchers pass the folder's.
        let mut t = Titles::new();
        let folder = "app";
        for name in [folder.to_string(), format!("{}-2", folder)] {
            t.titles.owned = Map::new();
            t.reset(title_snapshot());
            t.agent("name", Value::str(name.clone()));
            t.apply();
            assert_eq!(t.rename_calls(), vec![("w1:t9".to_string(), "Repair login".to_string())], "{}", name);
        }
        for name in [format!("{}-reviewer", folder), format!("{}-2", &folder[..1]), format!("{}-1", folder)] {
            t.titles.owned = Map::new();
            t.reset(title_snapshot());
            t.agent("name", Value::str(name.clone()));
            t.apply();
            assert!(t.calls().is_empty(), "{}", name);
        }
    }

    #[cfg(unix)]
    #[test]
    fn external_default_name_does_not_reclaim_ownership() {
        let mut t = Titles::new();
        t.apply();
        t.edit(|s| first(s, "tabs").insert("label", Value::str("")));
        t.set_title("New task");
        t.apply();
        assert_eq!(t.calls().len(), 1);
        assert_eq!(t.label(), Value::str(""));
    }

    #[cfg(unix)]
    #[test]
    fn workspace_positions_count_shell_tabs_and_reset_per_workspace() {
        let mut t = Titles::new();
        t.edit(|s| {
            let Some(Value::List(tabs)) = s.as_dict_mut().unwrap().get_mut("tabs") else { unreachable!() };
            tabs.insert(0, json(r#"{"tab_id": "w2:t50", "workspace_id": "w2", "label": "shell", "number": 50}"#));
            tabs.insert(1, json(r#"{"tab_id": "w1:t8", "workspace_id": "w1", "label": "shell", "number": 8}"#));
            tabs.last_mut().unwrap().as_dict_mut().unwrap().insert("label", Value::str("2"));
        });
        t.apply();
        assert_eq!(t.calls().len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn multiple_agents_other_agents_and_missing_sessions_are_skipped() {
        let mut t = Titles::new();
        let mut cases = Vec::new();
        let mut data = title_snapshot();
        let mut extra = Value::Dict(first(&mut data, "agents").clone());
        extra.as_dict_mut().unwrap().insert("pane_id", Value::str("w1:p10"));
        if let Some(Value::List(agents)) = data.as_dict_mut().unwrap().get_mut("agents") {
            agents.push(extra);
        }
        cases.push(data);
        let mut data = title_snapshot();
        first(&mut data, "agents").insert("agent", Value::str("codex"));
        cases.push(data);
        let mut data = title_snapshot();
        first(&mut data, "agents").remove("agent_session");
        cases.push(data);
        for data in cases {
            t.edit(|s| *s = data);
            t.apply();
        }
        assert!(t.calls().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn rename_or_session_switch_during_read_is_respected() {
        let mut t = Titles::new();
        let cases = [
            ("tabs", "label", Value::str("Manual title")),
            ("panes", "label", Value::str("Plugin name")),
            ("panes", "terminal_id", Value::str("replacement")),
        ];
        for (group, key, value) in cases {
            t.edit(|s| *s = title_snapshot());
            // The read after the one `apply` hands over: the re-check that
            // follows the transcript read.
            let at = t.reads() + 2;
            t.fake.state.lock().unwrap().edit_at = Some((
                at,
                Box::new(move |s: &mut Value| {
                    first(s, group).insert(key, value);
                }),
            ));
            t.apply();
            assert!(t.fake.state.lock().unwrap().edit_at.is_none(), "{}.{}: change not made", group, key);
        }
        assert!(t.calls().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn failure_does_not_pretend_tab_was_renamed() {
        let mut t = Titles::new();
        t.fake.state.lock().unwrap().rename_error = Some("connection failed".into());
        let mut result = t.apply();
        assert_eq!(first(&mut result, "tabs").get("label"), Some(&Value::str("1")));
    }

    #[cfg(unix)]
    #[test]
    fn opt_out_does_not_read_transcripts_or_mutate_session() {
        let mut t = Titles::new();
        t.vars.set("HERDR_AUTO_TITLE_TRANSCRIPT", "false");
        t.restart();
        t.vars.set("HERDR_AUTO_TITLE_TRANSCRIPT", "true");
        t.apply();
        assert!(!t.reader_called());
        assert!(t.calls().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn config_can_disable_and_reenable_titles_without_restart() {
        let mut t = Titles::new();
        t.titles.configure(&config(r#"{"auto_titles": false}"#));
        t.apply();
        assert!(t.calls().is_empty());
        assert!(!t.reader_called());
        t.titles.configure(&config(r#"{"auto_titles": true}"#));
        t.apply();
        assert_eq!(t.calls().last().unwrap(), "Repair login");
    }

    #[cfg(unix)]
    #[test]
    fn popup_only_reads_titles_written_by_watcher() {
        let mut t = Titles::new();
        let socket = t.fake.path.to_string_lossy().into_owned();
        let popup = crate::client::HerdrClient::new(Some(socket), 5.0, true);
        let config = Config::default();
        let snapshot = t.fake.state.lock().unwrap().snapshot.clone();
        let pool = crate::app::Pool::build(&snapshot, &config);
        let mut bar = crate::app::Bar::new(popup, config, crate::mru::Recents::new(None), crate::theme::Theme::new(None, None, None));
        bar.bootstrap(Ok(pool), Vec::new()).unwrap();
        assert!(t.calls().is_empty());
        // A fresh AutoTitles reading the real roots: CLAUDE_CONFIG_DIR.
        t.titles = AutoTitles::new();
        t.apply();
        bar.refresh(true);
        assert_eq!(bar.items()[0].title, "Repair login");
        t.edit(|s| first(s, "tabs").insert("label", Value::str("My choice")));
        bar.refresh(true);
        assert_eq!(bar.items()[0].title, "My choice");
        assert_eq!(t.calls().len(), 1);
    }

    // -- NamingPersistenceTest --

    #[cfg(unix)]
    fn persisted() -> Titles {
        let mut t = Titles::new();
        t.use_state_dir();
        t.vars.set("HERDR_SOCKET_PATH", "/test/server.sock");
        t.restart();
        t.codex();
        t.agent("terminal_title_stripped", Value::str("Task title | app"));
        t
    }

    #[cfg(unix)]
    #[test]
    fn external_default_name_stays_unowned_after_restart() {
        let mut t = persisted();
        t.apply();
        t.edit(|s| first(s, "tabs").insert("label", Value::str("1")));
        t.apply();
        t.restart();
        t.apply();
        assert_eq!(t.calls().len(), 1);
        assert_eq!(t.label(), Value::str("1"));
    }

    #[cfg(unix)]
    #[test]
    fn busy_lock_returns_without_server_requests_or_mutation() {
        let mut t = persisted();
        let state_dir = t.titles.state_dir.clone().unwrap();
        assert!(state_dir.starts_with(t.temp.path().join("state").join("title-servers")));
        std::fs::create_dir_all(&state_dir).unwrap();
        let lock = host::open_append(&state_dir.join("auto-titles.lock")).unwrap();
        host::lock(&lock, true).unwrap();
        let snapshot = crate::tab_status::shared(t.fake.state.lock().unwrap().snapshot.clone());
        let reads = t.reads();
        t.titles.apply(&mut t.client, snapshot).unwrap();
        assert_eq!(t.reads(), reads);
        drop(lock);
        assert!(t.calls().is_empty());
        assert!(!state_dir.join("auto-titles.json").exists());
    }

    #[cfg(unix)]
    #[test]
    fn idle_tick_uses_supplied_snapshot_and_does_not_write_state() {
        use std::os::unix::fs::MetadataExt;
        let mut t = persisted();
        t.apply();
        let path = t.titles.state_dir.clone().unwrap().join("auto-titles.json");
        let before = std::fs::metadata(&path).unwrap();
        let snapshot = t.client.snapshot().unwrap();
        let reads = t.reads();
        t.titles.apply(&mut t.client, snapshot).unwrap();
        assert_eq!(t.reads(), reads);
        let after = std::fs::metadata(&path).unwrap();
        assert_eq!((after.mtime(), after.mtime_nsec(), after.ino()), (before.mtime(), before.mtime_nsec(), before.ino()));
        assert_eq!(t.calls().len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn state_directory_and_write_errors_skip_rename_and_recover() {
        let mut t = persisted();
        let state_dir = t.titles.state_dir.clone().unwrap();
        // The state directory cannot be made: its parent is a file.
        let blocker = t.temp.path().join("blocker");
        std::fs::write(&blocker, "").unwrap();
        t.titles.state_dir = Some(blocker.join("state"));
        t.apply();
        t.titles.state_dir = Some(state_dir.clone());
        // The staging file cannot be written: a directory is in its place.
        let staging = state_dir.join("auto-titles.tmp");
        std::fs::create_dir_all(&staging).unwrap();
        t.apply();
        assert!(t.calls().is_empty());
        std::fs::remove_dir(&staging).unwrap();
        t.apply();
        assert_eq!(t.calls().last().unwrap(), "Task title");
    }
}
