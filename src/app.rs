//! The bar itself: state, key handling, and the draw loop.

use crate::age::{elapsed_seconds_many, pid_for};
use crate::client::{HerdrClient, Result};
use crate::config::Config;
use crate::fuzzy::{match_prepared, split_query, Match, Term};
use crate::fxhash::FxHashMap;
use crate::icons;
use crate::items::{build_items, build_pane_items, Item, KIND_AGENT, KIND_PANE, KIND_SPACE, KIND_TAB};
use crate::keys::{Event, KeyDecoder, MouseKind};
use crate::mru::Recents;
use crate::paint::Painter;
use crate::pystr;
use crate::pyjson::Value;
use crate::render::{self, compute_layout, scrollbar_column, Layout, Row};
use crate::term::Terminal;
use crate::textutil::{pad, sanitize};
use crate::theme::Theme;
use std::collections::{BTreeMap, HashMap};
use std::sync::OnceLock;
use std::time::Instant;

pub const SCOPES: &[&str] = &["all", "agent", "pane", "tab", "blocked"];

fn scope_sigil(text: &str) -> Option<&'static str> {
    Some(match text {
        "@" => "agent",
        "%" => "pane",
        "$" => "tab",
        "!" => "blocked",
        _ => return None,
    })
}

fn scope_chip(scope: &str) -> &'static str {
    match scope {
        "all" => "everything",
        "agent" => "@ agents",
        "pane" => "% panes",
        "tab" => "$ shells",
        "blocked" => "! needs you",
        _ => "",
    }
}

pub const TITLE_BONUS: i64 = 40;
pub const PREVIEW_DEBOUNCE: f64 = 0.07;
pub const PREVIEW_TTL: f64 = 1.5;
pub const ESC_TIMEOUT: f64 = 0.04;
pub const STATUS_TTL: f64 = 2.5;

pub const PLACEHOLDER: &str = "jump to a tab, pane or agent…    @ agents   % panes   $ shells   ! needs you";
pub const PLACEHOLDER_SHORT: &str = "jump to a tab, pane or agent…";

/// time.monotonic(): seconds on a clock that only moves forward. It starts
/// far from zero, as Python's does, so "never" (0.0) is long ago.
pub fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64() + 1_000_000.0
}

fn char_len(text: &str) -> usize {
    text.chars().count()
}

fn char_slice(text: &str, start: usize, stop: usize) -> String {
    text.chars().skip(start).take(stop.saturating_sub(start)).collect()
}

fn char_from(text: &str, start: usize) -> String {
    text.chars().skip(start).collect()
}

/// The words of a backward delete: `head.rstrip()`, cut after its last space.
fn delete_word_head(text: &str, cursor: usize) -> String {
    let head = char_slice(text, 0, cursor);
    let head = pystr::rstrip(&head);
    match head.rfind(' ') {
        Some(space) => head[..space + 1].to_string(),
        None => String::new(),
    }
}

/// `" ".join(text.split())`
fn squeeze(text: &str) -> String {
    pystr::split_whitespace(text).join(" ")
}

pub type Memo<'a> = FxHashMap<(usize, &'a str), Option<Match>>;

/// Score one row: every term must match one field, best field wins.
///
/// Positions come back keyed by the field that won the term, so a caller can
/// light up whichever of those fields it actually draws -- and terms that were
/// answered by a field nobody can see simply have nowhere to land.
///
/// `memo` is scratch shared across the rows of one pass. Whole fields repeat
/// across a session -- every agent is "claude", every other one is "working"
/// -- and scoring the same term against the same string twice cannot give a
/// different answer.
pub fn score_item<'a>(terms: &[Term], item: &'a Item, memo: &mut Memo<'a>) -> Option<(i64, BTreeMap<usize, Vec<usize>>)> {
    let mut total = 0;
    let mut found_positions: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for (term_index, term) in terms.iter().enumerate() {
        let mut best: Option<(i64, usize, &Match)> = None;
        for (index, field) in item.fields.iter().enumerate() {
            let key = (term_index, field.as_str());
            memo.entry(key).or_insert_with(|| match_prepared(term, &item.prepared[index]));
        }
        for (index, field) in item.fields.iter().enumerate() {
            let Some(found) = memo.get(&(term_index, field.as_str())).and_then(|f| f.as_ref()) else { continue };
            let value = found.score + if index == 0 { TITLE_BONUS } else { 0 };
            if best.map(|(b, _, _)| value > b).unwrap_or(true) {
                best = Some((value, index, found));
            }
        }
        let (value, index, found) = best?;
        total += value;
        if !found.positions.is_empty() {
            found_positions.entry(index).or_default().extend_from_slice(&found.positions);
        }
    }
    for marks in found_positions.values_mut() {
        marks.sort_unstable();
        marks.dedup();
    }
    Some((total, found_positions))
}

pub enum Outcome {
    Cancel,
    Confirm,
}

/// The rows one snapshot makes, before the bar takes them in.
pub struct Pool {
    workspace_count: usize,
    /// The tab and agent items, then the pane items.
    items: Vec<Item>,
    item_count: usize,
}

impl Pool {
    /// Pure: depends on the snapshot and the config alone, so it can be
    /// built on another thread.
    pub fn build(snapshot: &Value, config: &Config) -> Pool {
        let workspace_count = match snapshot.get("workspaces") {
            Value::List(list) => list.len(),
            Value::Dict(map) => map.len(),
            Value::Str(text) => pystr::len(text),
            _ => 0,
        };
        let mut items = build_items(snapshot);
        if !config.wants_workspaces(workspace_count) {
            items.retain(|item| item.kind != KIND_SPACE);
        }
        let item_count = items.len();
        items.extend(build_pane_items(snapshot));
        Pool { workspace_count, items, item_count }
    }
}

pub struct Bar {
    pub client: HerdrClient,
    pub config: Config,
    pub recents: Recents,
    pub theme: Theme,
    /// Rows point into this: the tab and agent items, then the pane items.
    pub pool: Vec<Item>,
    item_count: usize,
    pub rows: Vec<Row>,
    pub query: String,
    pub cursor: usize,
    pub scope: &'static str,
    pub selected: usize,
    pub offset: usize,
    pub preview_enabled: bool,
    pub preview_chosen: bool,
    preview_showing: bool,
    pub workspace_count: usize,
    pub current_key: Option<String>,
    pub current_title: String,
    pub status: Option<String>,
    pub status_until: f64,
    pub pending_close: Option<(String, String, usize)>,
    pub pending_rename: Option<(String, String)>,
    pub rename_text: String,
    pub rename_cursor: usize,
    /// Herdr's answer the pool was last built from (see `snapshot_since`).
    snapshot_answer: Vec<u8>,
    preview_cache: HashMap<String, (f64, Vec<String>)>,
    preview_pending: Option<(String, f64)>,
    ages: HashMap<String, Option<(f64, f64)>>,
    age_queue: Vec<String>,
    last_refresh: f64,
    dirty: bool,
    list_top: isize,
    list_height: isize,
    painter: Painter,
}

impl Bar {
    pub fn new(client: HerdrClient, config: Config, recents: Recents, mut theme: Theme) -> Bar {
        theme.agent_icons = icons::enabled(&config.agent_icons);
        let preview = config.preview_starts_on();
        let scope = config.scope;
        Bar {
            client,
            config,
            recents,
            theme,
            pool: Vec::new(),
            item_count: 0,
            rows: Vec::new(),
            query: String::new(),
            cursor: 0,
            scope,
            selected: 0,
            offset: 0,
            preview_enabled: preview,
            preview_chosen: false,
            preview_showing: preview,
            workspace_count: 0,
            current_key: None,
            current_title: String::new(),
            status: None,
            status_until: 0.0,
            pending_close: None,
            pending_rename: None,
            rename_text: String::new(),
            rename_cursor: 0,
            snapshot_answer: Vec::new(),
            preview_cache: HashMap::new(),
            preview_pending: None,
            ages: HashMap::new(),
            age_queue: Vec::new(),
            last_refresh: 0.0,
            dirty: true,
            list_top: 2,
            list_height: 10,
            painter: Painter::new(),
        }
    }

    pub fn items(&self) -> &[Item] {
        &self.pool[..self.item_count]
    }

    pub fn pane_items(&self) -> &[Item] {
        &self.pool[self.item_count..]
    }

    // -- data ---------------------------------------------------------------

    /// First load, outside the alt screen, so failures can be reported.
    ///
    /// The caller fetches the snapshot and builds its rows itself, so that
    /// work can overlap the rest of startup.
    pub fn bootstrap(&mut self, pool: Result<Pool>, answer: Vec<u8>) -> Result<()> {
        let pool = pool?;
        self.snapshot_answer = answer;
        self.install(pool, false);
        Ok(())
    }

    pub fn refresh(&mut self, force: bool) {
        match self.client.snapshot_since(&mut self.snapshot_answer) {
            Ok(Some(snapshot)) => {
                self.apply(&snapshot, force);
                crate::par::drop_aside(snapshot, self.pool.len() >= crate::par::MIN_PARALLEL);
            }
            Ok(None) => self.reapply(force),
            Err(error) => {
                self.flash(error.message);
                self.last_refresh = monotonic();
            }
        }
    }

    /// `apply` for a snapshot identical to the one the pool was built from.
    ///
    /// Building it again would give the same items, the same current row
    /// and -- since every change to the query, scope or pool already
    /// rebuilt them -- the same rows, so only what depends on the moment is
    /// redone: the refresh clock and keeping the selection on its row.
    fn reapply(&mut self, force: bool) {
        let keep = if self.rows.is_empty() || force { None } else { Some(self.selected_item().key.clone()) };
        self.last_refresh = monotonic();
        self.reselect(keep);
    }

    fn apply(&mut self, snapshot: &Value, force: bool) {
        self.install(Pool::build(snapshot, &self.config), force);
    }

    fn install(&mut self, pool: Pool, force: bool) {
        let keep = if self.rows.is_empty() { None } else { Some(self.selected_item().key.clone()) };
        self.workspace_count = pool.workspace_count;
        for item in &pool.items[..pool.item_count] {
            if item.focused && item.kind != KIND_SPACE {
                self.current_key = Some(item.key.clone());
                self.current_title = item.title.clone();
                break;
            }
        }
        self.item_count = pool.item_count;
        let old = std::mem::replace(&mut self.pool, pool.items);
        crate::par::drop_aside(old, self.pool.len() >= crate::par::MIN_PARALLEL);
        self.last_refresh = monotonic();
        self.rebuild(if force { None } else { keep });
    }

    pub fn rebuild(&mut self, keep_key: Option<String>) {
        self.rows = self.filter();
        self.reselect(keep_key);
    }

    fn reselect(&mut self, keep_key: Option<String>) {
        if let Some(keep) = keep_key.filter(|k| !k.is_empty()) {
            match self.rows.iter().position(|row| self.pool[row.item].key == keep) {
                Some(index) => self.selected = index,
                None => self.selected = 0,
            }
        }
        self.selected = if self.rows.is_empty() { 0 } else { self.selected.min(self.rows.len() - 1) };
        self.dirty = true;
    }

    fn in_scope(&self, item: &Item) -> bool {
        match self.scope {
            "agent" => item.kind == KIND_AGENT,
            "pane" => item.kind == KIND_PANE,
            "tab" => item.kind == KIND_TAB,
            "blocked" => item.status == "blocked" || item.status == "done",
            _ => true,
        }
    }

