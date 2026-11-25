//! Compose the bar's rows.
//!
//! Everything is drawn as full rows into the popup's inner area: Herdr already
//! draws the popup border, so the bar stays borderless and full-bleed. Rows
//! are composed as (role, text) segments, measured in display columns, and
//! only then turned into escape sequences -- which keeps truncation honest for
//! CJK and emoji.

use crate::age::format_age;
use crate::icons;
use crate::items::{Item, KIND_ACTION, KIND_PANE, KIND_SPACE};
use crate::pyos;
use crate::pystr;
use crate::textutil::{display_width, pad, truncate, truncate_middle, window_positions};
use crate::theme::{Theme, BOLD, RESET, UNDERLINE};
use std::collections::BTreeMap;

pub const PROMPT: &str = "❯";
pub const SELECTED_BAR: &str = "▌";
pub const SCROLL_TRACK: &str = "│";
pub const SCROLL_THUMB: &str = "┃";
pub const SEPARATOR: &str = " │ ";

pub fn status_word(status: &str) -> &'static str {
    match status {
        "blocked" => "needs you",
        "working" => "working",
        "done" => "done",
        "idle" => "idle",
        _ => "",
    }
}

pub const DETAIL_SEP: &str = " — ";
pub const MIN_DETAIL: isize = 6;
pub const MIN_TITLE: isize = 16;
pub const META_SHARE: f64 = 0.45;
pub const MIN_LIST: isize = 46;
pub const PREVIEW_MIN: isize = 28;
pub const PREVIEW_MAX: isize = 56;

/// An item plus where the query landed in it.
#[derive(Clone, Debug)]
pub struct Row {
    pub item: usize,
    pub positions: Vec<usize>,
    pub field_positions: BTreeMap<usize, Vec<usize>>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Layout {
    pub list_top: isize,
    pub list_height: isize,
    pub rules: bool,
    pub footer: bool,
    pub inner_width: isize,
    pub list_width: isize,
    pub preview_width: isize,
}

pub fn compute_layout(width: isize, height: isize, preview: bool) -> Layout {
    let rules = height >= 8;
    let footer = height >= 6;
    let list_top = if rules { 2 } else { 1 };
    let chrome = list_top + if rules && footer { 2 } else if footer { 1 } else { 0 };
    let list_height = (height - chrome).max(1);
    let inner = (width - 2).max(1);
    let mut preview_width = 0;
    let mut list_width = inner;
    let separator = pystr::len(SEPARATOR) as isize;
    if preview && inner >= PREVIEW_MIN + MIN_LIST + separator {
        preview_width = PREVIEW_MAX.min(PREVIEW_MIN.max(inner / 3));
        list_width = inner - preview_width - separator;
    }
    Layout { list_top, list_height, rules, footer, inner_width: inner, list_width, preview_width }
}

#[derive(Clone, Debug)]
pub struct Segment {
    pub role: &'static str,
    pub text: String,
    pub bold: bool,
    pub underline: bool,
}

impl Segment {
    pub fn new(role: &'static str, text: impl Into<String>) -> Segment {
        Segment { role, text: text.into(), bold: false, underline: false }
    }

