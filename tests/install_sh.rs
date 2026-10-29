//! `scripts/install.sh`'s download path, against a `file://` release directory
//! and a fake binary (a shell script), so no cargo or network is needed.
//! Ported from the Python release's tests/test_install_sh.py.
#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

const ROOT: &str = env!("CARGO_MANIFEST_DIR");
static COUNTER: AtomicUsize = AtomicUsize::new(0);

/// The first executable named `tool` on this process's PATH.
fn which(tool: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|dir| dir.join(tool)).find(|candidate| {
        fs::metadata(candidate).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    })
}

fn manifest_version() -> String {
    let text = fs::read_to_string(Path::new(ROOT).join("herdr-plugin.toml")).unwrap();
    let line = text.lines().find(|l| l.starts_with("version")).expect("no version in herdr-plugin.toml");
    line.split('"').nth(1).unwrap().to_string()
}

/// The asset name install.sh computes, from running its own function so the
/// two cannot drift apart.
fn asset_name(sh: &Path) -> Option<String> {
    let text = fs::read_to_string(Path::new(ROOT).join("scripts/install.sh")).unwrap();
    let start = text.find("asset_name() {").expect("asset_name in install.sh");
    let end = start + text[start..].find("\n}\n").expect("end of asset_name") + 3;
    let out = Command::new(sh).arg("-c").arg(format!("{}asset_name", &text[start..end])).output().ok()?;
    let name = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (out.status.success() && !name.is_empty()).then_some(name)
}

fn sha256_hex(file: &Path) -> String {
    let out = if let Some(tool) = which("sha256sum") {
        Command::new(tool).arg(file).output().unwrap()
    } else {
        Command::new(which("shasum").unwrap()).args(["-a", "256"]).arg(file).output().unwrap()
    };
    String::from_utf8_lossy(&out.stdout).split_whitespace().next().unwrap().to_string()
}

struct Setup {
    tmp: PathBuf,
    sh: PathBuf,
    asset: String,
    version: String,
    plugin: PathBuf,
    release: PathBuf,
    path_dir: PathBuf,
}

impl Drop for Setup {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.tmp);
    }
}

impl Setup {
    /// None, with a note on stderr, when this machine cannot run the test.
    fn new() -> Option<Setup> {
        let (Some(sh), Some(_)) = (which("sh"), which("curl")) else {
            eprintln!("skipped: needs sh and curl");
            return None;
        };
        if which("sha256sum").is_none() && which("shasum").is_none() {
            eprintln!("skipped: needs sha256sum or shasum");
            return None;
        }
        let Some(asset) = asset_name(&sh) else {
            eprintln!("skipped: no prebuilt asset for this machine");
            return None;
        };
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let tmp = std::env::temp_dir().join(format!("herdr-bar-install-{}-{}", std::process::id(), n));
        let _ = fs::remove_dir_all(&tmp);
        let plugin = tmp.join("plugin");
        let release = tmp.join("release");
        let path_dir = tmp.join("path");
        for dir in [plugin.join("scripts"), release.clone(), path_dir.clone()] {
            fs::create_dir_all(dir).unwrap();
        }
        fs::copy(Path::new(ROOT).join("scripts/install.sh"), plugin.join("scripts/install.sh")).unwrap();
        fs::copy(Path::new(ROOT).join("herdr-plugin.toml"), plugin.join("herdr-plugin.toml")).unwrap();
        // A PATH with the tools install.sh uses but no cargo, so a failed
        // download fails the install instead of building from source.
        for tool in [
            "curl", "sha256sum", "shasum", "awk", "sed", "head", "uname", "sysctl", "mktemp", "mkdir", "cp", "mv",
            "rm", "chmod", "dirname", "perl",
        ] {
            if let Some(found) = which(tool) {
                std::os::unix::fs::symlink(found, path_dir.join(tool)).unwrap();
            }
        }
        Some(Setup { tmp, sh, asset, version: manifest_version(), plugin, release, path_dir })
    }

