//! Antigravity CLI (`agy`) conversation titles.
//!
//! agy never puts its task in the terminal title, which stays the shell's. The
//! title it generates for a conversation is kept in
//! `~/.gemini/antigravity-cli/annotations/<conversation id>.pbtxt`, a protobuf
//! text-format file holding a `title:"..."` field. Herdr's `antigravity-cli`
//! integration reports that conversation id as the pane's session id.

use crate::auto_title::is_session_id;
use std::io::Read;
use std::path::PathBuf;

/// Annotations are a few hundred bytes; never read more than this.
const MAX_ANNOTATION: u64 = 64 * 1024;

fn annotations_dir() -> PathBuf {
    PathBuf::from(crate::pyos::expanduser("~/.gemini/antigravity-cli")).join("annotations")
}

/// The title agy generated for `session_id`, or "" when there is none yet.
pub fn title(session_id: &str) -> String {
    // The id becomes a file name: only a UUID may, so it cannot leave the directory.
    if !is_session_id(session_id) {
        return String::new();
    }
    let path = annotations_dir().join(format!("{}.pbtxt", session_id));
    let Ok(file) = std::fs::File::open(path) else { return String::new() };
    let mut data = Vec::new();
    if file.take(MAX_ANNOTATION).read_to_end(&mut data).is_err() {
        return String::new();
    }
    parse_title(&data).unwrap_or_default()
}

/// The value of the top-level `title` string field in protobuf text format.
/// Fields inside nested messages, and fields whose name merely ends in
/// `title`, are skipped.
pub fn parse_title(data: &[u8]) -> Option<String> {
    let mut depth = 0usize;
    let mut at = 0;
    while at < data.len() {
        match data[at] {
            b'"' | b'\'' => at = parse_string(data, at)?.1,
            b'#' => {
                while at < data.len() && data[at] != b'\n' {
                    at += 1;
                }
            }
            b'{' | b'<' => {
                depth += 1;
                at += 1;
            }
            b'}' | b'>' => {
                depth = depth.saturating_sub(1);
                at += 1;
            }
            b't' if depth == 0 && data[at..].starts_with(b"title") && (at == 0 || !is_ident(data[at - 1])) => {
                let mut next = at + b"title".len();
                if data.get(next).copied().is_some_and(is_ident) {
                    at = next;
                    continue;
                }
                next = skip_space(data, next);
                if data.get(next) != Some(&b':') {
                    at = next;
                    continue;
                }
                next = skip_space(data, next + 1);
                if !matches!(data.get(next), Some(b'"' | b'\'')) {
                    at = next;
                    continue;
                }
                // Adjacent literals concatenate, as in C.
                let mut bytes = Vec::new();
                while matches!(data.get(next), Some(b'"' | b'\'')) {
                    let (part, end) = parse_string(data, next)?;
                    bytes.extend(part);
                    next = skip_space(data, end);
                }
                return Some(String::from_utf8_lossy(&bytes).into_owned());
            }
            _ => at += 1,
        }
    }
    None
}

fn is_ident(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn skip_space(data: &[u8], mut at: usize) -> usize {
    while at < data.len() && data[at].is_ascii_whitespace() {
        at += 1;
    }
    at
}

/// The bytes of the quoted literal starting at `start`, and the index after
/// its closing quote. None when it is not terminated on the same line.
fn parse_string(data: &[u8], start: usize) -> Option<(Vec<u8>, usize)> {
    let quote = data[start];
    let mut out = Vec::new();
    let mut at = start + 1;
    loop {
        let byte = *data.get(at)?;
        at += 1;
        match byte {
            b'\n' => return None,
            b if b == quote => return Some((out, at)),
            b'\\' => {
                let escape = *data.get(at)?;
                at += 1;
                match escape {
                    b'n' => out.push(b'\n'),
                    b't' => out.push(b'\t'),
                    b'r' => out.push(b'\r'),
                    b'a' => out.push(0x07),
                    b'b' => out.push(0x08),
                    b'f' => out.push(0x0c),
                    b'v' => out.push(0x0b),
                    b'0'..=b'7' => {
                        let mut value = u32::from(escape - b'0');
                        for _ in 0..2 {
                            match data.get(at) {
                                Some(&digit @ b'0'..=b'7') => {
                                    value = value * 8 + u32::from(digit - b'0');
                                    at += 1;
                                }
                                _ => break,
                            }
                        }
                        out.push(value as u8);
                    }
                    b'x' | b'X' => {
                        let (value, used) = hex(data, at, 2)?;
                        at += used;
                        out.push(value as u8);
                    }
                    b'u' | b'U' => {
                        let (value, used) = hex(data, at, if escape == b'u' { 4 } else { 8 })?;
                        at += used;
                        let mut buffer = [0u8; 4];
                        out.extend(char::from_u32(value).unwrap_or('\u{fffd}').encode_utf8(&mut buffer).as_bytes());
                    }
                    other => out.push(other),
                }
            }
            other => out.push(other),
        }
    }
}

/// Up to `max` hex digits at `at` (all of them for `\u` and `\U`), with the
/// number of digits used.
fn hex(data: &[u8], at: usize, max: usize) -> Option<(u32, usize)> {
    let mut value = 0u32;
    let mut used = 0;
    while used < max {
        let Some(digit) = data.get(at + used).and_then(|b| (*b as char).to_digit(16)) else { break };
        value = value * 16 + digit;
        used += 1;
    }
    let exact = max == 4 || max == 8;
    if used == 0 || (exact && used != max) {
        return None;
    }
    Some((value, used))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(text: &str) -> Option<String> {
        parse_title(text.as_bytes())
    }

    #[test]
    fn titles() {
        for (text, expected) in [
            (r#"title:"Convert Markdown To HTML Page""#, Some("Convert Markdown To HTML Page")),
            ("title:\"一比一复刻源码修改\"", Some("一比一复刻源码修改")),
            ("  title : 'Fix login'\n", Some("Fix login")),
            ("id: \"x\"\ntitle: \"Fix login\"\nsummary: \"ignored\"", Some("Fix login")),
            (r#"title: "Fix " 'login'"#, Some("Fix login")),
            (r#"title: "say \"hi\"\\now""#, Some("say \"hi\"\\now")),
            (r#"title: "\344\270\255\346\226\207""#, Some("中文")),
            (r#"title: "\x41é\U0001F600""#, Some("Aé😀")),
            (r#"title: """#, Some("")),
        ] {
            assert_eq!(parsed(text).as_deref(), expected, "{}", text);
        }
    }

    #[test]
    fn only_the_top_level_title_field_counts() {
        for text in [
            "",
            "subtitle: \"no\"",
            "title_hint: \"no\"",
            "meta { title: \"nested\" }",
            "meta < title: \"nested\" >",
            "# title: \"comment\"",
            "summary: \"title: no\"",
            "title: 3",
            "title \"no colon\"",
            "title: \"unterminated",
            "title: \"broken\nline\"",
        ] {
            assert_eq!(parsed(text), None, "{}", text);
        }
        assert_eq!(parsed("meta { title: \"nested\" }\ntitle: \"top\"").as_deref(), Some("top"));
    }
}
