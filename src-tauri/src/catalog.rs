//! Tauri commands for choosing, downloading and pruning image releases.
use crate::images::{self, Installed, Manifest, Release};
use crate::vm::architecture;
use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager};

#[derive(Default)]
pub struct Downloads {
    busy: AtomicBool,
    cancel: AtomicBool,
    latest: Mutex<Option<(Release, Manifest)>>,
}

fn images_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    Ok(crate::data_dir(app)?.join("images"))
}

fn installed_json(installed: &[Installed]) -> serde_json::Value {
    installed
        .iter()
        .map(|i| {
            let paths: serde_json::Map<_, _> = i
                .variants
                .iter()
                .filter_map(|v| Some((v.clone(), i.top_path(v).ok()?.to_string_lossy().into())))
                .collect();
            serde_json::json!({
                "release": i.release,
                "variants": i.variants,
                "versions": i.manifest.versions,
                "paths": paths,
            })
        })
        .collect()
}

fn latest_release() -> Result<(Release, Manifest), String> {
    let client = images::api_client()?;
    let text = images::fetch_text(&client, images::RELEASES_URL)
        .map_err(|e| format!("Cannot reach GitHub to list image releases: {e}"))?;
    let json = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    images::pick_release(&images::releases_from_api(&json), architecture(), |url| {
        images::fetch_text(&client, url)
    })
}

