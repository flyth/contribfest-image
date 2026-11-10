//! Image releases: discovery on GitHub, resumable verified downloads, and the
//! releases already stored in the app data folder.
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

/// Guest contract this launcher understands; must match `GUEST_API` in scripts/build-image.py.
pub const GUEST_API: u32 = 1;
pub const RELEASES_URL: &str =
    "https://api.github.com/repos/flyth/contribfest-image/releases?per_page=100";
const TAG_PREFIX: &str = "images-v";

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq)]
pub struct FileEntry {
    pub size: u64,
    pub sha256: String,
}

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq)]
pub struct Variant {
    pub chain: Vec<String>,
    pub support: Vec<String>,
}

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq)]
pub struct Manifest {
    pub guest_api: u32,
    pub release: String,
    pub arch: String,
    #[serde(default)]
    pub versions: BTreeMap<String, String>,
    pub files: BTreeMap<String, FileEntry>,
    pub variants: BTreeMap<String, Variant>,
}

pub fn manifest_name(arch: &str) -> String {
    format!("manifest-{arch}.json")
}

pub fn safe_name(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.contains(['/', '\\', ':', '\0'])
        && !name.starts_with('.')
}

pub fn parse_manifest(text: &str, arch: &str) -> Result<Manifest, String> {
    let manifest: Manifest =
        serde_json::from_str(text).map_err(|e| format!("Invalid image manifest: {e}"))?;
    if manifest.arch != arch {
        return Err(format!(
            "Image manifest is for {}, not {arch}.",
            manifest.arch
        ));
    }
    if manifest.guest_api != GUEST_API {
        return Err(format!(
            "Image release {} needs a different launcher version (guest API {}, this launcher supports {GUEST_API}).",
            manifest.release, manifest.guest_api
        ));
    }
    for (name, entry) in &manifest.files {
        if !safe_name(name) {
            return Err(format!("Image manifest has an unsafe file name: {name:?}"));
        }
        if entry.sha256.len() != 64 || !entry.sha256.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(format!("Image manifest has an invalid checksum for {name}"));
        }
    }
    for (name, variant) in &manifest.variants {
        if variant.chain.is_empty() {
            return Err(format!("Image variant {name} has no disks."));
        }
        for file in variant.chain.iter().chain(&variant.support) {
            if !manifest.files.contains_key(file) {
                return Err(format!("Image variant {name} needs unlisted file {file}"));
            }
        }
    }
    Ok(manifest)
}

impl Manifest {
    pub fn variant(&self, variant: &str) -> Result<&Variant, String> {
        self.variants
            .get(variant)
            .ok_or_else(|| format!("Release {} has no {variant} variant.", self.release))
    }

    /// Files a variant needs: its disk chain from base to top, then support files.
    pub fn variant_files(&self, variant: &str) -> Result<Vec<String>, String> {
        let v = self.variant(variant)?;
        Ok(v.chain.iter().chain(&v.support).cloned().collect())
    }

    pub fn variant_size(&self, variant: &str) -> u64 {
        self.variant_files(variant)
            .map(|files| files.iter().map(|f| self.files[f].size).sum())
            .unwrap_or(0)
    }

