//! herdr-bar: a Cmd+K style bar for jumping between Herdr tabs, panes, and agents.

mod age;
mod antigravity;
mod app;
mod auto_title;
mod cli;
mod client;
mod config;
mod fuzzy;
mod fxhash;
mod host;
mod icons;
mod items;
mod keys;
mod mru;
mod opener;
mod paint;
mod par;
mod proc;
#[cfg(all(test, unix))]
mod probe;
mod pyjson;
mod pyos;
mod pystr;
mod render;
mod renaming_guard;
mod sequences;
mod sha256;
mod tab_status;
mod term;
#[cfg(test)]
mod testutil;
mod textutil;
mod theme;
mod title_watcher;
mod unicode_tables;

fn main() {
    let args: Vec<String> = std::env::args_os().skip(1).map(|a| a.to_string_lossy().into_owned()).collect();
    let code = if args == ["--open"] {
        // The Windows open action only runs the Herdr CLI.
        opener::open_bar()
    } else {
        cli::main(args)
    };
    term::restore_console();
    std::process::exit(code);
}