    pub fn bold(role: &'static str, text: impl Into<String>, bold: bool) -> Segment {
        Segment { role, text: text.into(), bold, underline: false }
    }
}

pub fn segments_to_text(theme: &Theme, segments: &[Segment], background: &str) -> String {
    let mut out = String::new();
    for segment in segments {
        if segment.text.is_empty() {
            continue;
        }
        let color = if segment.role.is_empty() { "" } else { theme.fg(segment.role) };
        if color.is_empty() && !segment.bold && !segment.underline {
            out.push_str(&segment.text);
            continue;
        }
        if segment.bold {
            out.push_str(BOLD);
        }
        if segment.underline {
            out.push_str(UNDERLINE);
        }
        out.push_str(color);
        out.push_str(&segment.text);
        out.push_str(RESET);
        out.push_str(background);
    }
    out
}

fn highlighted_label(theme: &Theme, text: &str) -> String {
    segments_to_text(theme, &[Segment::bold("accent", text, true)], "")
}

fn plain_width(segments: &[Segment]) -> isize {
    segments.iter().map(|s| display_width(&s.text) as isize).sum()
}

fn highlight(text: &str, positions: &[usize], base_role: &'static str, bold: bool) -> Vec<Segment> {
    if positions.is_empty() {
        return vec![Segment::bold(base_role, text, bold)];
    }
    let mut segments = Vec::new();
    let mut run = String::new();
    let mut run_marked = false;
    let flush = |segments: &mut Vec<Segment>, run: &str, marked: bool| {
        if run.is_empty() {
            return;
        }
        segments.push(Segment {
            role: if marked { "match" } else { base_role },
            text: run.to_string(),
            bold: bold || marked,
            underline: marked,
        });
    };
    for (index, c) in text.chars().enumerate() {
        let marked = positions.contains(&index);
        if marked != run_marked && !run.is_empty() {
            flush(&mut segments, &run, run_marked);
            run.clear();
        }
        run_marked = marked;
        run.push(c);
    }
    flush(&mut segments, &run, run_marked);
    segments
}

/// The row's place: a workspace name, or the last segment of its path, with
/// its offset inside item.subtitle.
fn context_label(item: &Item) -> (String, isize) {
    if item.kind == KIND_SPACE {
        return (item.subtitle.clone(), 0);
    }
    if !item.subtitle.is_empty() {
        let stripped = item.subtitle.trim_end_matches('/');
        let base = pyos::basename(stripped);
        if !base.is_empty() {
            return (base.to_string(), pystr::len(stripped) as isize - pystr::len(base) as isize);
        }
    }
    (String::new(), 0)
}

fn field_marks(item: &Item, row: &Row, text: &str, offset: isize) -> Vec<isize> {
    if text.is_empty() || row.field_positions.is_empty() {
        return Vec::new();
    }
    let Some(index) = item.fields.iter().position(|f| f == text) else { return Vec::new() };
    let Some(marks) = row.field_positions.get(&index) else { return Vec::new() };
    if marks.is_empty() {
        return Vec::new();
    }
    marks.iter().map(|&m| m as isize + offset).filter(|&m| offset == 0 || m >= 0).collect()
}

fn chunk(role: &'static str, text: &str, marks: &[isize]) -> Vec<Segment> {
    if marks.is_empty() {
        return vec![Segment::new(role, text)];
    }
    let length = pystr::len(text) as isize;
    let kept: Vec<usize> = marks.iter().filter(|&&m| 0 <= m && m < length).map(|&m| m as usize).collect();
    highlight(text, &kept, role, false)
}

pub fn agent_role(agent: Option<&str>) -> &'static str {
    let Some(agent) = agent.filter(|a| !a.is_empty()) else { return "muted" };
    match icons::agent_kind(agent).as_str() {
        "claude" => "agent_claude",
        "codex" => "agent_codex",
        "kimi" => "agent_kimi",
        "pi" => "agent_pi",
        "grok" => "agent_grok",
        "gemini" => "agent_gemini",
        "cursor" | "cursor-agent" => "agent_cursor",
        "opencode" | "open-code" => "agent_opencode",
        "agy" => "agent_agy",
        _ => "muted",
    }
}

/// Whether metadata would repeat the title or its visible summary.
fn duplicates_primary_text(item: &Item, label: &str) -> bool {
    let normalized = pystr::casefold(pystr::strip(label));
    !normalized.is_empty()
        && (normalized == pystr::casefold(pystr::strip(&item.title))
            || normalized == pystr::casefold(pystr::strip(&item.detail)))
}

fn status_role(status: &str) -> &'static str {
    match status {
        "blocked" => "blocked",
        "working" => "working",
        "done" => "done",
        "idle" => "idle",
        _ => "unknown",
    }
}