    fn filter(&self) -> Vec<Row> {
        let source: Vec<usize> = match self.scope {
            "pane" => (self.item_count..self.pool.len()).collect(),
            "all" => (0..self.pool.len()).collect(),
            _ => (0..self.item_count).collect(),
        };
        let candidates: Vec<usize> = source.into_iter().filter(|&i| self.in_scope(&self.pool[i])).collect();
        let terms = split_query(&self.query);
        if terms.is_empty() {
            return self
                .resting_order(&candidates)
                .into_iter()
                .map(|item| Row { item, positions: Vec::new(), field_positions: BTreeMap::new() })
                .collect();
        }
        let terms: Vec<Term> = terms.iter().map(|t| Term::new(t)).collect();
        let (pool, recents) = (&self.pool, &self.recents);
        // Rows score independently, so a big list is scored in chunks (see
        // `par`); each chunk keeps its own memo, which only saves work.
        let mut scored: Vec<((i64, usize, i64, usize), Row)> = crate::par::map_chunks(&candidates, |chunk| {
            let mut memo: Memo = Memo::default();
            let mut scored = Vec::new();
            for &index in chunk {
                let item = &pool[index];
                let Some((score, by_field)) = score_item(&terms, item, &mut memo) else { continue };
                let rank = recents.rank(&item.key, &item.title).unwrap_or(999);
                let positions = by_field.get(&0).cloned().unwrap_or_default();
                scored.push((
                    (-score, rank, item.status_rank(), char_len(&item.title)),
                    Row { item: index, positions, field_positions: by_field },
                ));
            }
            scored
        });
        // A stable sort on the keys alone, carrying positions instead of
        // whole rows, is the same order as sorting the rows by key.
        let mut order: Vec<usize> = (0..scored.len()).collect();
        order.sort_by_key(|&at| scored[at].0);
        let mut rows: Vec<Option<Row>> = scored.drain(..).map(|(_, row)| Some(row)).collect();
        order.into_iter().filter_map(|at| rows[at].take()).collect()
    }

    fn resting_order(&self, items: &[usize]) -> Vec<usize> {
        let mut recent: Vec<(usize, usize)> = Vec::new();
        let mut rest: Vec<usize> = Vec::new();
        let mut current: Option<usize> = None;
        for &index in items {
            let item = &self.pool[index];
            if Some(&item.key) == self.current_key.as_ref() || (self.scope == "pane" && item.kind == KIND_PANE && item.focused) {
                current = Some(index);
                continue;
            }
            match self.recents.rank(&item.key, &item.title) {
                None => rest.push(index),
                Some(rank) => recent.push((rank, index)),
            }
        }
        recent.sort_by_key(|entry| entry.0);
        rest.sort_by(|&a, &b| {
            let (a, b) = (&self.pool[a], &self.pool[b]);
            let key = |item: &Item| (item.kind == KIND_SPACE, item.status_rank());
            key(a)
                .cmp(&key(b))
                .then_with(|| a.workspace_label.cmp(&b.workspace_label))
                .then_with(|| a.tab_number.unwrap_or(1 << 30).cmp(&b.tab_number.unwrap_or(1 << 30)))
                .then_with(|| a.title.cmp(&b.title))
        });
        let mut ordered: Vec<usize> = recent.iter().map(|(_, item)| *item).collect();
        let recent_count = recent.len();
        ordered.extend(rest);
        if let Some(current) = current {
            // Never first: open-then-Enter should take you somewhere else, the
            // way alt-tab does. Right after the recents block is close enough
            // to stay findable.
            let at = ordered.len().min(recent_count.max(1));
            ordered.insert(at, current);
        }
        ordered
    }

    pub fn selected_item(&self) -> &Item {
        &self.pool[self.rows[self.selected].item]
    }

    // -- interaction --------------------------------------------------------

    pub fn flash(&mut self, message: impl Into<String>) {
        self.status = Some(message.into());
        self.status_until = monotonic() + STATUS_TTL;
        self.dirty = true;
    }

    fn move_by(&mut self, delta: isize, wrap: bool) {
        if self.rows.is_empty() {
            return;
        }
        let count = self.rows.len() as isize;
        let mut target = self.selected as isize + delta;
        if wrap && delta.abs() == 1 {
            target = target.rem_euclid(count);
        }
        self.selected = target.clamp(0, count - 1) as usize;
        self.dirty = true;
    }

    fn set_query(&mut self, query: String, cursor: Option<usize>) {
        let length = char_len(&query);
        self.cursor = cursor.map(|c| c.min(length)).unwrap_or(length);
        self.query = query;
        self.selected = 0;
        self.offset = 0;
        self.rebuild(None);
    }

    fn cycle_scope(&mut self, step: isize) {
        let index = SCOPES.iter().position(|s| *s == self.scope).unwrap_or(0) as isize;
        self.scope = SCOPES[(index + step).rem_euclid(SCOPES.len() as isize) as usize];
        self.selected = 0;
        self.rebuild(None);
    }

    pub fn insert(&mut self, text: &str, pasted: bool) {
        let mut text = sanitize(text, " ");
        if pasted {
            text = squeeze(&text);
        }
        if text.is_empty() || (self.query.is_empty() && pystr::strip(&text).is_empty()) {
            return;
        }
        if self.query.is_empty() && char_len(&text) == 1 {
            if let Some(scope) = scope_sigil(&text) {
                // A leading sigil is a filter, not a character: "@" scopes the
                // list to agents instead of searching for a literal at sign.
                self.scope = scope;
                self.selected = 0;
                self.rebuild(None);
                return;
            }
        }
        let query = format!("{}{}{}", char_slice(&self.query, 0, self.cursor), text, char_from(&self.query, self.cursor));
        let cursor = self.cursor + char_len(&text);
        self.set_query(query, Some(cursor));
    }

    fn backspace(&mut self) {
        if self.query.is_empty() {
            if self.scope != "all" {
                self.scope = "all";
                self.rebuild(None);
                return;
            }
            // Nothing left to unwind, and this is the key labelled "delete" on
            // a Mac keyboard: offer to close the row instead of doing nothing.
            self.request_close();
            return;
        }
        if self.cursor == 0 {
            return;
        }
        let query = format!("{}{}", char_slice(&self.query, 0, self.cursor - 1), char_from(&self.query, self.cursor));
        self.set_query(query, Some(self.cursor - 1));
    }

    fn delete_word(&mut self) {
        if self.cursor == 0 {
            return;
        }
        let head = delete_word_head(&self.query, self.cursor);
        let cursor = char_len(&head);
        let query = format!("{}{}", head, char_from(&self.query, self.cursor));
        self.set_query(query, Some(cursor));
    }

    pub fn handle_key(&mut self, name: &str) -> Option<Outcome> {
        match name {
            "esc" | "ctrl+c" | "ctrl+g" | "ctrl+q" => return Some(Outcome::Cancel),
            "enter" => return if self.rows.is_empty() { None } else { Some(Outcome::Confirm) },
            "up" | "ctrl+p" | "ctrl+k" => self.move_by(-1, true),
            "down" | "ctrl+n" | "ctrl+j" => self.move_by(1, true),
            "tab" => self.cycle_scope(1),
            "shift+tab" => self.cycle_scope(-1),
            "pgup" | "alt+v" => self.move_by(-self.page(), false),
            "pgdn" | "ctrl+v" => self.move_by(self.page(), false),
            "home" | "ctrl+home" | "ctrl+a" => {
                self.cursor = 0;
                self.dirty = true;
            }
            "end" | "ctrl+end" | "ctrl+e" => {
                self.cursor = char_len(&self.query);
                self.dirty = true;
            }
            "left" => {
                self.cursor = self.cursor.saturating_sub(1);
                self.dirty = true;
            }
            "right" => {
                self.cursor = (self.cursor + 1).min(char_len(&self.query));
                self.dirty = true;
            }
            "backspace" => self.backspace(),
            "delete" => self.request_close(),
            "ctrl+d" => {
                // Forward delete lives here now that ⌦ closes tabs.
                if self.cursor < char_len(&self.query) {
                    let query = format!("{}{}", char_slice(&self.query, 0, self.cursor), char_from(&self.query, self.cursor + 1));
                    self.set_query(query, Some(self.cursor));
                }
            }
            "ctrl+u" => {
                // Start over: the filter the bar opened with, not necessarily all.
                self.scope = self.config.scope;
                self.set_query(String::new(), None);
            }
            "ctrl+w" => self.delete_word(),
            "ctrl+o" => {
                // Flip what is on screen, not a hidden wish: in a popup too
                // narrow for "auto" the preview is off even though it is
                // enabled, and one press should bring it up.
                self.preview_enabled = !self.preview_showing;
                self.preview_chosen = true;
                self.dirty = true;
            }
            "ctrl+r" => self.request_rename(),
            _ => {}
        }
        None
    }

    fn page(&self) -> isize {
        (self.list_height - 1).max(1)
    }

    // -- renaming -----------------------------------------------------------

    /// Open an inline editor for the tab behind the selected row.
    fn request_rename(&mut self) {
        if self.rows.is_empty() {
            return;
        }
        let item = self.selected_item();
        let tab_id = item.tab_id.clone().filter(|t| !t.is_empty());
        if item.kind == KIND_PANE || item.kind == KIND_SPACE || tab_id.is_none() {
            self.flash("only tabs and agents can be renamed");
            return;
        }
        self.pending_rename = Some((tab_id.unwrap(), item.title.clone()));
        self.rename_text.clear();
        self.rename_cursor = 0;
        self.dirty = true;
    }

    fn cancel_rename(&mut self) {
        self.pending_rename = None;
        self.rename_text.clear();
        self.rename_cursor = 0;
        self.dirty = true;
    }

    fn set_rename_text(&mut self, text: String, cursor: Option<usize>) {
        let length = char_len(&text);
        self.rename_cursor = cursor.map(|c| c.min(length)).unwrap_or(length);
        self.rename_text = text;
        self.dirty = true;
    }

    fn insert_rename(&mut self, text: &str, pasted: bool) {
        let mut text = sanitize(text, " ");
        if pasted {
            text = squeeze(&text);
        }
        if text.is_empty() {
            return;
        }
        let joined = format!(
            "{}{}{}",
            char_slice(&self.rename_text, 0, self.rename_cursor),
            text,
            char_from(&self.rename_text, self.rename_cursor)
        );
        let cursor = self.rename_cursor + char_len(&text);
        self.set_rename_text(joined, Some(cursor));
    }

    fn rename_delete_word(&mut self) {
        if self.rename_cursor == 0 {
            return;
        }
        let head = delete_word_head(&self.rename_text, self.rename_cursor);
        let cursor = char_len(&head);
        let text = format!("{}{}", head, char_from(&self.rename_text, self.rename_cursor));
        self.set_rename_text(text, Some(cursor));
    }

    fn confirm_rename(&mut self) {
        let Some((tab_id, old_title)) = self.pending_rename.clone() else { return };
        let label = pystr::strip(&self.rename_text).to_string();
        if label.is_empty() || label == old_title {
            self.cancel_rename();
            self.flash("name unchanged");
            return;
        }
        if let Err(error) = self.client.rename_tab(&tab_id, &label) {
            self.flash(error.message);
            return;
        }
        self.cancel_rename();
        self.refresh(false);
        self.flash(format!("renamed to {}", label));
    }