    /// The disk a session overlay is backed by.
    pub fn top(&self, variant: &str) -> Result<String, String> {
        Ok(self.variant(variant)?.chain.last().unwrap().clone())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Release {
    pub tag: String,
    pub assets: BTreeMap<String, String>,
}

/// Numeric parts of `images-vX.Y.Z`, so releases sort as versions, not text.
pub fn tag_key(tag: &str) -> Vec<u64> {
    tag.trim_start_matches(TAG_PREFIX)
        .split(|c: char| !c.is_ascii_digit())
        .filter(|part| !part.is_empty())
        .map(|part| part.parse().unwrap_or(0))
        .collect()
}

pub fn releases_from_api(json: &serde_json::Value) -> Vec<Release> {
    let mut releases: Vec<Release> = json
        .as_array()
        .into_iter()
        .flatten()
        .filter(|r| r["draft"] != true && r["prerelease"] != true)
        .filter_map(|r| {
            let tag = r["tag_name"].as_str()?;
            tag.starts_with(TAG_PREFIX).then(|| Release {
                tag: tag.into(),
                assets: r["assets"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|a| {
                        Some((
                            a["name"].as_str()?.to_owned(),
                            a["browser_download_url"].as_str()?.to_owned(),
                        ))
                    })
                    .collect(),
            })
        })
        .collect();
    releases.sort_by_key(|r| std::cmp::Reverse(tag_key(&r.tag)));
    releases
}

/// The newest release whose manifest this launcher supports and whose files are all uploaded.
pub fn pick_release(
    releases: &[Release],
    arch: &str,
    fetch: impl Fn(&str) -> Result<String, String>,
) -> Result<(Release, Manifest), String> {
    for release in releases {
        let Some(url) = release.assets.get(&manifest_name(arch)) else {
            continue;
        };
        let Ok(manifest) = fetch(url).and_then(|text| parse_manifest(&text, arch)) else {
            continue;
        };
        if manifest
            .files
            .keys()
            .all(|name| release.assets.contains_key(name))
        {
            return Ok((release.clone(), manifest));
        }
    }
    Err(format!(
        "No compatible {arch} image release found. If newer images exist, update the app to use them."
    ))
}

pub fn client() -> Result<Client, String> {
    Client::builder()
        .user_agent("ig-demo-launcher")
        .connect_timeout(Duration::from_secs(30))
        .timeout(None::<Duration>)
        .build()
        .map_err(|e| e.to_string())
}

/// Client for the release list and manifests, which must never hang the catalog.
pub fn api_client() -> Result<Client, String> {
    Client::builder()
        .user_agent("ig-demo-launcher")
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|e| e.to_string())
}

pub fn fetch_text(client: &Client, url: &str) -> Result<String, String> {
    client
        .get(url)
        .header("Accept", "application/octet-stream, application/json")
        .send()
        .and_then(|r| r.error_for_status())
        .and_then(|r| r.text())
        .map_err(|e| e.to_string())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0; 1 << 20];
    loop {
        let read = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

fn file_size(path: &Path) -> u64 {
    fs::metadata(path).map(|m| m.len()).unwrap_or(0)
}

/// Download one manifest file into `dir`, resuming `<name>.part` and verifying its SHA256.
/// `progress` receives byte counts as they become available (including already-present bytes).
pub fn download_file(
    client: &Client,
    url: &str,
    dir: &Path,
    name: &str,
    entry: &FileEntry,
    cancel: &AtomicBool,
    progress: &mut dyn FnMut(u64),
) -> Result<(), String> {
    if !safe_name(name) {
        return Err(format!("Refusing unsafe file name {name:?}"));
    }
    let target = dir.join(name);
    // Files only get their final name after verification, so a size match is enough.
    if file_size(&target) == entry.size {
        progress(entry.size);
        return Ok(());
    }
    let part = dir.join(format!("{name}.part"));
    let mut have = file_size(&part);
    if have > entry.size {
        have = 0;
    }
    let mut out = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&part)
        .map_err(|e| e.to_string())?;
    if have < entry.size {
        let mut request = client.get(url);
        if have > 0 {
            request = request.header("Range", format!("bytes={have}-"));
        }
        let mut response = request.send().map_err(|e| e.to_string())?;
        match response.status().as_u16() {
            206 if have > 0 => {}
            200 => {
                // The server ignored the range: start over rather than append a second copy.
                have = 0;
                out.set_len(0).map_err(|e| e.to_string())?;
            }
            code => {
                drop(out);
                let _ = fs::remove_file(&part);
                return Err(format!("Downloading {name} failed: HTTP {code}"));
            }
        }
        progress(have);
        let mut buffer = vec![0; 1 << 20];
        loop {
            if cancel.load(Ordering::Relaxed) {
                return Err("Download cancelled.".into());
            }
            let read = response
                .read(&mut buffer)
                .map_err(|e| format!("Downloading {name} was interrupted: {e}"))?;
            if read == 0 {
                break;
            }
            out.write_all(&buffer[..read]).map_err(|e| e.to_string())?;
            have += read as u64;
            progress(read as u64);
            if have > entry.size {
                break;
            }
        }
    } else {
        progress(have);
    }
    out.sync_all().map_err(|e| e.to_string())?;
    drop(out);
    if file_size(&part) != entry.size || !sha256_file(&part)?.eq_ignore_ascii_case(&entry.sha256) {
        let _ = fs::remove_file(&part);
        return Err(format!(
            "{name} failed its checksum. The partial download was removed; try again."
        ));
    }
    fs::rename(&part, &target).map_err(|e| e.to_string())
}

#[derive(Clone, Debug, Serialize)]
pub struct Installed {
    pub release: String,
    pub dir: PathBuf,
    pub manifest: Manifest,
    pub variants: Vec<String>,
}

impl Installed {
    pub fn top_path(&self, variant: &str) -> Result<PathBuf, String> {
        Ok(self.dir.join(self.manifest.top(variant)?))
    }