pub fn render_row(
    theme: &Theme,
    item: &Item,
    row: &Row,
    width: isize,
    selected: bool,
    show_workspace: bool,
    age: Option<f64>,
) -> String {
    let background = if selected { theme.selection_bg.as_str() } else { "" };
    let marker =
        if selected { Segment::new("accent", format!("{} ", SELECTED_BAR)) } else { Segment::new("", "  ") };
    let glyph_role = status_role(&item.status);
    let glyph = Segment::new(glyph_role, format!("{} ", crate::theme::status_glyph(&item.status)));
    let logo = if theme.agent_icons { icons::logo_for(item.agent.as_deref()) } else { None };
    let mut leading = vec![marker];
    if let Some(logo) = logo {
        leading.push(Segment::new(agent_role(item.agent.as_deref()), format!("{}  ", logo)));
    }
    leading.push(glyph);

    let mut meta: Vec<Vec<Segment>> = Vec::new();
    let (context, context_start) = context_label(item);
    if show_workspace
        && !item.workspace_label.is_empty()
        && item.kind != KIND_SPACE
        && pystr::lower(&item.workspace_label) != pystr::lower(&context)
        && !duplicates_primary_text(item, &item.workspace_label)
    {
        let marks = field_marks(item, row, &item.workspace_label, 0);
        meta.push(chunk("muted", &item.workspace_label, &marks));
    }
    if !context.is_empty() && !duplicates_primary_text(item, &context) {
        let marks = field_marks(item, row, &item.subtitle, -context_start);
        meta.push(chunk("muted", &context, &marks));
    }
    let agent = item.agent.as_deref().unwrap_or("");
    let agent_name = item.agent_name.as_deref().unwrap_or("");
    if !agent.is_empty() && (!agent_name.is_empty() || logo.is_none()) {
        let (label, marks) = if !agent_name.is_empty() {
            (format!("@{}", agent_name), field_marks(item, row, agent_name, 1))
        } else {
            (agent.to_string(), field_marks(item, row, agent, 0))
        };
        meta.push(chunk(agent_role(Some(agent)), &label, &marks));
    }
    if item.focused {
        meta.push(vec![Segment::new("accent", "current")]);
    }
    if let Some(age) = age {
        meta.push(vec![Segment::new("muted", format_age(age))]);
    }
    let word = status_word(&item.status);
    if !word.is_empty() {
        let marks = field_marks(item, row, word, 0);
        meta.push(chunk(glyph_role, word, &marks));
    }
    if item.kind == KIND_SPACE {
        meta.push(chunk("muted", "space", &field_marks(item, row, "space", 0)));
    } else if item.kind == KIND_PANE {
        meta.push(chunk("muted", "pane", &field_marks(item, row, "pane", 0)));
    } else if item.kind == KIND_ACTION {
        meta.push(chunk("muted", "action", &field_marks(item, row, "action", 0)));
    }

    let fixed = plain_width(&leading);
    let available = (width - fixed).max(0);
    let meta_budget = MIN_TITLE.max((available as f64 * META_SHARE) as isize);
    while !meta.is_empty() && (meta_width(&meta) > meta_budget || available - meta_width(&meta) - 2 < MIN_TITLE) {
        meta.remove(0);
    }
    let meta_w = if meta.is_empty() { 0 } else { meta_width(&meta) };
    let title_width = (available - if meta_w > 0 { meta_w + 2 } else { 0 }).max(1);

    let (title_text, positions, _) = window_positions(&item.title, title_width, &row.positions);
    let title_role = if item.status == "working" { "working" } else { "text" };
    let mut title_segments = highlight(&title_text, &positions, title_role, selected);

    let mut used = plain_width(&title_segments);
    if !item.detail.is_empty() {
        let room = title_width - used - display_width(DETAIL_SEP) as isize;
        if room >= MIN_DETAIL {
            let empty = Vec::new();
            let detail_marks = if item.detail_field >= 0 {
                row.field_positions.get(&(item.detail_field as usize)).unwrap_or(&empty)
            } else {
                &empty
            };
            let (detail_text, marks, _) = window_positions(&item.detail, room, detail_marks);
            title_segments.push(Segment::new("unknown", DETAIL_SEP));
            title_segments.extend(highlight(&detail_text, &marks, "muted", false));
            used = plain_width(&title_segments);
        }
    }
    let gap = if meta_w > 0 { (available - used - meta_w).max(1) } else { available - used };
    let mut segments = leading;
    segments.extend(title_segments);
    if meta_w > 0 {
        segments.push(Segment::new("", " ".repeat(gap.max(0) as usize)));
        segments.extend(join_meta(meta));
    } else {
        segments.push(Segment::new("", " ".repeat(gap.max(0) as usize)));
    }

    let body = segments_to_text(theme, &segments, background);
    let trailing = (width - plain_width(&segments)).max(0) as usize;
    if !background.is_empty() {
        format!("{}{}{}{}", background, body, " ".repeat(trailing), RESET)
    } else {
        format!("{}{}", body, " ".repeat(trailing))
    }
}

fn join_meta(meta: Vec<Vec<Segment>>) -> Vec<Segment> {
    let mut out = Vec::new();
    for (index, chunk) in meta.into_iter().enumerate() {
        if index > 0 {
            out.push(Segment::new("unknown", " · "));
        }
        out.extend(chunk);
    }
    out
}

fn meta_width(meta: &[Vec<Segment>]) -> isize {
    if meta.is_empty() {
        return 0;
    }
    meta.iter().map(|chunk| plain_width(chunk)).sum::<isize>() + 3 * (meta.len() as isize - 1)
}

fn slice(text: &str, start: usize, stop: usize) -> String {
    text.chars().skip(start).take(stop.saturating_sub(start)).collect()
}