    /// The rename editor owns input until it is saved or cancelled.
    fn handle_rename(&mut self, event: &Event) -> Option<Outcome> {
        let name = match event {
            Event::Text(text) => {
                self.insert_rename(text, false);
                return None;
            }
            Event::Paste(text) => {
                self.insert_rename(text, true);
                return None;
            }
            Event::Key(name) => name.as_str(),
            Event::Mouse { .. } => return None,
        };
        match name {
            "esc" | "ctrl+g" => self.cancel_rename(),
            "ctrl+c" | "ctrl+q" => {
                self.cancel_rename();
                return Some(Outcome::Cancel);
            }
            "enter" => self.confirm_rename(),
            "home" | "ctrl+home" | "ctrl+a" => {
                self.rename_cursor = 0;
                self.dirty = true;
            }
            "end" | "ctrl+end" | "ctrl+e" => {
                self.rename_cursor = char_len(&self.rename_text);
                self.dirty = true;
            }
            "left" => {
                self.rename_cursor = self.rename_cursor.saturating_sub(1);
                self.dirty = true;
            }
            "right" => {
                self.rename_cursor = (self.rename_cursor + 1).min(char_len(&self.rename_text));
                self.dirty = true;
            }
            "backspace" | "ctrl+h" => {
                if self.rename_cursor > 0 {
                    let text = format!(
                        "{}{}",
                        char_slice(&self.rename_text, 0, self.rename_cursor - 1),
                        char_from(&self.rename_text, self.rename_cursor)
                    );
                    self.set_rename_text(text, Some(self.rename_cursor - 1));
                }
            }
            "delete" | "ctrl+d" => {
                if self.rename_cursor < char_len(&self.rename_text) {
                    let text = format!(
                        "{}{}",
                        char_slice(&self.rename_text, 0, self.rename_cursor),
                        char_from(&self.rename_text, self.rename_cursor + 1)
                    );
                    self.set_rename_text(text, Some(self.rename_cursor));
                }
            }
            "ctrl+u" => self.set_rename_text(String::new(), None),
            "ctrl+w" => self.rename_delete_word(),
            _ => {}
        }
        None
    }

    // -- closing ------------------------------------------------------------

    /// Whether backspace currently arms a close rather than erasing.
    fn backspace_closes(&self) -> bool {
        self.query.is_empty() && self.scope == "all"
    }

    /// Arm the confirmation for the selected row's tab.
    ///
    /// Rows are per agent, so a tab running two of them shows up twice; the
    /// confirmation says how many are going, because the tab takes them all.
    fn request_close(&mut self) {
        if self.rows.is_empty() {
            return;
        }
        let item = self.selected_item();
        let tab_id = item.tab_id.clone().filter(|t| !t.is_empty());
        if item.kind == KIND_PANE || item.kind == KIND_SPACE || tab_id.is_none() {
            self.flash("only tabs and agents can be closed");
            return;
        }
        let tab_id = tab_id.unwrap();
        let agents = self.items().iter().filter(|o| o.kind == KIND_AGENT && o.tab_id.as_deref() == Some(tab_id.as_str())).count();
        self.pending_close = Some((tab_id, item.title.clone(), agents));
        self.dirty = true;
    }

    fn confirm_close(&mut self) {
        let Some((tab_id, title, _)) = self.pending_close.take() else { return };
        if let Err(error) = self.client.close_tab(&tab_id) {
            self.flash(error.message);
            return;
        }
        self.refresh(true);
        self.flash(format!("closed {}", title));
    }

    /// An armed close owns the keyboard: every key answers it, once.
    ///
    /// Deliberately not the delete keys themselves -- they repeat when held,
    /// and a repeat should never be read as a yes.
    fn handle_confirm(&mut self, event: &Event) -> Option<Outcome> {
        match event {
            Event::Key(name) if name == "enter" => {
                self.confirm_close();
                return None;
            }
            Event::Key(name) if name == "ctrl+c" || name == "ctrl+q" => {
                // The panic exit stays the panic exit.
                self.pending_close = None;
                return Some(Outcome::Cancel);
            }
            Event::Text(text) if text == "y" || text == "Y" => {
                self.confirm_close();
                return None;
            }
            _ => {}
        }
        self.pending_close = None;
        self.dirty = true;
        None
    }

    fn handle_mouse(&mut self, kind: MouseKind, y: i128) -> Option<Outcome> {
        match kind {
            MouseKind::WheelUp => {
                self.move_by(-3, false);
                return None;
            }
            MouseKind::WheelDown => {
                self.move_by(3, false);
                return None;
            }
            MouseKind::Release => return None,
            MouseKind::Press => {}
        }
        let row_index = y - 1 - self.list_top as i128;
        if row_index < 0 || row_index >= self.list_height as i128 {
            return None;
        }
        let target = self.offset as i128 + row_index;
        if target >= self.rows.len() as i128 {
            return None;
        }
        if target == self.selected as i128 {
            return Some(Outcome::Confirm);
        }
        self.selected = target as usize;
        self.dirty = true;
        None
    }

    // -- preview ------------------------------------------------------------

    fn preview_lines(&mut self, item: Option<usize>) -> (Vec<String>, bool) {
        let Some(index) = item else { return (Vec::new(), false) };
        let item = &self.pool[index];
        let Some(pane_id) = item.pane_id.clone().filter(|p| !p.is_empty()) else { return (Vec::new(), false) };
        if item.kind == KIND_SPACE {
            return (Vec::new(), false);
        }
        let cached = self.preview_cache.get(&pane_id);
        let now = monotonic();
        if let Some((taken, lines)) = cached {
            if now - taken < PREVIEW_TTL {
                return (lines.clone(), false);
            }
        }
        let lines = cached.map(|(_, lines)| lines.clone()).unwrap_or_default();
        if self.preview_pending.as_ref().map(|(p, _)| *p != pane_id).unwrap_or(true) {
            self.preview_pending = Some((pane_id, now + PREVIEW_DEBOUNCE));
        }
        (lines, true)
    }

    fn pump_preview(&mut self, height: isize) {
        let Some((pane_id, due)) = self.preview_pending.clone() else { return };
        if monotonic() < due {
            return;
        }
        self.preview_pending = None;
        let text = self.client.read_pane(&pane_id, height.max(4) as i64, "visible").unwrap_or_default();
        let mut lines: Vec<String> = pystr::splitlines(&text).iter().map(|line| sanitize(line, " ")).collect();
        while lines.last().map(|l| pystr::strip(l).is_empty()).unwrap_or(false) {
            lines.pop();
        }
        self.preview_cache.insert(pane_id, (monotonic(), lines));
        self.dirty = true;
    }

    // -- running time -------------------------------------------------------

    /// How long the row's process has been up, queueing a first reading.
    ///
    /// A start time never moves, so each pane is read once and then ticked
    /// locally -- the list stays live without paying for it every refresh.
    fn age_of(&mut self, index: usize) -> Option<f64> {
        let item = &self.pool[index];
        let pane_id = item.pane_id.as_deref().filter(|p| !p.is_empty())?;
        if item.kind == KIND_SPACE || !self.config.running_time {
            // Disabled: never enqueue, so pump_ages never runs ps/process_info.
            return None;
        }
        match self.ages.get(pane_id) {
            None => {
                if !self.age_queue.iter().any(|p| p == pane_id) {
                    self.age_queue.push(pane_id.to_string());
                }
                None
            }
            Some(None) => None,
            Some(Some((taken, seconds))) => Some(seconds + (monotonic() - taken)),
        }
    }

    /// Date every queued pane in one pass.
    ///
    /// Each pane still costs its own `pane.process_info`, in queue order, but
    /// the process table is read once for all of them: a screenful of new
    /// rows waits on one `ps` rather than one each, so input waits on at
    /// most one too.
    fn pump_ages(&mut self) {
        let mut panes: Vec<String> = Vec::new();
        for pane_id in std::mem::take(&mut self.age_queue) {
            if !self.ages.contains_key(&pane_id) && !panes.contains(&pane_id) {
                panes.push(pane_id);
            }
        }
        if panes.is_empty() {
            return;
        }
        let pids: Vec<Option<Value>> =
            self.client.process_infos(&panes).into_iter().map(|info| info.ok().and_then(|info| pid_for(&info))).collect();
        let readings = elapsed_seconds_many(&pids);
        for (pane_id, seconds) in panes.into_iter().zip(readings) {
            // A pane that cannot be dated is remembered as such: one failed
            // reading is enough, and the row simply shows no time.
            self.ages.insert(pane_id, seconds.map(|s| (monotonic(), s)));
        }
        self.dirty = true;
    }

    // -- drawing ------------------------------------------------------------

    pub fn draw(&mut self, terminal: &mut Terminal) {
        let (width, height) = terminal.size();
        let (width, height) = (width as isize, height as isize);
        let rows = self.compose(width, height);
        let payload = self.painter.frame(&rows, width.max(0) as usize, height.max(0) as usize);
        if !payload.is_empty() {
            // A frame with nothing to say says nothing: no bytes, no syscall.
            terminal.write(&payload);
            terminal.flush();
        }
        self.dirty = false;
    }

    /// The rows of one frame at this size, before painting.
    fn compose(&mut self, width: isize, height: isize) -> Vec<String> {
        let preview_on = self.preview_enabled && self.config.fits_preview((width - 2).max(0), self.preview_chosen);
        self.preview_showing = preview_on;
        let layout = compute_layout(width, height, preview_on);
        self.list_top = layout.list_top;
        self.list_height = layout.list_height;
        self.scroll_into_view(layout.list_height);

        let mut rows: Vec<String> = Vec::new();
        let chip = scope_chip(self.scope);
        let placeholder = if layout.inner_width >= 62 { PLACEHOLDER } else { PLACEHOLDER_SHORT };
        if let Some((_, old_title)) = &self.pending_rename {
            rows.push(format!(
                " {}",
                render::render_input(
                    &self.theme,
                    &self.rename_text,
                    self.rename_cursor,
                    layout.inner_width,
                    &format!("new name for “{}”", old_title),
                    "⏎ save · esc keep",
                    "rename ❯",
                )
            ));
        } else {
            rows.push(format!(
                " {}",
                render::render_input(&self.theme, &self.query, self.cursor, layout.inner_width, placeholder, chip, render::PROMPT)
            ));
        }
        if layout.rules {
            rows.push(format!(" {}", render::render_rule(&self.theme, layout.inner_width)));
        }

        let selected = if self.rows.is_empty() { None } else { Some(self.rows[self.selected].item) };
        let mut preview_body: Vec<String> = Vec::new();
        if layout.preview_width != 0 {
            let (lines, loading) = self.preview_lines(selected);
            preview_body = render::render_preview(
                &self.theme,
                selected.map(|i| &self.pool[i]),
                &lines,
                layout.preview_width,
                layout.list_height,
                loading,
            );
        }

        let list_body = self.list_rows(&layout);
        for index in 0..layout.list_height.max(0) as usize {
            let left = list_body.get(index).map(|s| s.as_str()).unwrap_or("");
            if layout.preview_width != 0 {
                let right = preview_body.get(index).map(|s| s.as_str()).unwrap_or("");
                let left = pad(left, layout.list_width);
                let separator = self.theme.paint("unknown", render::SEPARATOR);
                rows.push(format!(" {}{}{}", left, separator, right));
            } else {
                rows.push(format!(" {}", left));
            }
        }

        if layout.footer {
            if layout.rules {
                rows.push(format!(" {}", render::render_rule(&self.theme, layout.inner_width)));
            }
            rows.push(format!(" {}", self.footer(layout.inner_width)));
        }
        rows
    }