fn canonical(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

/// Which installed release and variant the last start used, compared as canonical paths
/// (on Windows the stored path carries a `\\?\` prefix).
fn selected(app: &tauri::AppHandle, installed: &[Installed]) -> serde_json::Value {
    let Ok(data) = crate::data_dir(app) else {
        return serde_json::Value::Null;
    };
    let last = std::fs::read_to_string(data.join("base-image.txt")).unwrap_or_default();
    if last.is_empty() {
        return serde_json::Value::Null;
    }
    let last = canonical(Path::new(&last));
    for item in installed {
        for variant in &item.variants {
            if item.top_path(variant).map(|p| canonical(&p)) == Ok(last.clone()) {
                return serde_json::json!({"release": item.release, "variant": variant});
            }
        }
    }
    serde_json::Value::Null
}

/// Installed releases plus, when `online`, the newest compatible release on GitHub
/// (or why it is unavailable). The offline form never touches the network.
#[tauri::command]
pub async fn image_catalog(
    app: tauri::AppHandle,
    online: bool,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let installed = images::installed(&images_dir(&app)?, architecture());
        let latest = if online {
            latest_release()
        } else {
            Err(String::new())
        };
        let (available, error) = match latest {
            Ok((release, manifest)) => {
                let local = installed.iter().find(|i| i.release == release.tag);
                let variants: serde_json::Map<_, _> = manifest
                    .variants
                    .keys()
                    .map(|v| {
                        let missing = local.map_or(manifest.variant_size(v), |i| i.missing(v));
                        (
                            v.clone(),
                            serde_json::json!({"size": manifest.variant_size(v), "missing": missing}),
                        )
                    })
                    .collect();
                let value = serde_json::json!({
                    "release": release.tag,
                    "versions": manifest.versions,
                    "variants": variants,
                });
                *app.state::<Downloads>().latest.lock().unwrap() = Some((release, manifest));
                (value, serde_json::Value::Null)
            }
            Err(e) if e.is_empty() => (serde_json::Value::Null, serde_json::Value::Null),
            Err(e) => (serde_json::Value::Null, e.into()),
        };
        Ok(serde_json::json!({
            "selected": selected(&app, &installed),
            "installed": installed_json(&installed),
            "available": available,
            "error": error,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

struct Busy<'a>(&'a AtomicBool);
impl Drop for Busy<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

/// Download one variant of the release last shown by `image_catalog`; returns its top disk.
#[tauri::command]
pub async fn download_variant(
    app: tauri::AppHandle,
    release: String,
    variant: String,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let downloads = app.state::<Downloads>();
        if downloads.busy.swap(true, Ordering::SeqCst) {
            return Err("A download is already running.".into());
        }
        let _busy = Busy(&downloads.busy);
        downloads.cancel.store(false, Ordering::SeqCst);
        let (found, manifest) = downloads
            .latest
            .lock()
            .unwrap()
            .clone()
            .filter(|(r, _)| r.tag == release)
            .ok_or("This release is no longer offered. Refresh and try again.")?;
        let dir = images_dir(&app)?.join(&found.tag);
        crate::vm::private_dir(&dir)?;
        std::fs::write(
            dir.join(images::manifest_name(architecture())),
            serde_json::to_string_pretty(&manifest).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        let total = manifest.variant_size(&variant);
        let mut done = 0;
        let mut last = Instant::now() - Duration::from_secs(1);
        for name in manifest.variant_files(&variant)? {
            let url = found
                .assets
                .get(&name)
                .ok_or_else(|| format!("{name} is missing from release {}", found.tag))?;
            images::download_file(
                &images::client()?,
                url,
                &dir,
                &name,
                &manifest.files[&name],
                &downloads.cancel,
                &mut |n| {
                    done += n;
                    if last.elapsed() >= Duration::from_millis(250) {
                        last = Instant::now();
                        let _ = app.emit(
                            "image-progress",
                            serde_json::json!({"done": done, "total": total, "file": name}),
                        );
                    }
                },
            )?;
        }
        let _ = app.emit(
            "image-progress",
            serde_json::json!({"done": total, "total": total}),
        );
        Ok(dir.join(manifest.top(&variant)?).to_string_lossy().into())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub fn cancel_download(app: tauri::AppHandle) {
    app.state::<Downloads>()
        .cancel
        .store(true, Ordering::SeqCst);
}

/// Remove releases older than `keep` whose variants `keep` fully replaces,
/// never the folder holding the disk the last start used.
pub fn prune(images: &Path, keep: &str, in_use: &Path) -> Result<(), String> {
    if !images::safe_name(keep) {
        return Err(format!("Invalid release name {keep:?}"));
    }
    let all = images::installed(images, architecture());
    let Some(kept) = all
        .iter()
        .find(|i| i.dir.file_name() == Some(keep.as_ref()))
    else {
        return Ok(());
    };
    let in_use = canonical(in_use);
    for old in &all {
        let older = images::tag_key(&old.release) < images::tag_key(&kept.release);
        let replaced = old.variants.iter().all(|v| kept.variants.contains(v));
        if !older || !replaced || in_use.starts_with(canonical(&old.dir)) {
            continue;
        }
        std::fs::remove_dir_all(&old.dir)
            .map_err(|e| format!("Cannot remove {}: {e}", old.dir.display()))?;
    }
    Ok(())
}

#[tauri::command]
pub async fn prune_images(app: tauri::AppHandle, keep: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        // Never remove folders while a download may be writing into one.
        if app.state::<Downloads>().busy.load(Ordering::SeqCst) {
            return Ok(());
        }
        let data = crate::data_dir(&app)?;
        let in_use =
            PathBuf::from(std::fs::read_to_string(data.join("base-image.txt")).unwrap_or_default());
        prune(&data.join("images"), &keep, &in_use)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::Digest;

    fn release(root: &Path, tag: &str, observability: bool) {
        let dir = root.join(tag);
        std::fs::create_dir_all(&dir).unwrap();
        let arch = architecture();
        let all = [("base", b"b".as_slice()), ("layer", b"l".as_slice())];
        let files: serde_json::Map<_, _> = all
            .iter()
            .map(|(n, d)| {
                let digest: String = sha2::Sha256::digest(d)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect();
                (
                    n.to_string(),
                    serde_json::json!({"size": d.len(), "sha256": digest}),
                )
            })
            .collect();
        for (name, data) in all {
            if name == "base" || observability {
                std::fs::write(dir.join(name), data).unwrap();
            }
        }
        let manifest = serde_json::json!({
            "guest_api": images::GUEST_API, "release": tag, "arch": arch, "files": files,
            "variants": {
                "minimal": {"chain": ["base"], "support": []},
                "observability": {"chain": ["base", "layer"], "support": []}
            }
        });
        std::fs::write(dir.join(images::manifest_name(arch)), manifest.to_string()).unwrap();
    }

    #[test]
    fn prune_only_removes_older_replaced_releases_not_in_use() {
        let root = std::env::temp_dir().join(format!("ig-prune-{}", uuid::Uuid::new_v4().simple()));
        release(&root, "images-v1.0.0", true);
        release(&root, "images-v1.1.0", false);
        release(&root, "images-v1.2.0", false);
        release(&root, "images-v1.3.0", true);
        // Keeping 1.2.0 (minimal only) must keep 1.0.0's layer and the newer 1.3.0.
        prune(&root, "images-v1.2.0", Path::new("")).unwrap();
        assert!(root.join("images-v1.0.0").exists());
        assert!(!root.join("images-v1.1.0").exists());
        assert!(root.join("images-v1.3.0").exists());
        // The folder in use survives even when it is older and replaced.
        let in_use = root.join("images-v1.2.0").join("base");
        prune(&root, "images-v1.3.0", &in_use).unwrap();
        assert!(!root.join("images-v1.0.0").exists());
        assert!(root.join("images-v1.2.0").exists());
        for bad in ["", "..", "../x"] {
            assert!(prune(&root, bad, &in_use).is_err());
        }
        assert!(root.join("images-v1.3.0").exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}