pub fn render_input(
    theme: &Theme,
    query: &str,
    cursor: usize,
    width: isize,
    placeholder: &str,
    chip: &str,
    prompt_text: &str,
) -> String {
    let mut chip_text = String::new();
    let mut chip_width = 0;
    let prompt_width = display_width(prompt_text) as isize + 1;
    if !chip.is_empty() && width > display_width(chip) as isize + prompt_width + 24 {
        chip_text = format!(" {} ", chip);
        chip_width = display_width(&chip_text) as isize;
    }
    let field_width = (width - prompt_width - chip_width).max(1);
    let prompt = segments_to_text(theme, &[Segment::bold("accent", format!("{} ", prompt_text), true)], "");

    let mut rendered;
    let used;
    if !query.is_empty() {
        let visible = truncate(query, field_width);
        let length = pystr::len(&visible);
        let cursor_index = cursor.min(length);
        let before = slice(&visible, 0, cursor_index);
        let under = if cursor_index < length { slice(&visible, cursor_index, cursor_index + 1) } else { " ".into() };
        let after = if cursor_index < length { slice(&visible, cursor_index + 1, length) } else { String::new() };
        rendered = format!(
            "{}{}\x1b[7m{}\x1b[27m{}",
            prompt,
            segments_to_text(theme, &[Segment::new("text", before.clone())], ""),
            under,
            segments_to_text(theme, &[Segment::new("text", after.clone())], "")
        );
        used = prompt_width
            + display_width(&before) as isize
            + display_width(&under) as isize
            + display_width(&after) as isize;
    } else {
        let hint = truncate(placeholder, (field_width - 2).max(0));
        rendered =
            format!("{}\x1b[7m \x1b[27m{}", prompt, segments_to_text(theme, &[Segment::new("unknown", hint.clone())], ""));
        used = prompt_width + 1 + display_width(&hint) as isize;
    }
    if !chip_text.is_empty() {
        let padding = (width - used - display_width(&chip_text) as isize).max(1);
        rendered.push_str(&" ".repeat(padding as usize));
        rendered.push_str(&highlighted_label(theme, &chip_text));
    }
    rendered
}

pub fn render_rule(theme: &Theme, width: isize) -> String {
    segments_to_text(theme, &[Segment::new("unknown", "─".repeat(width.max(0) as usize))], "")
}

pub fn render_footer(theme: &Theme, width: isize, hints: &[(&str, &str)], counter: &str) -> String {
    let mut segments: Vec<Segment> = Vec::new();
    for (index, (key, label)) in hints.iter().enumerate() {
        if index > 0 {
            segments.push(Segment::new("unknown", "  "));
        }
        segments.push(Segment::new("muted", *key));
        if !label.is_empty() {
            segments.push(Segment::new("unknown", format!(" {}", label)));
        }
    }
    let mut used = plain_width(&segments);
    let counter_width = display_width(counter) as isize;
    while !segments.is_empty() && used + counter_width + 2 > width {
        segments.pop();
        while let Some(last) = segments.last() {
            if last.role == "unknown" && pystr::strip(&last.text).is_empty() {
                segments.pop();
            } else {
                break;
            }
        }
        used = plain_width(&segments);
    }
    let padding = (width - used - counter_width).max(1);
    segments.push(Segment::new("", " ".repeat(padding as usize)));
    segments.push(Segment::new("unknown", counter));
    segments_to_text(theme, &segments, "")
}

/// The armed close: what is about to go, and the keys that answer.
pub fn render_confirm(theme: &Theme, width: isize, title: &str, agents: usize) -> String {
    let mut keys = "⏎ close · esc keep";
    let detail = if agents > 1 { format!(" and {} agents", agents) } else { String::new() };
    let room = 6.max(width - display_width(keys) as isize - display_width(&detail) as isize - 12);
    let mut segments = vec![
        Segment::bold("blocked", "close ", true),
        Segment::bold("text", format!("“{}”", truncate(title, room)), true),
        Segment::new("blocked", format!("{}?", detail)),
    ];
    let mut used = plain_width(&segments);
    if used + display_width(keys) as isize + 1 > width {
        keys = "⏎/esc";
    }
    if used + display_width(keys) as isize + 1 > width {
        let text = truncate(&format!("close “{}”?", title), (width - display_width(keys) as isize - 1).max(0));
        segments = vec![Segment::bold("blocked", text, true)];
        used = plain_width(&segments);
    }
    let padding = (width - used - display_width(keys) as isize).max(1);
    if used + padding + display_width(keys) as isize > width {
        return segments_to_text(theme, &[Segment::bold("blocked", truncate("close?", width), true)], "");
    }
    segments.push(Segment::new("", " ".repeat(padding as usize)));
    segments.push(Segment::new("muted", keys));
    segments_to_text(theme, &segments, "")
}