    /// Bytes still missing for `variant` given what this folder holds.
    pub fn missing(&self, variant: &str) -> u64 {
        missing_bytes(&self.dir, &self.manifest, variant)
    }
}

pub fn missing_bytes(dir: &Path, manifest: &Manifest, variant: &str) -> u64 {
    manifest
        .variant_files(variant)
        .unwrap_or_default()
        .iter()
        .filter(|name| file_size(&dir.join(name)) != manifest.files[*name].size)
        .map(|name| manifest.files[name].size)
        .sum()
}

/// Releases stored under `images`, newest first. A variant counts once all its files are present.
pub fn installed(images: &Path, arch: &str) -> Vec<Installed> {
    let mut found: Vec<Installed> = fs::read_dir(images)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|entry| {
            let dir = entry.path();
            let text = fs::read_to_string(dir.join(manifest_name(arch))).ok()?;
            let manifest = parse_manifest(&text, arch).ok()?;
            let variants = manifest
                .variants
                .keys()
                .filter(|v| missing_bytes(&dir, &manifest, v) == 0)
                .cloned()
                .collect();
            Some(Installed {
                release: manifest.release.clone(),
                dir,
                manifest,
                variants,
            })
        })
        .collect();
    found.sort_by_key(|i| std::cmp::Reverse(tag_key(&i.release)));
    found
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::BufRead,
        net::TcpListener,
        sync::{Arc, Mutex},
    };

    fn sha(data: &[u8]) -> String {
        Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    fn manifest_json(arch: &str, guest_api: u32, files: &[(&str, &[u8])]) -> String {
        let entries: serde_json::Map<String, serde_json::Value> = files
            .iter()
            .map(|(n, d)| {
                (
                    n.to_string(),
                    serde_json::json!({"size": d.len(), "sha256": sha(d)}),
                )
            })
            .collect();
        serde_json::json!({
            "guest_api": guest_api, "release": "images-v1.0.0", "arch": arch,
            "versions": {"IG_VERSION": "v0.57.0"},
            "files": entries,
            "variants": {
                "minimal": {"chain": ["base.qcow2"], "support": ["seed.qcow2"]},
                "observability": {"chain": ["base.qcow2", "layer.qcow2"], "support": ["seed.qcow2"]}
            }
        })
        .to_string()
    }

    const FILES: [(&str, &[u8]); 3] = [
        ("base.qcow2", b"base disk"),
        ("layer.qcow2", b"layer!"),
        ("seed.qcow2", b"seed"),
    ];

    struct Scratch(PathBuf);
    impl Scratch {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("ig-images-{}", uuid::Uuid::new_v4().simple()));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    /// Serves one scripted response per connection; records each request's Range header.
    fn serve(responses: Vec<Vec<u8>>) -> (String, Arc<Mutex<Vec<Option<String>>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/file", listener.local_addr().unwrap());
        let ranges = Arc::new(Mutex::new(vec![]));
        let seen = ranges.clone();
        std::thread::spawn(move || {
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let mut reader = std::io::BufReader::new(stream.try_clone().unwrap());
                let mut range = None;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line.trim().is_empty() {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("range:") {
                        range = Some(value.trim().to_owned());
                    }
                }
                seen.lock().unwrap().push(range);
                stream.write_all(&response).unwrap();
            }
        });
        (url, ranges)
    }

    fn http(status: &str, body: &[u8]) -> Vec<u8> {
        let mut out = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        )
        .into_bytes();
        out.extend_from_slice(body);
        out
    }

    fn test_client() -> Client {
        Client::builder().no_proxy().build().unwrap()
    }

    fn entry(data: &[u8]) -> FileEntry {
        FileEntry {
            size: data.len() as u64,
            sha256: sha(data),
        }
    }

    #[test]
    fn manifest_rejects_paths_other_arch_and_newer_guest_api() {
        assert!(parse_manifest(&manifest_json("arm64", GUEST_API, &FILES), "arm64").is_ok());
        assert!(parse_manifest(&manifest_json("amd64", GUEST_API, &FILES), "arm64").is_err());
        let newer = parse_manifest(&manifest_json("arm64", GUEST_API + 1, &FILES), "arm64");
        assert!(newer.unwrap_err().contains("different launcher"));
        for bad in ["../evil", "a/b", "a\\b", "..", ".hidden", "C:x"] {
            let mut files = FILES.to_vec();
            files.push((bad, b"x"));
            let error = parse_manifest(&manifest_json("arm64", GUEST_API, &files), "arm64");
            assert!(error.unwrap_err().contains("unsafe"), "{bad} accepted");
        }
        let missing = manifest_json("arm64", GUEST_API, &FILES[..2]);
        assert!(parse_manifest(&missing, "arm64")
            .unwrap_err()
            .contains("unlisted"));
    }

    #[test]
    fn variant_files_are_chain_then_support_and_sizes_add_up() {
        let m = parse_manifest(&manifest_json("arm64", GUEST_API, &FILES), "arm64").unwrap();
        assert_eq!(
            m.variant_files("observability").unwrap(),
            ["base.qcow2", "layer.qcow2", "seed.qcow2"]
        );
        assert_eq!(m.variant_size("minimal"), 9 + 4);
        assert_eq!(m.variant_size("observability"), 9 + 6 + 4);
        assert_eq!(m.top("minimal").unwrap(), "base.qcow2");
        assert_eq!(m.top("observability").unwrap(), "layer.qcow2");
        assert!(m.top("other").is_err());
    }

    fn release(tag: &str, names: &[&str]) -> serde_json::Value {
        serde_json::json!({
            "tag_name": tag, "draft": false, "prerelease": false,
            "assets": names.iter().map(|n| serde_json::json!({
                "name": n, "browser_download_url": format!("https://x/{tag}/{n}")
            })).collect::<Vec<_>>()
        })
    }

    #[test]
    fn pick_release_skips_incompatible_and_non_image_releases() {
        let all = [
            "base.qcow2",
            "layer.qcow2",
            "seed.qcow2",
            "manifest-arm64.json",
        ];
        let mut draft = release("images-v9.0.0", &all);
        draft["draft"] = true.into();
        let api = serde_json::json!([
            release("v2.0.0", &all),
            draft,
            release("images-v1.2.0", &all),
            release("images-v1.10.0", &all),
            release("images-v1.11.0", &all[1..]),
            release("images-v1.0.0", &all),
        ]);
        let releases = releases_from_api(&api);
        assert_eq!(
            releases.iter().map(|r| r.tag.as_str()).collect::<Vec<_>>(),
            [
                "images-v1.11.0",
                "images-v1.10.0",
                "images-v1.2.0",
                "images-v1.0.0"
            ]
        );
        let (picked, manifest) = pick_release(&releases, "arm64", |url| {
            Ok(if url.contains("images-v1.10.0") {
                manifest_json("arm64", GUEST_API + 1, &FILES)
            } else {
                manifest_json("arm64", GUEST_API, &FILES)
            })
        })
        .unwrap();
        // 1.11.0 lacks its manifest, 1.10.0 needs a newer launcher.
        assert_eq!(picked.tag, "images-v1.2.0");
        assert_eq!(manifest.guest_api, GUEST_API);
    }

    #[test]
    fn pick_release_errors_when_nothing_is_compatible() {
        let releases = releases_from_api(&serde_json::json!([release(
            "images-v1.0.0",
            &["manifest-arm64.json"]
        )]));
        let error = pick_release(&releases, "arm64", |_| {
            Ok(manifest_json("arm64", GUEST_API + 1, &FILES))
        })
        .unwrap_err();
        assert!(error.contains("update the app"));
        assert!(pick_release(&[], "arm64", |_| unreachable!()).is_err());
    }

    #[test]
    fn download_resumes_partial_file_with_range() {
        let scratch = Scratch::new();
        let data = b"0123456789abcdef";
        fs::write(scratch.0.join("disk.qcow2.part"), &data[..6]).unwrap();
        let (url, ranges) = serve(vec![http("206 Partial Content", &data[6..])]);
        let mut seen = 0;
        download_file(
            &test_client(),
            &url,
            &scratch.0,
            "disk.qcow2",
            &entry(data),
            &AtomicBool::new(false),
            &mut |n| seen += n,
        )
        .unwrap();
        assert_eq!(fs::read(scratch.0.join("disk.qcow2")).unwrap(), data);
        assert!(!scratch.0.join("disk.qcow2.part").exists());
        assert_eq!(ranges.lock().unwrap()[0].as_deref(), Some("bytes=6-"));
        assert_eq!(seen, data.len() as u64);
    }

    #[test]
    fn download_restarts_when_server_ignores_range() {
        let scratch = Scratch::new();
        let data = b"0123456789abcdef";
        fs::write(scratch.0.join("disk.qcow2.part"), b"012345").unwrap();
        let (url, _) = serve(vec![http("200 OK", data)]);
        download_file(
            &test_client(),
            &url,
            &scratch.0,
            "disk.qcow2",
            &entry(data),
            &AtomicBool::new(false),
            &mut |_| {},
        )
        .unwrap();
        assert_eq!(fs::read(scratch.0.join("disk.qcow2")).unwrap(), data);
    }

    #[test]
    fn download_rejects_checksum_mismatch_and_removes_part() {
        let scratch = Scratch::new();
        let (url, _) = serve(vec![http("200 OK", b"tampered!!")]);
        let error = download_file(
            &test_client(),
            &url,
            &scratch.0,
            "disk.qcow2",
            &entry(b"genuine!!!"),
            &AtomicBool::new(false),
            &mut |_| {},
        )
        .unwrap_err();
        assert!(error.contains("checksum"));
        assert!(!scratch.0.join("disk.qcow2").exists());
        assert!(!scratch.0.join("disk.qcow2.part").exists());
    }

    #[test]
    fn download_skips_verified_files_and_honours_cancel() {
        let scratch = Scratch::new();
        fs::write(scratch.0.join("done.qcow2"), b"ready").unwrap();
        // No server: a verified file must not be fetched again.
        download_file(
            &test_client(),
            "http://127.0.0.1:9/never",
            &scratch.0,
            "done.qcow2",
            &entry(b"ready"),
            &AtomicBool::new(false),
            &mut |_| {},
        )
        .unwrap();
        let (url, _) = serve(vec![http("200 OK", b"abc")]);
        let error = download_file(
            &test_client(),
            &url,
            &scratch.0,
            "new.qcow2",
            &entry(b"abc"),
            &AtomicBool::new(true),
            &mut |_| {},
        )
        .unwrap_err();
        assert!(error.contains("cancelled"));
        assert!(!scratch.0.join("new.qcow2").exists());
    }

    #[test]
    fn installed_counts_only_complete_variants() {
        let scratch = Scratch::new();
        let older = scratch.0.join("images-v1.0.0");
        let newer = scratch.0.join("images-v1.2.0");
        for dir in [&older, &newer] {
            fs::create_dir(dir).unwrap();
            let text = manifest_json("arm64", GUEST_API, &FILES)
                .replace("images-v1.0.0", dir.file_name().unwrap().to_str().unwrap());
            fs::write(dir.join("manifest-arm64.json"), text).unwrap();
        }
        for (name, data) in FILES {
            fs::write(older.join(name), data).unwrap();
        }
        fs::write(newer.join("base.qcow2"), FILES[0].1).unwrap();
        fs::write(newer.join("seed.qcow2"), FILES[2].1).unwrap();
        fs::write(newer.join("layer.qcow2.part"), b"la").unwrap();
        let found = installed(&scratch.0, "arm64");
        assert_eq!(found[0].release, "images-v1.2.0");
        assert_eq!(found[0].variants, ["minimal"]);
        assert_eq!(found[0].missing("observability"), 6);
        assert_eq!(found[1].variants, ["minimal", "observability"]);
        assert_eq!(
            found[1].top_path("observability").unwrap(),
            older.join("layer.qcow2")
        );
        assert!(installed(&scratch.0, "amd64").is_empty());
    }
}