    fn publish(&self, reported: &str, checksum: Option<&str>, asset: bool) {
        let body = format!("#!/bin/sh\necho '{reported}'\n");
        let file = self.release.join(&self.asset);
        fs::write(&file, &body).unwrap();
        let digest = checksum.map(str::to_string).unwrap_or_else(|| sha256_hex(&file));
        if !asset {
            fs::remove_file(&file).unwrap();
        }
        fs::write(self.release.join("SHA256SUMS"), format!("{digest}  {}\n", self.asset)).unwrap();
    }

    fn run_install(&self, base: Option<&str>) -> (bool, String) {
        let base = base.map(str::to_string).unwrap_or_else(|| format!("file://{}", self.release.display()));
        let out: Output = Command::new(&self.sh)
            .arg(self.plugin.join("scripts/install.sh"))
            .env_clear()
            .env("PATH", &self.path_dir)
            .env("HOME", &self.tmp)
            .env("TMPDIR", &self.tmp)
            .env("HERDR_BAR_DOWNLOAD_BASE", base)
            .stdin(Stdio::null())
            .output()
            .unwrap();
        (out.status.success(), String::from_utf8_lossy(&out.stderr).into_owned())
    }

    fn target(&self) -> PathBuf {
        self.plugin.join("bin/herdr-bar")
    }

    fn assert_clean_bin(&self) {
        let leftovers: Vec<String> = fs::read_dir(self.plugin.join("bin"))
            .map(|entries| {
                entries.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).collect()
            })
            .unwrap_or_default();
        let leftovers: Vec<&String> = leftovers.iter().filter(|n| *n != "herdr-bar").collect();
        assert!(leftovers.is_empty(), "left in bin/: {leftovers:?}");
    }
}

#[test]
fn good_asset_installs() {
    let Some(s) = Setup::new() else { return };
    s.publish(&format!("herdr-bar {}", s.version), None, true);
    let (ok, stderr) = s.run_install(None);
    assert!(ok, "{stderr}");
    assert!(stderr.contains("installed"), "{stderr}");
    let mode = fs::metadata(s.target()).expect("installed binary").permissions().mode();
    assert!(mode & 0o100 != 0, "not executable: {mode:o}");
    s.assert_clean_bin();
}

#[test]
fn checksum_mismatch_fails() {
    let Some(s) = Setup::new() else { return };
    s.publish(&format!("herdr-bar {}", s.version), Some(&"0".repeat(64)), true);
    let (ok, stderr) = s.run_install(None);
    assert!(!ok);
    assert!(stderr.contains("checksum mismatch"), "{stderr}");
    assert!(!s.target().exists());
    s.assert_clean_bin();
}

#[test]
fn missing_asset_fails() {
    let Some(s) = Setup::new() else { return };
    s.publish(&format!("herdr-bar {}", s.version), None, false);
    let (ok, stderr) = s.run_install(None);
    assert!(!ok);
    assert!(stderr.contains("download failed"), "{stderr}");
    assert!(stderr.contains("herdr plugin install rob-64/herdr-bar"), "{stderr}");
    assert!(!s.target().exists());
}

#[test]
fn wrong_version_fails() {
    let Some(s) = Setup::new() else { return };
    s.publish("herdr-bar 0.0.0", None, true);
    let (ok, stderr) = s.run_install(None);
    assert!(!ok);
    assert!(stderr.contains("reports 'herdr-bar 0.0.0'"), "{stderr}");
    assert!(!s.target().exists());
    s.assert_clean_bin();
}

#[test]
fn http_base_warns() {
    let Some(s) = Setup::new() else { return };
    // Nothing listens on port 9; only the warning matters.
    let (ok, stderr) = s.run_install(Some("http://127.0.0.1:9"));
    assert!(!ok);
    assert!(stderr.contains("warning: HERDR_BAR_DOWNLOAD_BASE is not https"), "{stderr}");
}