pub fn render_empty_state(theme: &Theme, width: isize, query: &str, has_items: bool, filter_name: &str) -> Vec<String> {
    let mut highlighted_filter = "";
    let headline: String;
    let detail: String;
    let hint: String;
    if !has_items {
        headline = "nothing to jump to".into();
        detail = "open a tab or start an agent".into();
        hint = String::new();
    } else if !query.is_empty() {
        headline = "no matches".into();
        detail = format!("for “{}”", truncate(query, 6.max(width - 12)));
        hint = "^u clears the query".into();
    } else if !filter_name.is_empty() {
        headline = format!("nothing in {} filter", filter_name);
        highlighted_filter = filter_name;
        detail = String::new();
        hint = "⇥ next filter · ⌫ back to everything".into();
    } else {
        headline = "no matches".into();
        detail = String::new();
        hint = String::new();
    }
    let mut lines = Vec::new();
    let rows: [(&str, &'static str, bool, &str); 3] =
        [(&headline, "text", true, highlighted_filter), (&detail, "muted", false, ""), (&hint, "unknown", false, "")];
    for (text, role, bold, selected_label) in rows {
        if text.is_empty() {
            continue;
        }
        let mut text = text.to_string();
        let rendered = if !selected_label.is_empty() && display_width(&text) as isize <= width {
            format!(
                "{}{}{}",
                segments_to_text(theme, &[Segment::bold(role, "nothing in ", bold)], ""),
                highlighted_label(theme, selected_label),
                segments_to_text(theme, &[Segment::bold(role, " filter", bold)], "")
            )
        } else {
            text = truncate(&text, width);
            segments_to_text(theme, &[Segment::bold(role, text.clone(), bold)], "")
        };
        let left = ((width - display_width(&text) as isize) / 2).max(0);
        // Python's floor division, for the negative case max() then discards.
        let left = if width - (display_width(&text) as isize) < 0 { 0 } else { left };
        lines.push(pad(&format!("{}{}", " ".repeat(left as usize), rendered), width));
    }
    lines
}

pub fn render_preview(
    theme: &Theme,
    item: Option<&Item>,
    lines: &[String],
    width: isize,
    height: isize,
    loading: bool,
) -> Vec<String> {
    if height <= 0 || width <= 0 {
        return Vec::new();
    }
    let Some(item) = item else { return vec![String::new(); height as usize] };
    if item.kind == KIND_ACTION {
        return render_action_preview(theme, item, width, height);
    }
    let mut out: Vec<String> = Vec::new();
    let mut header = vec![Segment::bold("text", truncate(&item.title, width), true)];
    let room = width - plain_width(&header) - display_width(DETAIL_SEP) as isize;
    if !item.detail.is_empty() && room >= MIN_DETAIL {
        header.push(Segment::new("unknown", DETAIL_SEP));
        header.push(Segment::new("muted", truncate(&item.detail, room)));
    }
    out.push(segments_to_text(theme, &header, ""));
    let subtitle = if !item.subtitle.is_empty() { &item.subtitle } else { &item.workspace_label };
    out.push(if !subtitle.is_empty() {
        segments_to_text(theme, &[Segment::new("muted", truncate_middle(subtitle, width))], "")
    } else {
        String::new()
    });
    out.push(segments_to_text(theme, &[Segment::new("unknown", "─".repeat(width as usize))], ""));

    let body_height = height - out.len() as isize;
    if body_height <= 0 {
        out.truncate(height as usize);
        return out;
    }
    let body: Vec<String> = if loading && lines.is_empty() {
        vec!["…".into()]
    } else if lines.is_empty() {
        vec!["no output yet".into()]
    } else {
        let skip = lines.len().saturating_sub(body_height as usize);
        lines[skip..].to_vec()
    };
    for _ in 0..(body_height - body.len() as isize).max(0) {
        out.push(String::new());
    }
    let skip = body.len().saturating_sub(body_height as usize);
    for line in &body[skip..] {
        out.push(segments_to_text(theme, &[Segment::new("muted", truncate(line, width))], ""));
    }
    out.truncate(height as usize);
    out
}

/// An action has nothing to tail: its name, the id a keybinding would use,
/// and what it says it does, from the top.
fn render_action_preview(theme: &Theme, item: &Item, width: isize, height: isize) -> Vec<String> {
    let qualified = item.plugin_action.as_ref().map(|(plugin, action)| format!("{}.{}", plugin, action)).unwrap_or_default();
    let mut out = vec![
        segments_to_text(theme, &[Segment::bold("text", truncate(&item.title, width), true)], ""),
        segments_to_text(theme, &[Segment::new("muted", truncate_middle(&qualified, width))], ""),
        segments_to_text(theme, &[Segment::new("unknown", "─".repeat(width as usize))], ""),
    ];
    let body = if item.detail.is_empty() { vec!["no description".to_string()] } else { wrap(&item.detail, width) };
    for line in body {
        out.push(segments_to_text(theme, &[Segment::new("muted", truncate(&line, width))], ""));
    }
    out.resize(height as usize, String::new());
    out
}

/// `text` in lines of at most `width` columns, broken between words where
/// it can be.
fn wrap(text: &str, width: isize) -> Vec<String> {
    let width = width.max(1) as usize;
    let mut lines: Vec<String> = Vec::new();
    let mut line = String::new();
    for word in text.split_whitespace() {
        if !line.is_empty() && display_width(&line) + 1 + display_width(word) > width {
            lines.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line.push_str(word);
        // A word wider than the line goes on its own and gets truncated.
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}

/// Row index -> scrollbar glyph for the visible window.
pub fn scrollbar_column(total: isize, visible: isize, offset: isize, height: isize) -> BTreeMap<isize, &'static str> {
    let mut marks = BTreeMap::new();
    if total <= visible || height <= 0 {
        return marks;
    }
    let thumb = 1.max(((height * visible) as f64 / total as f64).round_ties_even() as isize);
    let span = 1.max(total - visible);
    let start = ((height - thumb) as f64 * (offset as f64 / span as f64)).round_ties_even() as isize;
    for row in 0..height {
        marks.insert(row, if start <= row && row < start + thumb { SCROLL_THUMB } else { SCROLL_TRACK });
    }
    marks
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{score_item, Memo, PLACEHOLDER};
    use crate::fuzzy::{split_query, Term};
    use crate::items::build_items;
    use crate::textutil::{strip_ansi, visible_width};

    #[test]
    fn agy_has_its_own_color_role() {
        assert_eq!(agent_role(Some("agy")), "agent_agy");
        assert_eq!(agent_role(Some("AGY")), "agent_agy");
        assert!(crate::theme::DEFAULTS.iter().any(|(role, _)| *role == "agent_agy"));
    }

    fn theme() -> Theme {
        Theme::new(None, None, None)
    }

    fn items() -> Vec<Item> {
        build_items(&crate::testutil::snapshot())
    }

    fn row(positions: &[usize]) -> Row {
        Row { item: 0, positions: positions.to_vec(), field_positions: BTreeMap::new() }
    }

    fn draw(theme: &Theme, item: &Item, row: &Row, width: isize, selected: bool, age: Option<f64>) -> String {
        render_row(theme, item, row, width, selected, true, age)
    }

    /// `Row(item, found[1].get(0, ()), found[1])` for a query.
    fn matched_row(item: &Item, query: &str) -> Row {
        let terms: Vec<Term> = split_query(query).iter().map(|t| Term::new(t)).collect();
        let mut memo = Memo::default();
        let (_, found) = score_item(&terms, item, &mut memo).expect("query matches");
        Row { item: 0, positions: found.get(&0).cloned().unwrap_or_default(), field_positions: found }
    }

    fn has_age(text: &str) -> bool {
        // r"\d+[hms]\b"
        let chars: Vec<char> = text.chars().collect();
        (1..chars.len()).any(|i| {
            chars[i - 1].is_ascii_digit()
                && matches!(chars[i], 'h' | 'm' | 's')
                && chars.get(i + 1).map(|c| !(c.is_alphanumeric() || *c == '_')).unwrap_or(true)
        })
    }

    // -- LayoutTest ---------------------------------------------------------

    #[test]
    fn preview_only_appears_when_there_is_room() {
        let narrow = compute_layout(70, 20, true);
        let wide = compute_layout(140, 20, true);
        assert_eq!(narrow.preview_width, 0);
        assert!(wide.preview_width > 0);
        assert_eq!(wide.list_width + wide.preview_width + 3, wide.inner_width);
    }

    #[test]
    fn disabled_preview_gives_the_list_everything() {
        let layout = compute_layout(140, 20, false);
        assert_eq!(layout.preview_width, 0);
        assert_eq!(layout.list_width, layout.inner_width);
    }

    #[test]
    fn tiny_popups_still_render_a_list() {
        let layout = compute_layout(20, 3, true);
        assert!(layout.list_height >= 1);
        assert!(!layout.rules);
    }

    // -- ContentTest (render-level parts; the full-frame checks need Bar) ---

    #[test]
    fn query_is_echoed_in_the_input_row() {
        let row = render_input(&theme(), "week", 4, 108, PLACEHOLDER, "everything", PROMPT);
        assert!(row.contains("week"));
    }

    #[test]
    fn placeholder_appears_only_when_empty() {
        let empty = render_input(&theme(), "", 0, 108, PLACEHOLDER, "everything", PROMPT);
        let typed = render_input(&theme(), "week", 4, 108, PLACEHOLDER, "everything", PROMPT);
        assert!(empty.contains("jump to a tab"));
        assert!(!typed.contains("jump to a tab"));
    }

    #[test]
    fn top_bar_highlights_the_active_filter() {
        let accent = theme().fg("accent").to_string();
        let all = render_input(&theme(), "", 0, 108, PLACEHOLDER, "everything", PROMPT);
        let agents = render_input(&theme(), "", 0, 108, PLACEHOLDER, "@ agents", PROMPT);
        assert!(all.contains(&format!("{} everything ", accent)));
        assert!(strip_ansi(&all).contains("everything"));
        assert!(agents.contains(&format!("{} @ agents ", accent)));
        assert!(strip_ansi(&agents).contains("@ agents"));
    }

    #[test]
    fn scoped_empty_state_names_the_active_filter() {
        for filter_name in ["@ agents", "$ shells", "! needs you"] {
            let theme = theme();
            let rendered = render_empty_state(&theme, 60, "", true, filter_name).join("\n");
            let message = strip_ansi(&rendered);
            assert!(message.contains(&format!("nothing in {} filter", filter_name)));
            assert!(!message.contains("this filter"));
            assert!(!rendered.contains("\x1b[7m"));
            let start = rendered.find(theme.fg("accent")).unwrap();
            let end = start + rendered[start..].find("\x1b[0m").unwrap();
            assert!(rendered[start..end].contains(filter_name));
            assert!(rendered[end..].contains(" filter"));
        }
    }

    #[test]
    fn empty_state_when_nothing_matches() {
        let lines = render_empty_state(&theme(), 106, "zzzqqq", true, "");
        assert!(lines.join("\n").contains("no matches"));
    }

    // -- RowTest ------------------------------------------------------------

    #[test]
    fn row_is_padded_to_exactly_the_width() {
        let theme = theme();
        for width in (20..120).step_by(7) {
            for item in &items() {
                let r = draw(&theme, item, &row(&[]), width, false, None);
                assert_eq!(visible_width(&r) as isize, width, "width {}, {:?}", width, item.title);
            }
        }
    }

    #[test]
    fn selected_row_is_padded_too() {
        let (theme, items) = (theme(), items());
        for width in [30, 60, 100] {
            assert_eq!(visible_width(&draw(&theme, &items[0], &row(&[]), width, true, None)) as isize, width);
        }
    }

    #[test]
    fn wide_characters_do_not_overflow() {
        let theme = theme();
        let mut item = items().remove(0);
        item.title = "日本語のタブ名前がとても長い場合のテスト".repeat(3);
        for width in [24, 40, 80] {
            assert_eq!(visible_width(&draw(&theme, &item, &row(&[]), width, false, None)) as isize, width);
        }
    }

    #[test]
    fn highlights_stay_inside_the_visible_title() {
        let item = items().remove(0);
        assert_eq!(visible_width(&draw(&theme(), &item, &row(&[0, 1, 2]), 60, false, None)), 60);
    }

    #[test]
    fn a_summary_match_is_highlighted_where_the_summary_is_drawn() {
        let item = items().into_iter().find(|i| !i.detail.is_empty()).unwrap();
        let first = item.detail.split_whitespace().next().unwrap().to_string();
        let r = matched_row(&item, &first);
        assert!(draw(&theme(), &item, &r, 120, false, None).contains("\x1b[4m"));
    }

    #[test]
    fn a_meta_match_is_highlighted_and_the_row_keeps_its_width() {
        let theme = theme();
        let item = items().into_iter().find(|i| i.agent_name.as_deref().is_some_and(|n| !n.is_empty())).unwrap();
        let name = item.agent_name.clone().unwrap();
        let r = matched_row(&item, &name);
        let rendered = draw(&theme, &item, &r, 120, false, None);
        assert!(rendered.contains(&format!("\x1b[4m{}{}", theme.fg("match"), name)));
        for width in (20..130).step_by(3) {
            assert_eq!(visible_width(&draw(&theme, &item, &r, width, false, Some(90.0))) as isize, width, "width {}", width);
        }
    }

    #[test]
    fn matched_characters_are_underlined_and_the_rest_is_not() {
        let theme = theme();
        let mut item = items().remove(0);
        item.title = "week loading".into();
        let r = draw(&theme, &item, &row(&[0, 1, 2, 3]), 60, false, None);
        assert!(r.contains(&format!("\x1b[4m{}week", theme.fg("match"))));
        assert!(!r.contains(&format!("\x1b[4m{} loading", theme.fg("match"))));
        assert_eq!(visible_width(&r), 60);
    }

    #[test]
    fn the_summary_follows_the_tab_name_when_there_is_room() {
        let item = items().into_iter().find(|i| !i.detail.is_empty()).unwrap();
        let plain = strip_ansi(&draw(&theme(), &item, &row(&[]), 120, false, None));
        assert!(plain.contains(&format!("{} — {}", item.title, item.detail)));
    }

    #[test]
    fn a_narrow_row_drops_the_summary_and_keeps_its_width() {
        let theme = theme();
        let item = items().into_iter().find(|i| !i.detail.is_empty()).unwrap();
        for width in (20..120).step_by(3) {
            assert_eq!(visible_width(&draw(&theme, &item, &row(&[]), width, false, None)) as isize, width, "width {}", width);
        }
        let narrow = strip_ansi(&draw(&theme, &item, &row(&[]), 34, false, None));
        assert!(!narrow.contains(&item.detail));
    }

    #[test]
    fn running_time_joins_the_meta_without_overflowing() {
        let theme = theme();
        let item = items().remove(0);
        for width in (20..120).step_by(7) {
            assert_eq!(visible_width(&draw(&theme, &item, &row(&[]), width, false, Some(7440.0))) as isize, width);
        }
        assert!(draw(&theme, &item, &row(&[]), 90, false, Some(7440.0)).contains("2h04m"));
    }

    #[test]
    fn context_is_not_repeated_when_the_summary_already_shows_it() {
        let mut item = items().into_iter().find(|i| !i.detail.is_empty()).unwrap();
        item.detail = "pacebeats".into();
        item.subtitle = "/Users/dev/workspace/pacebeats".into();
        item.workspace_label = "pacebeats".into();
        let plain = strip_ansi(&draw(&theme(), &item, &row(&[]), 120, false, None));
        assert_eq!(plain.matches("pacebeats").count(), 1);
    }

    #[test]
    fn context_is_not_repeated_when_it_is_the_title() {
        let mut item = items().into_iter().find(|i| i.agent.as_deref() == Some("codex")).unwrap();
        item.title = "erestor".into();
        item.detail = String::new();
        item.subtitle = "/Users/dev/workspace/erestor".into();
        item.workspace_label = "erestor".into();
        let plain = strip_ansi(&draw(&theme(), &item, &row(&[]), 120, false, None));
        assert_eq!(plain.matches("erestor").count(), 1);
    }

    #[test]
    fn agent_labels_have_distinct_colors() {
        let theme = theme();
        let expected =
            [("claude", "agent_claude"), ("codex", "agent_codex"), ("gemini", "agent_gemini"), ("kimi", "agent_kimi")];
        let mut item = items()
            .into_iter()
            .find(|i| i.agent.as_deref() == Some("claude") && i.agent_name.as_deref().unwrap_or("").is_empty())
            .unwrap();
        for (agent, role) in expected {
            item.agent = Some(agent.into());
            let rendered = draw(&theme, &item, &row(&[]), 120, false, None);
            assert!(rendered.contains(&format!("{}{}", theme.fg(role), agent)), "{}", agent);
        }
        let colors: std::collections::HashSet<&str> = expected.iter().map(|(_, role)| theme.fg(role)).collect();
        assert_eq!(colors.len(), expected.len());
    }

    #[test]
    fn named_agents_use_their_underlying_agent_color() {
        let theme = theme();
        let item = items().into_iter().find(|i| i.agent_name.as_deref().is_some_and(|n| !n.is_empty())).unwrap();
        let rendered = draw(&theme, &item, &row(&[]), 120, false, None);
        assert!(rendered.contains(&format!("{}@battery", theme.fg("agent_claude"))));
    }

    #[test]
    fn a_pane_with_no_reading_shows_no_time() {
        let item = items().remove(0);
        let plain = strip_ansi(&draw(&theme(), &item, &row(&[]), 90, false, None));
        assert!(!has_age(&plain), "{:?}", plain);
    }

    #[test]
    fn working_rows_show_a_steady_glyph() {
        let item = items().into_iter().find(|i| i.status == "working").unwrap();
        assert!(strip_ansi(&draw(&theme(), &item, &row(&[]), 60, false, None)).contains("◐ "));
    }

    // -- WidgetTest ---------------------------------------------------------

    #[test]
    fn input_row_fits() {
        for width in [20, 40, 80, 120] {
            let r = render_input(&theme(), "hello", 5, width, "placeholder text", "@ agents", PROMPT);
            assert!(visible_width(&r) as isize <= width);
        }
    }

    #[test]
    fn rename_prompt_and_hint_fit() {
        for width in [20, 40, 80, 120] {
            let r = render_input(&theme(), "new name", 8, width, "old name", "⏎ save · esc keep", "rename ❯");
            assert!(visible_width(&r) as isize <= width);
            assert!(strip_ansi(&r).contains("rename"));
        }
    }

    #[test]
    fn footer_drops_hints_before_overflowing() {
        let hints = [("↑↓", "move"), ("⏎", "jump"), ("⇥", "scope"), ("esc", "close")];
        for width in [16, 24, 40, 80] {
            assert!(visible_width(&render_footer(&theme(), width, &hints, "12/34")) as isize <= width);
        }
    }

    #[test]
    fn confirmation_fits_every_width() {
        let widths: Vec<isize> = (1..20).chain([24, 40, 60, 80, 120]).collect();
        for width in widths {
            for agents in [1, 3] {
                let r = render_confirm(&theme(), width, "library depth battery", agents);
                assert!(visible_width(&r) as isize <= width, "width {}", width);
            }
        }
    }

    #[test]
    fn confirmation_warns_when_more_than_one_agent_goes() {
        assert!(render_confirm(&theme(), 80, "server", 3).contains("3 agents"));
        assert!(!render_confirm(&theme(), 80, "server", 1).contains("agents"));
    }

    #[test]
    fn scrollbar_only_when_the_list_overflows() {
        assert!(scrollbar_column(5, 10, 0, 10).is_empty());
        let marks = scrollbar_column(50, 10, 0, 10);
        assert_eq!(marks.len(), 10);
        assert_eq!(marks[&0], "┃");
        assert_eq!(marks[&9], "│");
    }

    #[test]
    fn scrollbar_thumb_tracks_the_offset() {
        assert_eq!(scrollbar_column(50, 10, 40, 10)[&9], "┃");
    }
}