    /// Forget what is on screen, so the next draw repaints all of it.
    pub fn invalidate(&mut self) {
        self.painter.reset();
        self.dirty = true;
    }

    fn scroll_into_view(&mut self, height: isize) {
        if self.rows.is_empty() {
            self.offset = 0;
            return;
        }
        let (selected, mut offset) = (self.selected as isize, self.offset as isize);
        if selected < offset {
            offset = selected;
        } else if selected >= offset + height {
            offset = selected - height + 1;
        }
        self.offset = offset.min((self.rows.len() as isize - height).max(0)).max(0) as usize;
    }

    fn list_rows(&mut self, layout: &Layout) -> Vec<String> {
        if self.rows.is_empty() {
            let filter = if self.scope != "all" { scope_chip(self.scope) } else { "" };
            let empty = render::render_empty_state(&self.theme, layout.list_width, &self.query, !self.items().is_empty(), filter);
            let top = ((layout.list_height - empty.len() as isize).max(0) / 2) as usize;
            let mut out = vec![String::new(); top];
            out.extend(empty);
            return out;
        }
        let marks = scrollbar_column(self.rows.len() as isize, layout.list_height, self.offset as isize, layout.list_height);
        let content_width = layout.list_width - if marks.is_empty() { 0 } else { 1 };
        let show_workspace = self.workspace_count > 1;
        let mut out = Vec::new();
        for index in 0..layout.list_height.max(0) {
            let position = self.offset + index as usize;
            let mut body = if position >= self.rows.len() {
                String::new()
            } else {
                let item_index = self.rows[position].item;
                let age = self.age_of(item_index);
                render::render_row(
                    &self.theme,
                    &self.pool[item_index],
                    &self.rows[position],
                    content_width,
                    position == self.selected,
                    show_workspace,
                    age,
                )
            };
            if !marks.is_empty() {
                let mark = marks.get(&index).copied().unwrap_or(" ");
                body = format!("{}{}", pad(&body, content_width), self.theme.paint("unknown", mark));
            }
            out.push(body);
        }
        out
    }

    fn footer(&self, width: isize) -> String {
        if let Some((_, title, agents)) = &self.pending_close {
            return render::render_confirm(&self.theme, width, title, *agents);
        }
        if let Some(status) = &self.status {
            if !status.is_empty() && monotonic() < self.status_until {
                return self.theme.paint("accent", status);
            }
        }
        if self.pending_rename.is_some() {
            return render::render_footer(&self.theme, width, &[("⏎", "save"), ("esc", "keep"), ("^u", "clear")], "renaming");
        }
        let close_key = if self.backspace_closes() { "⌫" } else { "⌦" };
        let mut hints: Vec<(&str, &str)> = vec![
            ("↑↓", "move"),
            ("⏎", "jump"),
            ("⇥", "scope"),
            // Backspace only reaches the close once there is nothing left for
            // it to erase, so the footer names whichever key works right now.
            (close_key, "close"),
            ("^r", "rename"),
            ("^o", "preview"),
            ("esc", "quit"),
        ];
        if width < 52 {
            hints = vec![("↑↓", ""), ("⏎", ""), ("esc", "")];
        }
        let total = match self.scope {
            "pane" => self.pane_items().len(),
            "all" => self.pool.len(),
            _ => self.items().len(),
        };
        let shown = self.rows.len();
        let counter = if shown != total { format!("{}/{}", shown, total) } else { total.to_string() };
        render::render_footer(&self.theme, width, &hints, &counter)
    }

    // -- main loop ----------------------------------------------------------

    pub fn run(&mut self, terminal: &mut Terminal) -> Option<usize> {
        let mut decoder = KeyDecoder::new();
        self.draw(terminal);
        let mut escape_deadline: Option<f64> = None;
        loop {
            let timeout = self.next_timeout(escape_deadline);
            if terminal.wait(timeout) {
                let data = terminal.read();
                if data.is_empty() {
                    return None;
                }
                let events = decoder.feed(&data);
                let outcome = self.consume(&events);
                escape_deadline = if decoder.pending_escape() { Some(monotonic() + ESC_TIMEOUT) } else { None };
                match outcome {
                    Some(Outcome::Cancel) => return None,
                    Some(Outcome::Confirm) => return if self.rows.is_empty() { None } else { Some(self.rows[self.selected].item) },
                    None => {}
                }
            } else if let Some(deadline) = escape_deadline {
                if monotonic() >= deadline {
                    escape_deadline = None;
                    let events = decoder.flush();
                    if let Some(Outcome::Cancel) = self.consume(&events) {
                        return None;
                    }
                }
            }

            if terminal.take_resize() {
                self.invalidate();
            }
            let now = monotonic();
            if now - self.last_refresh >= self.config.refresh_ms as f64 / 1000.0 {
                self.refresh(false);
            }
            self.pump_preview(self.list_height);
            self.pump_ages();
            if self.status.as_deref().map(|s| !s.is_empty()).unwrap_or(false) && now >= self.status_until {
                self.status = None;
                self.dirty = true;
            }
            if self.dirty {
                self.draw(terminal);
            }
        }
    }

    fn consume(&mut self, events: &[Event]) -> Option<Outcome> {
        let mut outcome: Option<Outcome> = None;
        for event in events {
            if self.pending_close.is_some() {
                if let Some(found) = self.handle_confirm(event) {
                    outcome = Some(found);
                }
                if matches!(outcome, Some(Outcome::Cancel)) {
                    return outcome;
                }
                continue;
            }
            if self.pending_rename.is_some() {
                if let Some(found) = self.handle_rename(event) {
                    outcome = Some(found);
                }
                if matches!(outcome, Some(Outcome::Cancel)) {
                    return outcome;
                }
                continue;
            }
            let found = match event {
                Event::Key(name) => self.handle_key(name),
                Event::Text(text) => {
                    self.insert(text, false);
                    None
                }
                Event::Paste(text) => {
                    self.insert(text, true);
                    None
                }
                Event::Mouse { kind, y, .. } => self.handle_mouse(*kind, *y),
            };
            if found.is_some() {
                outcome = found;
            }
            if outcome.is_some() {
                return outcome;
            }
        }
        outcome
    }

    fn next_timeout(&self, escape_deadline: Option<f64>) -> f64 {
        let now = monotonic();
        if !self.age_queue.is_empty() {
            // Readings outstanding: come straight back for them, so a freshly
            // drawn list fills in its times in the next frame. A zero wait
            // still takes any input that is already waiting first.
            return 0.0;
        }
        let mut deadline = self.last_refresh + self.config.refresh_ms as f64 / 1000.0;
        if let Some((_, due)) = &self.preview_pending {
            deadline = deadline.min(*due);
        }
        if let Some(escape) = escape_deadline {
            deadline = deadline.min(escape);
        }
        if self.status.as_deref().map(|s| !s.is_empty()).unwrap_or(false) {
            deadline = deadline.min(self.status_until);
        }
        (deadline - now).max(0.01)
    }
}

/// Focus whatever the row points at.
pub fn jump(client: &mut HerdrClient, item: &Item) -> Result<()> {
    if item.kind == KIND_SPACE {
        return client.focus_workspace(&item.workspace_id);
    }
    if let Some(tab_id) = item.tab_id.as_deref().filter(|t| !t.is_empty()) {
        client.focus_tab(tab_id)?;
    }
    if let Some(pane_id) = item.pane_id.as_deref().filter(|p| !p.is_empty()) {
        if item.kind == KIND_AGENT {
            client.focus_agent(pane_id)?;
        } else if item.kind == KIND_PANE {
            client.focus_pane(pane_id)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::items::ItemInit;
    #[cfg(unix)]
    use crate::pyjson;
    use crate::pyjson::Map;
    #[cfg(unix)]
    use crate::testutil;

    fn item_with(fields: &[&str]) -> Item {
        let get = |i: usize| fields.get(i).map(|s| s.to_string()).unwrap_or_default();
        Item::new(ItemInit {
            kind: KIND_TAB,
            key: "k".into(),
            title: get(0),
            detail: get(1),
            subtitle: get(2),
            workspace_id: String::new(),
            workspace_label: get(3),
            tab_id: None,
            pane_id: None,
            agent: None,
            agent_name: None,
            status: String::new(),
            tab_number: None,
            focused: false,
            extra_terms: fields.iter().skip(4).map(|s| s.to_string()).collect(),
        })
    }

    fn score(terms: &[&str], fields: &[&str]) -> Option<(i64, BTreeMap<usize, Vec<usize>>)> {
        let item = item_with(fields);
        assert_eq!(item.fields.len(), fields.len());
        let terms: Vec<Term> = terms.iter().map(|t| Term::new(t)).collect();
        let mut memo = Memo::default();
        score_item(&terms, &item, &mut memo)
    }

    #[test]
    fn terms_may_match_different_fields() {
        assert!(score(&["retry", "codex"], &["retry queue backoff", "erestor", "codex"]).is_some());
    }

    #[test]
    fn a_term_matching_nothing_rejects_the_row() {
        assert!(score(&["nope"], &["retry queue", "erestor"]).is_none());
    }

    #[test]
    fn positions_are_keyed_by_the_field_that_won_the_term() {
        let (_, positions) = score(&["codex"], &["retry queue backoff", "codex"]).unwrap();
        assert_eq!(positions, BTreeMap::from([(1, vec![0, 1, 2, 3, 4])]));
    }

    #[test]
    fn each_term_highlights_its_own_field() {
        let (_, positions) = score(&["retry", "codex"], &["retry queue", "codex"]).unwrap();
        assert_eq!(positions.keys().copied().collect::<Vec<_>>(), vec![0, 1]);
    }

    #[test]
    fn title_matches_outrank_field_matches() {
        let (title_hit, _) = score(&["retry"], &["retry queue", "other"]).unwrap();
        let (field_hit, _) = score(&["retry"], &["something else", "retry"]).unwrap();
        assert!(title_hit > field_hit);
    }

    #[test]
    fn false_hides_the_preview_to_begin_with() {
        let config = config_of(&[("preview", Value::Bool(false))]);
        assert!(!config.preview_starts_on());
        assert!(config.fits_preview(120, true));
    }

    #[test]
    fn auto_waits_for_a_wide_popup_unless_asked() {
        let config = Config::default();
        assert!(config.preview_starts_on());
        assert!(!config.fits_preview(88, false));
        assert!(config.fits_preview(88, true));
        assert!(config.fits_preview(120, false));
    }

    #[test]
    fn true_keeps_the_preview_in_narrower_popups() {
        let config = config_of(&[("preview", Value::Bool(true))]);
        assert!(config.preview_starts_on());
        assert!(config.fits_preview(70, false));
        assert!(!config.fits_preview(40, false));
    }

    fn config_of(entries: &[(&str, Value)]) -> Config {
        let mut map = Map::new();
        for (key, value) in entries {
            map.insert(*key, value.clone());
        }
        Config::from_values(&map)
    }

    // -- a fake Herdr ---------------------------------------------------------

    /// What the Python tests' FakeClient held.
    #[cfg(unix)]
    struct State {
        snapshot: Value,
        calls: Vec<Vec<String>>,
        fail: bool,
        close_error: Option<String>,
        rename_error: Option<String>,
        /// Panes the fake can date (it reports this test process for them).
        ages: Value,
        age_error: bool,
    }

    #[cfg(unix)]
    type Shared = std::sync::Arc<std::sync::Mutex<State>>;

    #[cfg(unix)]
    struct Fake {
        state: Shared,
        path: std::path::PathBuf,
        stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
        thread: Option<std::thread::JoinHandle<()>>,
    }

    #[cfg(unix)]
    fn s(value: &Value, key: &str) -> String {
        value.get(key).as_str().unwrap_or("").to_string()
    }

    #[cfg(unix)]
    fn ok(result: Map) -> Value {
        let mut response = Map::new();
        response.insert("result", Value::Dict(result));
        Value::Dict(response)
    }

    #[cfg(unix)]
    fn err(message: &str) -> Value {
        let mut error = Map::new();
        error.insert("message", Value::str(message));
        let mut response = Map::new();
        response.insert("error", Value::Dict(error));
        Value::Dict(response)
    }

    #[cfg(unix)]
    fn answer(state: &Shared, request: &Value) -> Value {
        let mut state = state.lock().unwrap();
        let params = request.get("params").clone();
        let method = s(request, "method");
        let mut result = Map::new();
        match method.as_str() {
            "session.snapshot" => {
                if state.fail {
                    return err("herdr is not running");
                }
                result.insert("snapshot", state.snapshot.clone());
            }
            "pane.read" => {
                state.calls.push(vec!["read".into(), s(&params, "pane_id")]);
                let mut read = Map::new();
                read.insert("text", Value::str(testutil::preview_text()));
                result.insert("read", Value::Dict(read));
            }
            "pane.process_info" => {
                let pane = s(&params, "pane_id");
                state.calls.push(vec!["age".into(), pane.clone()]);
                if state.age_error {
                    return err("herdr is not running");
                }
                let mut info = Map::new();
                if !state.ages.get(&pane).is_null() {
                    info.insert("shell_pid", Value::Int(std::process::id() as i128));
                }
                result.insert("process_info", Value::Dict(info));
            }
            "tab.close" => {
                let tab = s(&params, "tab_id");
                state.calls.push(vec!["close".into(), tab.clone()]);
                if let Some(message) = state.close_error.clone() {
                    return err(&message);
                }
                let snapshot = state.snapshot.as_dict_mut().unwrap();
                for key in ["tabs", "agents", "panes"] {
                    if let Some(Value::List(records)) = snapshot.get_mut(key) {
                        records.retain(|record| record.get("tab_id").as_str() != Some(tab.as_str()));
                    }
                }
            }
            "tab.rename" => {
                let (tab, label) = (s(&params, "tab_id"), s(&params, "label"));
                state.calls.push(vec!["rename".into(), tab.clone(), label.clone()]);
                if let Some(message) = state.rename_error.clone() {
                    return err(&message);
                }
                if let Some(Value::List(tabs)) = state.snapshot.as_dict_mut().unwrap().get_mut("tabs") {
                    for record in tabs.iter_mut() {
                        if record.get("tab_id").as_str() == Some(tab.as_str()) {
                            record.as_dict_mut().unwrap().insert("label", Value::str(label.clone()));
                        }
                    }
                }
            }
            "workspace.focus" => state.calls.push(vec!["workspace".into(), s(&params, "workspace_id")]),
            "tab.focus" => state.calls.push(vec!["tab".into(), s(&params, "tab_id")]),
            "agent.focus" => state.calls.push(vec!["agent".into(), s(&params, "target")]),
            "pane.focus" => state.calls.push(vec!["pane".into(), s(&params, "pane_id")]),
            _ => return err("unknown method"),
        }
        ok(result)
    }

    #[cfg(unix)]
    impl Fake {
        fn new(snapshot: Value) -> Fake {
            use std::io::{BufRead, BufReader, Write};
            let path = testutil::socket_path();
            let listener = std::os::unix::net::UnixListener::bind(&path).expect("bind fake herdr");
            let state: Shared = std::sync::Arc::new(std::sync::Mutex::new(State {
                snapshot,
                calls: Vec::new(),
                fail: false,
                close_error: None,
                rename_error: None,
                ages: testutil::pane_ages(),
                age_error: false,
            }));
            let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            let (shared, halt) = (state.clone(), stop.clone());
            let thread = std::thread::spawn(move || {
                for conn in listener.incoming() {
                    if halt.load(std::sync::atomic::Ordering::SeqCst) {
                        break;
                    }
                    let Ok(mut conn) = conn else { continue };
                    let mut line = String::new();
                    if BufReader::new(&conn).read_line(&mut line).is_err() {
                        continue;
                    }
                    let request = pyjson::loads(&line).unwrap_or(Value::Null);
                    let mut out = pyjson::dumps(&answer(&shared, &request), false);
                    out.push('\n');
                    let _ = conn.write_all(out.as_bytes());
                }
            });
            Fake { state, path, stop, thread: Some(thread) }
        }

        fn client(&self) -> HerdrClient {
            let client = HerdrClient::new(Some(self.path.to_string_lossy().into_owned()), 4.0, false);
            assert!(client.socket_ok());
            client
        }

        fn set(&self, change: impl FnOnce(&mut State)) {
            change(&mut self.state.lock().unwrap());
        }

        fn calls(&self, kind: &str) -> Vec<Vec<String>> {
            self.state.lock().unwrap().calls.iter().filter(|c| c[0] == kind).cloned().collect()
        }

        fn all_calls(&self) -> Vec<Vec<String>> {
            self.state.lock().unwrap().calls.clone()
        }

        fn clear_calls(&self) {
            self.state.lock().unwrap().calls.clear();
        }
    }

    #[cfg(unix)]
    impl Drop for Fake {
        fn drop(&mut self) {
            self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
            let _ = std::os::unix::net::UnixStream::connect(&self.path);
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
            let _ = std::fs::remove_file(&self.path);
        }
    }

    /// The Python tests' `bar()` fixture, plus the fake behind it.
    #[cfg(unix)]
    struct T {
        bar: Bar,
        fake: Fake,
        _env: std::sync::MutexGuard<'static, ()>,
    }

    #[cfg(unix)]
    fn bar_with(snapshot: Value, config: Config, recents: Recents) -> T {
        let env = testutil::env_lock();
        let fake = Fake::new(snapshot);
        let mut client = fake.client();
        let pool = client.snapshot().map(|snapshot| Pool::build(&snapshot, &config));
        let mut bar = Bar::new(client, config, recents, Theme::new(None, None, None));
        bar.bootstrap(pool, Vec::new()).expect("bootstrap");
        T { bar, fake, _env: env }
    }

    #[cfg(unix)]
    fn bar() -> T {
        bar_with(testutil::snapshot(), Config::default(), Recents::new(None))
    }

    #[cfg(unix)]
    fn titles(bar: &Bar) -> Vec<String> {
        bar.rows.iter().map(|row| bar.pool[row.item].title.clone()).collect()
    }

    #[cfg(unix)]
    fn row_item(bar: &Bar, index: usize) -> &Item {
        &bar.pool[bar.rows[index].item]
    }

    #[cfg(unix)]
    fn name(outcome: Option<Outcome>) -> Option<&'static str> {
        outcome.map(|o| match o {
            Outcome::Cancel => "cancel",
            Outcome::Confirm => "confirm",
        })
    }

    #[cfg(unix)]
    fn press(bar: &mut Bar, keys: &[&[u8]]) -> Option<&'static str> {
        let mut decoder = KeyDecoder::new();
        let mut outcome = None;
        for key in keys {
            let events = decoder.feed(key);
            outcome = name(bar.consume(&events)).or(outcome);
        }
        outcome
    }

    #[cfg(unix)]
    fn escape(bar: &mut Bar) -> Option<&'static str> {
        let mut decoder = KeyDecoder::new();
        decoder.feed(b"\x1b");
        let events = decoder.flush();
        name(bar.consume(&events))
    }

    #[cfg(unix)]
    fn index_of(bar: &Bar, find: impl Fn(&Item) -> bool) -> usize {
        bar.rows.iter().position(|row| find(&bar.pool[row.item])).expect("row")
    }

    #[cfg(unix)]
    fn item_index(bar: &Bar, find: impl Fn(&Item) -> bool) -> usize {
        bar.items().iter().position(find).expect("item")
    }

    #[cfg(unix)]
    const DELETE: &[u8] = b"\x1b[3~";

    // -- FilterTest -------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn empty_query_lists_regular_items_and_named_panes() {
        let t = bar();
        assert_eq!(t.bar.rows.len(), t.bar.items().len() + t.bar.pane_items().len());
        assert!(titles(&t.bar).contains(&"server logs".to_string()));
    }

    #[cfg(unix)]
    #[test]
    fn typing_filters_the_list() {
        let mut t = bar();
        t.bar.set_query("retry".into(), None);
        assert_eq!(titles(&t.bar)[0], "retry queue backoff");
    }

    #[cfg(unix)]
    #[test]
    fn everything_search_includes_named_panes() {
        let mut t = bar();
        t.bar.set_query("server logs".into(), None);
        assert_eq!(titles(&t.bar)[0], "server logs");
        assert_eq!(row_item(&t.bar, 0).kind, KIND_PANE);
    }

    #[cfg(unix)]
    #[test]
    fn named_current_pane_does_not_replace_the_current_agent_row() {
        let mut snapshot = testutil::snapshot();
        if let Some(Value::List(panes)) = snapshot.as_dict_mut().unwrap().get_mut("panes") {
            let focused = panes.iter_mut().find(|p| p.get("pane_id").as_str() == Some("w1:p3")).unwrap();
            focused.as_dict_mut().unwrap().insert("label", Value::str("current editor"));
        }
        let t = bar_with(snapshot, Config::default(), Recents::new(None));
        let keys: Vec<&str> = t.bar.rows.iter().map(|row| t.bar.pool[row.item].key.as_str()).collect();
        assert!(keys.contains(&"w1:p3"));
        assert!(keys.contains(&"pane:w1:p3"));
    }

    #[cfg(unix)]
    #[test]
    fn search_reaches_the_working_directory() {
        let mut t = bar();
        t.bar.set_query("worktrees".into(), None);
        assert!(titles(&t.bar).contains(&"library depth battery".to_string()));
    }

    #[cfg(unix)]
    #[test]
    fn search_reaches_the_agent_kind() {
        let mut t = bar();
        t.bar.set_query("gemini".into(), None);
        assert_eq!(titles(&t.bar), vec!["migrations"]);
    }

    #[cfg(unix)]
    #[test]
    fn terms_are_combined_with_and() {
        let mut t = bar();
        t.bar.set_query("cards codex".into(), None);
        assert_eq!(titles(&t.bar), vec!["cards meaningless"]);
    }

    #[cfg(unix)]
    #[test]
    fn current_row_is_never_first_at_rest() {
        let t = bar();
        let current = t.bar.current_key.clone().unwrap();
        assert_ne!(row_item(&t.bar, 0).key, current);
        assert!(t.bar.rows.iter().any(|row| t.bar.pool[row.item].key == current));
    }

    #[cfg(unix)]
    #[test]
    fn recents_float_to_the_top() {
        let mut recents = Recents::new(None);
        recents.touch("w1:p5", "library depth battery");
        let t = bar_with(testutil::snapshot(), Config::default(), recents);
        assert_eq!(titles(&t.bar)[0], "library depth battery");
    }

    #[cfg(unix)]
    #[test]
    fn current_row_sits_right_after_the_recents() {
        let mut recents = Recents::new(None);
        recents.touch("w1:p5", "");
        let t = bar_with(testutil::snapshot(), Config::default(), recents);
        assert_eq!(Some(&row_item(&t.bar, 1).key), t.bar.current_key.as_ref());
    }

    // -- ScopeTest --------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn sigil_sets_the_scope_without_entering_text() {
        let mut t = bar();
        t.bar.insert("@", false);
        assert_eq!(t.bar.scope, "agent");
        assert_eq!(t.bar.query, "");
        assert!(t.bar.rows.iter().all(|row| t.bar.pool[row.item].kind == KIND_AGENT));
    }

    #[cfg(unix)]
    #[test]
    fn shell_sigil_shows_tabs_without_agents() {
        let mut t = bar();
        t.bar.insert("$", false);
        let mut found = titles(&t.bar);
        found.sort();
        assert_eq!(found, vec!["logs", "server"]);
    }

    #[cfg(unix)]
    #[test]
    fn pane_sigil_shows_only_named_panes() {
        let mut t = bar();
        t.bar.insert("%", false);
        assert_eq!(t.bar.scope, "pane");
        assert!(t.bar.rows.iter().all(|row| t.bar.pool[row.item].kind == KIND_PANE));
        let mut found = titles(&t.bar);
        found.sort();
        assert_eq!(found, vec!["server logs", "web server"]);
    }

    #[cfg(unix)]
    #[test]
    fn needs_you_sigil_shows_blocked_and_done() {
        let mut t = bar();
        t.bar.insert("!", false);
        assert!(t.bar.rows.iter().all(|row| matches!(t.bar.pool[row.item].status.as_str(), "blocked" | "done")));
    }

    #[cfg(unix)]
    #[test]
    fn sigil_is_literal_once_a_query_exists() {
        let mut t = bar();
        t.bar.insert("a", false);
        t.bar.insert("@", false);
        assert_eq!(t.bar.query, "a@");
        assert_eq!(t.bar.scope, "all");
    }

    #[cfg(unix)]
    #[test]
    fn backspace_on_an_empty_query_clears_the_scope() {
        let mut t = bar();
        t.bar.insert("@", false);
        t.bar.backspace();
        assert_eq!(t.bar.scope, "all");
    }

    #[cfg(unix)]
    #[test]
    fn tab_cycles_scopes_both_ways() {
        let mut t = bar();
        t.bar.cycle_scope(1);
        assert_eq!(t.bar.scope, "agent");
        t.bar.cycle_scope(1);
        assert_eq!(t.bar.scope, "pane");
        t.bar.cycle_scope(1);
        assert_eq!(t.bar.scope, "tab");
        t.bar.cycle_scope(-1);
        assert_eq!(t.bar.scope, "pane");
    }

    #[cfg(unix)]
    #[test]
    fn configured_scope_is_the_filter_the_bar_opens_with() {
        let config = config_of(&[("scope", Value::str("agent"))]);
        let t = bar_with(testutil::snapshot(), config, Recents::new(None));
        assert_eq!(t.bar.scope, "agent");
        assert!(!t.bar.rows.is_empty());
        assert!(t.bar.rows.iter().all(|row| t.bar.pool[row.item].kind == KIND_AGENT));
    }

    #[cfg(unix)]
    #[test]
    fn backspace_clears_a_configured_scope_and_ctrl_u_restores_it() {
        let config = config_of(&[("scope", Value::str("agent"))]);
        let mut t = bar_with(testutil::snapshot(), config, Recents::new(None));
        press(&mut t.bar, &[b"\x7f"]);
        assert_eq!(t.bar.scope, "all");
        press(&mut t.bar, &[b"retry"]);
        press(&mut t.bar, &[b"\x15"]);
        assert_eq!(t.bar.query, "");
        assert_eq!(t.bar.scope, "agent");
    }

    #[cfg(unix)]
    #[test]
    fn ctrl_u_clears_a_typed_scope_back_to_all() {
        let mut t = bar();
        press(&mut t.bar, &[b"@"]);
        press(&mut t.bar, &[b"\x15"]);
        assert_eq!(t.bar.scope, "all");
    }

    // -- KeyTest ----------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn arrows_move_the_selection() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x1b[B"]);
        assert_eq!(t.bar.selected, 1);
        press(&mut t.bar, &[b"\x1b[A"]);
        assert_eq!(t.bar.selected, 0);
    }

    #[cfg(unix)]
    #[test]
    fn selection_wraps() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x1b[A"]);
        assert_eq!(t.bar.selected, t.bar.rows.len() - 1);
    }

    #[cfg(unix)]
    #[test]
    fn emacs_movement_keys() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x0e\x0e"]);
        assert_eq!(t.bar.selected, 2);
        press(&mut t.bar, &[b"\x10"]);
        assert_eq!(t.bar.selected, 1);
    }

    #[cfg(unix)]
    #[test]
    fn vim_movement_keys() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x0a\x0a"]);
        assert_eq!(t.bar.selected, 2);
        press(&mut t.bar, &[b"\x0b"]);
        assert_eq!(t.bar.selected, 1);
    }

    #[cfg(unix)]
    #[test]
    fn typing_and_editing() {
        let mut t = bar();
        press(&mut t.bar, &[b"retry"]);
        assert_eq!(t.bar.query, "retry");
        press(&mut t.bar, &[b"\x7f"]);
        assert_eq!(t.bar.query, "retr");
        press(&mut t.bar, &[b"\x15"]);
        assert_eq!(t.bar.query, "");
    }

    #[cfg(unix)]
    #[test]
    fn delete_word() {
        let mut t = bar();
        press(&mut t.bar, &[b"week gen"]);
        press(&mut t.bar, &[b"\x17"]);
        assert_eq!(t.bar.query, "week ");
    }

    #[cfg(unix)]
    #[test]
    fn cursor_editing_inserts_in_place() {
        let mut t = bar();
        press(&mut t.bar, &[b"week"]);
        press(&mut t.bar, &[b"\x1b[D\x1b[D"]);
        press(&mut t.bar, &[b"X"]);
        assert_eq!(t.bar.query, "weXek");
    }

    #[cfg(unix)]
    #[test]
    fn enter_confirms_and_escape_cancels() {
        let mut t = bar();
        assert_eq!(press(&mut t.bar, &[b"\r"]), Some("confirm"));
        assert_eq!(escape(&mut t.bar), Some("cancel"));
    }

    #[cfg(unix)]
    #[test]
    fn paste_becomes_query_text() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x1b[200~retry queue\x1b[201~"]);
        assert_eq!(t.bar.query, "retry queue");
    }

    #[cfg(unix)]
    #[test]
    fn enter_does_nothing_when_the_list_is_empty() {
        let mut t = bar();
        t.bar.set_query("zzzqqq".into(), None);
        assert_eq!(press(&mut t.bar, &[b"\r"]), None);
    }

    #[cfg(unix)]
    #[test]
    fn preview_toggle() {
        let mut t = bar();
        let before = t.bar.preview_enabled;
        press(&mut t.bar, &[b"\x0f"]);
        assert_ne!(before, t.bar.preview_enabled);
    }

    #[cfg(unix)]
    #[test]
    fn preview_false_is_a_starting_point_not_a_lock() {
        let mut t = bar_with(testutil::snapshot(), config_of(&[("preview", Value::Bool(false))]), Recents::new(None));
        assert!(!t.bar.preview_enabled);
        press(&mut t.bar, &[b"\x0f"]);
        assert!(t.bar.preview_enabled);
    }

    // -- MouseTest --------------------------------------------------------------

    #[cfg(unix)]
    fn mouse_bar() -> T {
        let mut t = bar();
        t.bar.list_top = 2;
        t.bar.list_height = 8;
        t
    }

    #[cfg(unix)]
    #[test]
    fn wheel_scrolls_the_selection() {
        let mut t = mouse_bar();
        t.bar.handle_mouse(MouseKind::WheelDown, 5);
        assert_eq!(t.bar.selected, 3);
    }

    #[cfg(unix)]
    #[test]
    fn click_selects_then_confirms() {
        let mut t = mouse_bar();
        assert_eq!(name(t.bar.handle_mouse(MouseKind::Press, 5)), None);
        assert_eq!(t.bar.selected, 2);
        assert_eq!(name(t.bar.handle_mouse(MouseKind::Press, 5)), Some("confirm"));
    }

    #[cfg(unix)]
    #[test]
    fn clicks_outside_the_list_are_ignored() {
        let mut t = mouse_bar();
        assert_eq!(name(t.bar.handle_mouse(MouseKind::Press, 1)), None);
        assert_eq!(t.bar.selected, 0);
    }

    // -- RefreshTest ------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn selection_survives_a_refresh() {
        let mut t = bar();
        t.bar.selected = 3;
        let keep = t.bar.selected_item().key.clone();
        t.bar.refresh(false);
        assert_eq!(t.bar.selected_item().key, keep);
    }

    #[cfg(unix)]
    #[test]
    fn a_dead_server_flashes_instead_of_raising() {
        let mut t = bar();
        t.fake.set(|s| s.fail = true);
        t.bar.refresh(false);
        assert!(t.bar.status.is_some());
        assert!(!t.bar.rows.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn closed_rows_disappear() {
        let mut t = bar();
        t.fake.set(|s| {
            if let Some(Value::List(agents)) = s.snapshot.as_dict_mut().unwrap().get_mut("agents") {
                agents.truncate(1);
            }
        });
        t.bar.refresh(false);
        assert_eq!(t.bar.rows.iter().filter(|row| t.bar.pool[row.item].kind == KIND_AGENT).count(), 1);
    }

    // -- JumpTest ---------------------------------------------------------------

    #[cfg(unix)]
    fn jumped(t: &mut T, index: usize) -> Vec<Vec<String>> {
        t.fake.clear_calls();
        let item = t.bar.pool[index].clone();
        jump(&mut t.bar.client, &item).expect("jump");
        t.fake.all_calls()
    }

    #[cfg(unix)]
    fn call(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    #[cfg(unix)]
    #[test]
    fn agent_rows_focus_the_tab_and_the_pane() {
        let mut t = bar();
        let index = item_index(&t.bar, |i| i.title == "cards meaningless");
        assert_eq!(jumped(&mut t, index), vec![call(&["tab", "w1:t4"]), call(&["agent", "w1:p4"])]);
    }

    #[cfg(unix)]
    #[test]
    fn tab_rows_only_focus_the_tab() {
        let mut t = bar();
        let index = item_index(&t.bar, |i| i.kind == KIND_TAB);
        let tab_id = t.bar.pool[index].tab_id.clone().unwrap();
        assert_eq!(jumped(&mut t, index), vec![call(&["tab", &tab_id])]);
    }

    #[cfg(unix)]
    #[test]
    fn workspace_rows_focus_the_workspace() {
        let mut t = bar();
        let index = item_index(&t.bar, |i| i.kind == KIND_SPACE);
        let workspace_id = t.bar.pool[index].workspace_id.clone();
        assert_eq!(jumped(&mut t, index), vec![call(&["workspace", &workspace_id])]);
    }

    #[cfg(unix)]
    #[test]
    fn pane_rows_focus_the_tab_and_exact_pane() {
        let mut t = bar();
        t.bar.insert("%", false);
        let row = index_of(&t.bar, |i| i.pane_id.as_deref() == Some("w1:p7"));
        let index = t.bar.rows[row].item;
        assert_eq!(jumped(&mut t, index), vec![call(&["tab", "w1:t1"]), call(&["pane", "w1:p7"])]);
    }

    // -- CloseTest --------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn delete_arms_a_confirmation_instead_of_closing() {
        let mut t = bar();
        press(&mut t.bar, &[DELETE]);
        assert!(t.bar.pending_close.is_some());
        assert!(t.fake.calls("close").is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn enter_closes_the_tab_behind_the_row() {
        let mut t = bar();
        let tab_id = t.bar.selected_item().tab_id.clone().unwrap();
        press(&mut t.bar, &[DELETE]);
        assert_eq!(press(&mut t.bar, &[b"\r"]), None); // confirming is not jumping
        assert_eq!(t.fake.calls("close"), vec![call(&["close", &tab_id])]);
        assert!(t.bar.pending_close.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn backspace_arms_it_too_once_it_has_nothing_to_erase() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x7f"]);
        assert!(t.bar.pending_close.is_some());
        assert!(t.bar.backspace_closes());
    }

    #[cfg(unix)]
    #[test]
    fn backspace_erases_the_query_before_it_closes_anything() {
        let mut t = bar();
        press(&mut t.bar, &[b"week"]);
        press(&mut t.bar, &[b"\x7f"]);
        assert_eq!(t.bar.query, "wee");
        assert!(t.bar.pending_close.is_none());
        assert!(!t.bar.backspace_closes());
    }

    #[cfg(unix)]
    #[test]
    fn backspace_clears_the_filter_before_it_closes_anything() {
        let mut t = bar();
        press(&mut t.bar, &[b"@"]);
        press(&mut t.bar, &[b"\x7f"]);
        assert_eq!(t.bar.scope, "all");
        assert!(t.bar.pending_close.is_none());
        press(&mut t.bar, &[b"\x7f"]);
        assert!(t.bar.pending_close.is_some());
    }

    #[cfg(unix)]
    #[test]
    fn a_held_delete_key_cannot_confirm_itself() {
        let mut t = bar();
        press(&mut t.bar, &[DELETE, DELETE]);
        assert!(t.bar.pending_close.is_none());
        assert!(t.fake.calls("close").is_empty());
        press(&mut t.bar, &[b"\x7f", b"\x7f"]);
        assert!(t.bar.pending_close.is_none());
        assert!(t.fake.calls("close").is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn y_confirms_too() {
        let mut t = bar();
        press(&mut t.bar, &[DELETE, b"y"]);
        assert_eq!(t.fake.calls("close").len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn escape_cancels_the_close_and_leaves_the_bar_open() {
        let mut t = bar();
        press(&mut t.bar, &[DELETE]);
        assert_eq!(escape(&mut t.bar), None);
        assert!(t.bar.pending_close.is_none());
        assert!(t.fake.calls("close").is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn any_other_key_cancels_and_is_swallowed() {
        let mut t = bar();
        press(&mut t.bar, &[DELETE, b"n"]);
        assert!(t.bar.pending_close.is_none());
        assert_eq!(t.bar.query, "");
        assert!(t.fake.calls("close").is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn ctrl_c_still_leaves() {
        let mut t = bar();
        press(&mut t.bar, &[DELETE]);
        assert_eq!(press(&mut t.bar, &[b"\x03"]), Some("cancel"));
        assert!(t.bar.pending_close.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn the_closed_row_disappears() {
        let mut t = bar();
        let title = t.bar.selected_item().title.clone();
        press(&mut t.bar, &[DELETE, b"\r"]);
        assert!(!titles(&t.bar).contains(&title));
        assert!(t.bar.selected < t.bar.rows.len());
    }

    #[cfg(unix)]
    #[test]
    fn the_confirmation_counts_the_agents_going_with_the_tab() {
        let mut snapshot = testutil::snapshot();
        {
            let map = snapshot.as_dict_mut().unwrap();
            let mut second = map.get("agents").unwrap().list_or_empty()[0].clone();
            let record = second.as_dict_mut().unwrap();
            record.insert("pane_id", Value::str("w1:p7"));
            record.insert("terminal_title_stripped", Value::str("second agent"));
            for key in ["agents", "panes"] {
                if let Some(Value::List(list)) = map.get_mut(key) {
                    list.push(second.clone());
                }
            }
        }
        let mut t = bar_with(snapshot, Config::default(), Recents::new(None));
        t.bar.selected = index_of(&t.bar, |i| i.pane_id.as_deref() == Some("w1:p7"));
        press(&mut t.bar, &[DELETE]);
        let pending = t.bar.pending_close.clone().unwrap();
        assert_eq!(pending.0, "w1:t2");
        assert_eq!(pending.2, 2);
    }

    #[cfg(unix)]
    #[test]
    fn workspace_rows_cannot_be_closed() {
        let mut t = bar();
        t.bar.selected = index_of(&t.bar, |i| i.kind == KIND_SPACE);
        press(&mut t.bar, &[DELETE]);
        assert!(t.bar.pending_close.is_none());
        assert!(t.bar.status.is_some());
        assert!(t.fake.calls("close").is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn a_refused_close_flashes_the_reason() {
        let mut t = bar();
        t.fake.set(|s| s.close_error = Some("tab is busy".into()));
        press(&mut t.bar, &[DELETE, b"\r"]);
        assert!(t.bar.status.as_deref().unwrap_or("").contains("busy"));
    }

    #[cfg(unix)]
    #[test]
    fn ctrl_d_forward_deletes_in_the_query() {
        let mut t = bar();
        press(&mut t.bar, &[b"week"]);
        press(&mut t.bar, &[b"\x1b[D\x1b[D"]);
        press(&mut t.bar, &[b"\x04"]);
        assert_eq!(t.bar.query, "wek");
    }

    // -- RenameTest -------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn ctrl_r_opens_an_editor_for_the_selected_tabs_name() {
        let mut t = bar();
        let selected = t.bar.selected_item().clone();
        press(&mut t.bar, &[b"\x12"]);
        assert_eq!(t.bar.pending_rename, Some((selected.tab_id.unwrap(), selected.title)));
        assert_eq!(t.bar.rename_text, "");
    }

    #[cfg(unix)]
    #[test]
    fn enter_renames_the_tab_behind_an_agent_row() {
        let mut t = bar();
        let tab_id = t.bar.selected_item().tab_id.clone().unwrap();
        press(&mut t.bar, &[b"\x12", b"new panel name", b"\r"]);
        assert_eq!(t.fake.calls("rename"), vec![call(&["rename", &tab_id, "new panel name"])]);
        assert!(t.bar.pending_rename.is_none());
        assert!(titles(&t.bar).contains(&"new panel name".to_string()));
    }

    #[cfg(unix)]
    #[test]
    fn escape_keeps_the_old_name_and_the_bar_open() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x12", b"replacement"]);
        assert_eq!(escape(&mut t.bar), None);
        assert!(t.bar.pending_rename.is_none());
        assert!(t.fake.calls("rename").is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn empty_enter_keeps_the_old_name() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x12", b"\r"]);
        assert!(t.bar.pending_rename.is_none());
        assert!(t.fake.calls("rename").is_empty());
        assert!(t.bar.status.as_deref().unwrap_or("").contains("unchanged"));
    }

    #[cfg(unix)]
    #[test]
    fn rename_editor_supports_cursor_edits_and_paste() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x12", b"\x1b[200~panel nme\x1b[201~", b"\x1b[D\x1b[D", b"a", b"\r"]);
        assert_eq!(t.fake.calls("rename")[0][2], "panel name");
    }

    #[cfg(unix)]
    #[test]
    fn rename_failure_stays_in_the_editor() {
        let mut t = bar();
        t.fake.set(|s| s.rename_error = Some("name rejected".into()));
        press(&mut t.bar, &[b"\x12", b"bad name", b"\r"]);
        assert!(t.bar.pending_rename.is_some());
        assert_eq!(t.bar.rename_text, "bad name");
        assert!(t.bar.status.as_deref().unwrap_or("").contains("rejected"));
    }

    #[cfg(unix)]
    #[test]
    fn workspace_rows_cannot_be_renamed() {
        let mut t = bar();
        t.bar.selected = index_of(&t.bar, |i| i.kind == KIND_SPACE);
        press(&mut t.bar, &[b"\x12"]);
        assert!(t.bar.pending_rename.is_none());
        assert!(t.bar.status.as_deref().unwrap_or("").contains("tabs and agents"));
    }

    #[cfg(unix)]
    #[test]
    fn ctrl_c_during_rename_still_leaves_the_bar() {
        let mut t = bar();
        press(&mut t.bar, &[b"\x12"]);
        assert_eq!(press(&mut t.bar, &[b"\x03"]), Some("cancel"));
        assert!(t.bar.pending_rename.is_none());
    }

    // -- AgeTest ----------------------------------------------------------------
    //
    // The fake reports this test process for every pane it can date, so a
    // reading is this process's real age rather than the fixture's number.

    #[cfg(unix)]
    fn pane_item(bar: &Bar, pane_id: &str) -> usize {
        item_index(bar, |i| i.pane_id.as_deref() == Some(pane_id))
    }

    #[cfg(unix)]
    #[test]
    fn a_pane_is_read_once_and_then_ticked_locally() {
        let mut t = bar();
        let item = pane_item(&t.bar, "w1:p3");
        assert_eq!(t.bar.age_of(item), None); // queued, not read yet
        t.bar.pump_ages();
        let age = t.bar.age_of(item).expect("dated");
        assert!(age >= 0.0);
        t.bar.pump_ages();
        assert_eq!(t.fake.calls("age"), vec![call(&["age", "w1:p3"])]);
    }

    #[cfg(unix)]
    #[test]
    fn every_queued_pane_is_read_in_one_pass() {
        // Python read one pane per pass. The port dates the whole queue at
        // once, with one `ps` for all of it (commit bc4747f), and never asks
        // about a pane again.
        let mut t = bar();
        for index in 0..t.bar.items().len() {
            t.bar.age_of(index);
        }
        let mut queued = t.bar.age_queue.clone();
        queued.sort();
        queued.dedup();
        assert!(queued.len() > 1);
        t.bar.pump_ages();
        assert_eq!(t.fake.calls("age").len(), queued.len());
        assert!(t.bar.age_queue.is_empty());
        t.bar.pump_ages();
        assert_eq!(t.fake.calls("age").len(), queued.len());
    }

    #[cfg(unix)]
    #[test]
    fn an_undatable_pane_is_not_asked_twice() {
        let mut t = bar();
        t.fake.set(|s| s.ages = Value::Dict(Map::new()));
        let item = pane_item(&t.bar, "w1:p3");
        t.bar.age_of(item);
        t.bar.pump_ages();
        assert_eq!(t.bar.age_of(item), None);
        t.bar.pump_ages();
        assert_eq!(t.fake.calls("age").len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn a_failing_read_is_not_fatal() {
        let mut t = bar();
        t.fake.set(|s| s.age_error = true);
        let item = pane_item(&t.bar, "w1:p3");
        t.bar.age_of(item);
        t.bar.pump_ages();
        assert_eq!(t.bar.age_of(item), None);
    }

    #[cfg(unix)]
    #[test]
    fn workspace_rows_have_no_running_time() {
        let mut t = bar();
        let item = item_index(&t.bar, |i| i.kind == KIND_SPACE);
        assert_eq!(t.bar.age_of(item), None);
        assert!(t.bar.age_queue.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn running_time_disabled_skips_the_reading() {
        let mut t = bar_with(testutil::snapshot(), config_of(&[("running_time", Value::Bool(false))]), Recents::new(None));
        let item = pane_item(&t.bar, "w1:p3");
        assert_eq!(t.bar.age_of(item), None);
        assert!(t.bar.age_queue.is_empty()); // never queued, so ps is never run
        t.bar.pump_ages();
        assert!(t.fake.calls("age").is_empty());
    }

    // -- PreviewTest ------------------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn preview_is_debounced_then_cached() {
        let mut t = bar();
        let item = t.bar.items().iter().position(|i| i.pane_id.is_some()).unwrap();
        let pane_id = t.bar.pool[item].pane_id.clone().unwrap();
        let (lines, loading) = t.bar.preview_lines(Some(item));
        assert!(loading);
        assert!(lines.is_empty());
        t.bar.preview_pending = Some((pane_id, 0.0));
        t.bar.pump_preview(10);
        let (lines, loading) = t.bar.preview_lines(Some(item));
        assert!(!loading);
        assert!(lines.join("\n").contains("Ready"));
        t.bar.pump_preview(10);
        assert_eq!(t.fake.calls("read").len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn workspace_rows_have_no_preview() {
        let mut t = bar();
        let item = item_index(&t.bar, |i| i.kind == KIND_SPACE);
        let (lines, loading) = t.bar.preview_lines(Some(item));
        assert!(lines.is_empty());
        assert!(!loading);
    }

    // -- frames (tests/test_render.py's draw() helper) -----------------------

    #[cfg(unix)]
    const SIZES: &[(isize, isize)] =
        &[(40, 6), (56, 8), (72, 10), (80, 24), (96, 12), (110, 20), (140, 30), (200, 40), (30, 5), (24, 4)];

    /// One draw, then the preview and running times the loop would fetch.
    #[cfg(unix)]
    fn settle(bar: &mut Bar, width: isize, height: isize) {
        bar.compose(width, height);
        if let Some((pane, _)) = bar.preview_pending.clone() {
            bar.preview_pending = Some((pane, 0.0)); // skip the debounce
        }
        let height = bar.list_height;
        bar.pump_preview(height);
        while !bar.age_queue.is_empty() {
            bar.pump_ages();
        }
    }

    #[cfg(unix)]
    struct Frame {
        t: T,
        rows: Vec<String>,
    }

    #[cfg(unix)]
    fn frame(width: isize, height: isize, query: &str, scope: &'static str, config: Config, confirm: bool, keys: &[u8]) -> Frame {
        let mut t = bar_with(testutil::snapshot(), config, Recents::new(None));
        let icons = t.bar.theme.agent_icons;
        t.bar.theme = Theme::new(None, Some(&Value::str("237")), None);
        t.bar.theme.agent_icons = icons;
        // FakeClient.pane_age answered from PANE_AGES; the fake socket dates
        // panes by this test process instead, so seed the fixture's readings.
        if let Value::Dict(ages) = testutil::pane_ages() {
            for (pane, seconds) in ages.iter() {
                let seconds = match seconds {
                    Value::Int(n) => *n as f64,
                    Value::Float(f) => *f,
                    _ => continue,
                };
                t.bar.ages.insert(pane.clone(), Some((monotonic(), seconds)));
            }
        }
        t.bar.scope = scope;
        if !query.is_empty() {
            t.bar.set_query(query.to_string(), None);
        }
        t.bar.rebuild(None);
        settle(&mut t.bar, width, height);
        if !keys.is_empty() {
            press(&mut t.bar, &[keys]);
            settle(&mut t.bar, width, height);
        }
        if confirm {
            t.bar.request_close();
        }
        let mut rows = t.bar.compose(width, height);
        rows.resize(height.max(0) as usize, String::new());
        Frame { t, rows }
    }

    #[cfg(unix)]
    fn default_config() -> Config {
        config_of(&[("selection_background", Value::str("237"))])
    }

    #[cfg(unix)]
    fn draw(width: isize, height: isize, query: &str) -> Frame {
        frame(width, height, query, "all", default_config(), false, b"")
    }

    #[cfg(unix)]
    fn joined(rows: &[String]) -> String {
        crate::textutil::strip_ansi(&rows.join("\n"))
    }

    /// Whether `text` has digits, `sep`, then digits (`\d+<sep>\d+`).
    #[cfg(unix)]
    fn has_digits_around(text: &str, sep: char) -> bool {
        let c: Vec<char> = text.chars().collect();
        (1..c.len().saturating_sub(1)).any(|i| c[i] == sep && c[i - 1].is_ascii_digit() && c[i + 1].is_ascii_digit())
    }

    /// `\d+h\d\dm`
    #[cfg(unix)]
    fn has_hours(text: &str) -> bool {
        let c: Vec<char> = text.chars().collect();
        (1..c.len()).any(|i| {
            c[i] == 'h'
                && c[i - 1].is_ascii_digit()
                && c.len() > i + 3
                && c[i + 1].is_ascii_digit()
                && c[i + 2].is_ascii_digit()
                && c[i + 3] == 'm'
        })
    }

    #[cfg(unix)]
    #[test]
    fn rows_never_exceed_the_popup_width() {
        for &(width, height) in SIZES {
            for query in ["", "week", "zzzz"] {
                let f = draw(width, height, query);
                for (index, row) in f.rows.iter().enumerate() {
                    assert!(
                        crate::textutil::visible_width(row) as isize <= width,
                        "row {} overflows at {}x{} query={:?}: {:?}",
                        index, width, height, query, row
                    );
                }
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn an_armed_close_never_overflows_either() {
        for &(width, height) in SIZES {
            let f = frame(width, height, "", "all", default_config(), true, b"");
            for (index, row) in f.rows.iter().enumerate() {
                assert!(crate::textutil::visible_width(row) as isize <= width, "row {} at {}x{}: {:?}", index, width, height, row);
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn frame_never_exceeds_the_popup_height() {
        for &(width, height) in SIZES {
            let mut t = bar();
            assert!(t.bar.compose(width, height).len() as isize <= height, "{}x{}", width, height);
            drop(t);
            assert_eq!(draw(width, height, "").rows.len() as isize, height);
        }
    }

    #[cfg(unix)]
    #[test]
    fn query_is_echoed_in_the_input_row() {
        assert!(draw(110, 20, "week").rows[0].contains("week"));
    }

    #[cfg(unix)]
    #[test]
    fn placeholder_appears_only_when_empty() {
        assert!(draw(110, 20, "").rows[0].contains("jump to a tab"));
        assert!(!draw(110, 20, "week").rows[0].contains("jump to a tab"));
    }

    #[cfg(unix)]
    #[test]
    fn top_bar_highlights_the_active_filter() {
        let accent = Theme::new(None, None, None).fg("accent").to_string();
        let all = draw(110, 20, "").rows[0].clone();
        let agent = frame(110, 20, "", "agent", default_config(), false, b"").rows[0].clone();
        assert!(all.contains(&format!("{} everything ", accent)));
        assert!(crate::textutil::strip_ansi(&all).contains("everything"));
        assert!(agent.contains(&format!("{} @ agents ", accent)));
        assert!(crate::textutil::strip_ansi(&agent).contains("@ agents"));
    }

    #[cfg(unix)]
    #[test]
    fn selected_row_is_marked() {
        let f = draw(110, 20, "");
        let body: Vec<&String> = f.rows.iter().filter(|r| r.contains("▌")).collect();
        assert_eq!(body.len(), 1);
        let title: String = f.t.bar.selected_item().title.chars().take(12).collect();
        assert!(body[0].contains(&title));
    }

    #[cfg(unix)]
    #[test]
    fn preview_shows_pane_output() {
        assert!(draw(140, 20, "").rows.join("\n").contains("Ready"));
    }

    #[cfg(unix)]
    #[test]
    fn preview_false_hides_the_preview_but_ctrl_o_brings_it_back() {
        let hidden = || config_of(&[("preview", Value::Bool(false))]);
        assert!(!frame(140, 20, "", "all", hidden(), false, b"").rows.join("\n").contains("Ready"));
        assert!(frame(140, 20, "", "all", hidden(), false, b"\x0f").rows.join("\n").contains("Ready"));
    }

    #[cfg(unix)]
    #[test]
    fn ctrl_o_opens_the_preview_in_a_popup_auto_thinks_is_too_narrow() {
        assert!(!draw(90, 20, "").rows.join("\n").contains("Ready"));
        assert!(frame(90, 20, "", "all", default_config(), false, b"\x0f").rows.join("\n").contains("Ready"));
    }

    #[cfg(unix)]
    #[test]
    fn ctrl_o_hides_a_preview_that_is_showing() {
        assert!(!frame(140, 20, "", "all", default_config(), false, b"\x0f").rows.join("\n").contains("Ready"));
    }

    #[cfg(unix)]
    #[test]
    fn empty_state_when_nothing_matches() {
        assert!(draw(110, 20, "zzzqqq").rows.join("\n").contains("no matches"));
    }

    #[cfg(unix)]
    #[test]
    fn pane_filter_shows_its_name_and_named_panes() {
        let plain = joined(&frame(110, 20, "", "pane", default_config(), false, b"").rows);
        assert!(plain.contains("% panes"));
        assert!(plain.contains("server logs"));
    }

    #[cfg(unix)]
    #[test]
    fn counter_shows_filtered_over_total() {
        let f = draw(110, 20, "week");
        assert!(has_digits_around(f.rows.last().unwrap(), '/'), "{:?}", f.rows.last());
    }

    #[cfg(unix)]
    #[test]
    fn rows_carry_their_running_time() {
        let plain = joined(&draw(140, 20, "").rows);
        assert!(has_hours(&plain), "{}", plain);
    }

    #[cfg(unix)]
    #[test]
    fn the_footer_names_the_delete_key_that_works() {
        let empty = crate::textutil::strip_ansi(draw(110, 20, "").rows.last().unwrap());
        let typed = crate::textutil::strip_ansi(draw(110, 20, "week").rows.last().unwrap());
        assert!(empty.contains("⌫ close"), "{:?}", empty);
        assert!(typed.contains("⌦ close"), "{:?}", typed);
    }

    #[cfg(unix)]
    #[test]
    fn an_armed_close_replaces_the_hints() {
        let f = frame(110, 20, "", "all", default_config(), true, b"");
        let footer = crate::textutil::strip_ansi(f.rows.last().unwrap());
        let title: String = f.t.bar.pending_close.as_ref().expect("armed").1.chars().take(12).collect();
        assert!(footer.contains("close"));
        assert!(footer.contains(&title));
        assert!(!footer.contains("scope"));
    }
}
