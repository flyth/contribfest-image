# Layered Images and In-App Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the demo VM as a minimal base disk plus an optional observability layer, and let the app download either variant from GitHub releases.

**Architecture:** The builder provisions the base as today (minus Headlamp/Prometheus/Grafana), then boots an overlay of it with an SMBIOS flag that makes a guest unit install the observability stack; the overlay is packaged with `qemu-img convert -B <base filename>`. A per-arch `manifest-<arch>.json` describes the release. The app gains an `images` module that lists `images-v*` releases, picks the newest with a supported `guest_api`, downloads with resume + sha256, and stores releases under `<data>/images/<tag>/`.

**Tech Stack:** Python 3 builder and guest API, bash, QEMU/qemu-img, Rust (Tauri 2, reqwest blocking with native-tls, sha2), vanilla JS UI.

**Spec:** `docs/superpowers/specs/2026-11-07-layered-images-design.md`

## Global Constraints

- Image release tags: `images-v*`. App tags stay `v*`.
- File names: `ig-minimal-<arch>.qcow2`, `ig-observability-<arch>.qcow2`, `ig-registry-seed-<arch>.qcow2`, `QEMU_EFI-arm64.fd`, `versions-<arch>.env`, `manifest-<arch>.json`, `SHA256SUMS-<arch>`.
- The layer's backing reference is exactly the bare base filename, format qcow2.
- `GUEST_API = 1` in both `scripts/build-image.py` and `src-tauri/src/images.rs`.
- Layer marker: `/opt/demo/layers/observability`. Status reply field: `"layers": [...]`.
- Provision trigger: SMBIOS type 11 string `io.inspektor-gadget.demo.provision=observability`.
- GitHub repo: `flyth/ig-launcher`. API: `https://api.github.com/repos/flyth/ig-launcher/releases?per_page=30`. User-Agent `ig-demo-launcher`.
- Manifest file names must be plain names (no `/`, `\`, `..`); anything else is rejected.
- Releases are published non-draft with `--latest=false`; the manifest is uploaded last.
- Updates never happen without a click. Registry disk is never touched by variant switches or updates.

## Review Focus

1. Manifest naming a path like `../../x` — app must reject it, never write outside the release folder (test in Task 4).
2. Download interrupted mid-file, then resumed — must produce a verified file; a server ignoring `Range` (200 instead of 206) must restart cleanly, not append (test in Task 4).
3. Newer release with higher `guest_api` — app must skip it and choose the older compatible one (test in Task 4).
4. Base image without the layer marker — readiness must not wait for Headlamp/Grafana or probe 30080/30090 (test in Task 1).
5. Layer copied to another folder with its base — must still boot because the backing reference is relative (test in Task 2).

---

### Task 1: Guest — layer-aware readiness and layer provisioning

**Files:**
- Modify: `guest/api.py`, `guest/provision.sh`, `guest/smoke.sh`
- Create: `guest/provision-layer.sh`, `guest/smoke-observability.sh`, `guest/demo-layer.service`
- Test: `tests/test_demo.py`

**Interfaces:**
- Produces: `api.smbios_value(key: str) -> str | None`; `api.launcher_token()` uses it; `api.layers() -> list[str]` (names of files in `LAYERS_PATH = Path("/opt/demo/layers")`); `status()` ready reply includes `"layers"`.

- [ ] **Step 1: Tests** in `GuestTests`:
  - `test_base_readiness_ignores_observability_workloads`: `layers` patched to `[]`, kubectl fake returns ready for registry/gadget/coredns/kube-proxy and raises for anything else; urlopen patched; assert ready and `layers == []`, and urlopen was only called for port 30500.
  - `test_observability_layer_requires_its_workloads_and_ports`: `layers` → `["observability"]`; headlamp not ready → not ready; all ready → probes include 30080 and 30090, `layers == ["observability"]`.
  - Update existing monitoring/headlamp readiness tests to patch `layers` to `["observability"]`.
  - `test_smbios_value_reads_any_key`: record with token + `io.inspektor-gadget.demo.provision=observability` → both read; missing key → `None`.
  - `MonitoringConfigTests.test_base_provision_installs_no_observability_charts`: `provision.sh` contains no `headlamp`/`prometheus`/`grafana` helm installs; `provision-layer.sh` contains all three plus `touch /opt/demo/layers/observability`; `provision.sh` enables `demo-layer.service`.
- [ ] **Step 2:** `python3 -m unittest tests.test_demo -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
  - `api.py`: refactor SMBIOS parsing into `smbios_value`; workload list = base list + (headlamp, prometheus, grafana, node-exporter) when `"observability" in layers()`; ports = `[(30500, "/v2/")]` + `[(30080, "/"), (30090, "/api/health")]` with layer.
  - `provision.sh`: drop the headlamp/prometheus/grafana repos and installs; `systemctl enable demo-layer.service`.
  - `smoke.sh`: drop Headlamp curl and Prometheus check; trigger the trace with `kubectl -n demo exec deployment/registry -- cat /etc/hostname`.
  - `demo-layer.service`: `Type=oneshot`, `After=demo.service demo-api.service`, `Requires=demo.service`, `ExecStart=/opt/demo/provision-layer.sh`, `TimeoutStartSec=0`, console output, `WantedBy=multi-user.target`.
  - `provision-layer.sh`: exit 0 unless `smbios_value("io.inspektor-gadget.demo.provision") == "observability"` (read via `python3 -c 'import runpy; …'`); `set -Eeuxo pipefail`; ERR trap prints `DEMO_LAYER_FAILED` and powers off; helm repos + the three installs moved verbatim from `provision.sh`; `mkdir -p /opt/demo/layers && touch /opt/demo/layers/observability`; run `smoke-observability.sh`; clean helm cache; `fstrim -av`; `sync`; `echo DEMO_LAYER_READY`; `systemctl poweroff`.
  - `smoke-observability.sh`: wait for `status()` ready (as `smoke.sh`), `curl` 30080 and 30090 `/api/health`, run `registry.demo.svc.cluster.local:5000/demo/trace_open:demo` for 30 s while exec-ing `cat /etc/hostname` in headlamp, then the existing Prometheus `ig_grpc_run_gadget_total` loop.
- [ ] **Step 4:** unittest PASS; `shellcheck guest/*.sh` clean.
- [ ] **Step 5:** Commit `guest: split observability into a provisioned layer`.

### Task 2: Builder — two variants, relative layer, manifest

**Files:**
- Modify: `scripts/build-image.py`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: Task 1 SMBIOS key and `DEMO_LAYER_READY`.
- Produces: `GUEST_API = 1`; `qemu_args(..., provision=None)` adds `value=io.inspektor-gadget.demo.provision={provision}` to the type 11 record; `package_layer(layer: Path, output: Path, base_name: str) -> Path`; `write_manifest(output: Path, arch: str, release: str, files: list[Path]) -> Path`.

- [ ] **Step 1: Tests:**
  - `test_provision_flag_shares_the_token_record`: `qemu_args(..., provision="observability")` has one `-smbios` with both values; without it, no provision value.
  - `test_manifest_lists_variants_in_chain_order`: temp files → manifest JSON has `guest_api == builder.GUEST_API`, `arch`, `release`, each file's size and sha256, `variants.minimal.chain == ["ig-minimal-arm64.qcow2"]`, `variants.observability.chain == ["ig-minimal-arm64.qcow2", "ig-observability-arm64.qcow2"]`, `support == ["ig-registry-seed-arm64.qcow2", "QEMU_EFI-arm64.fd"]` (no firmware for amd64).
  - `test_layer_backs_onto_base_by_filename` (skip without `qemu-img`): build base + overlay with data, `package_layer` into a dir that holds the base copy; `qemu-img info --output=json` → `backing-filename == "ig-minimal-<arch>.qcow2"`; move the dir, `qemu-img compare` against the original overlay still succeeds.
- [ ] **Step 2:** tests FAIL.
- [ ] **Step 3: Implement.**
  - `--release` arg, default `env IMAGE_RELEASE` or `"local"`.
  - After base provisioning: `layer = work / "observability.qcow2"` overlay of `disk` (skip if `layer-provision.log` has `DEMO_LAYER_READY` and `--resume`); registry = temp copy of `registry_seed`; boot online with `provision="observability"`; require `DEMO_LAYER_READY`; chmod 444.
  - Cold boots: base boots 1 and 2 as today minus `check_monitoring` and minus the 18080 probe in `check_host_access` (move that probe into the observability boot). Then cold boot 3: session on `layer`, same `registry` copy, offline: ready, `status["layers"] == ["observability"]`, Headlamp 200, `check_monitoring()`, `check_os_reset(first_boot=False)`, `check_registry_blob(blob)`.
  - Package: base → `ig-minimal-<arch>.qcow2` (`-c`), then `package_layer` → `ig-observability-<arch>.qcow2` via `qemu-img convert -c -O qcow2 -B ig-minimal-<arch>.qcow2 -F qcow2`, then `qemu-img check` the output; seed, firmware, versions as today; `SHA256SUMS-<arch>` over all; `write_manifest` last.
- [ ] **Step 4:** tests PASS.
- [ ] **Step 5:** Commit `builder: package observability as a backed layer with a manifest`.

### Task 3: CI and docs

**Files:**
- Modify: `.github/workflows/images.yml`, `README.md`
- Test: `tests/test_demo.py`

- [ ] **Step 1: Test** `test_image_releases_are_public_and_separate`: `images.yml` triggers on `images-v*` (not `"v*"`), passes `--release "$GITHUB_REF_NAME"`, contains `--latest=false`, no `--draft`, uploads `manifest-` after the other files.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Workflow: tag filter `images-v*`; `python3 scripts/build-image.py --arch … --release "${GITHUB_REF_NAME:-local}"`; create release `--title … --notes "Demo VM images" --latest=false`; upload everything except `manifest-*`, then the manifest. README: variants, download flow, file names, `images-v*` release process, `guest_api` rule.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `ci: publish image releases under images-v tags`.

### Task 4: App — `images` module

**Files:**
- Create: `src-tauri/src/images.rs`
- Modify: `src-tauri/Cargo.toml` (reqwest feature `native-tls`, add `sha2 = "0.10"`)

**Interfaces:**
- Produces:
  - `pub const GUEST_API: u32 = 1;`
  - `#[derive(Deserialize, Serialize, Clone)] pub struct Manifest { guest_api, release, arch, versions: BTreeMap<String,String>, files: BTreeMap<String, FileEntry{size: u64, sha256: String}>, variants: BTreeMap<String, Variant{chain: Vec<String>, support: Vec<String>}> }`
  - `pub fn parse_manifest(text: &str, arch: &str) -> Result<Manifest, String>` — rejects other arch, unsupported `guest_api`, unsafe names, variant files missing from `files`, empty chain.
  - `impl Manifest { pub fn variant_files(&self, variant: &str) -> Result<Vec<String>, String>; pub fn variant_size(&self, variant: &str) -> u64; pub fn top(&self, variant: &str) -> Result<String, String> }`
  - `pub struct Release { pub tag: String, pub assets: BTreeMap<String, String> }` (name → download URL); `pub fn releases_from_api(json: &serde_json::Value) -> Vec<Release>` (keeps `images-v*`, non-draft, API order).
  - `pub fn pick_release(releases: &[Release], arch: &str, fetch: impl Fn(&str) -> Result<String, String>) -> Result<(Release, Manifest), String>`
  - `pub fn download_file(client: &Client, url: &str, dir: &Path, name: &str, entry: &FileEntry, cancel: &AtomicBool, progress: &mut dyn FnMut(u64)) -> Result<(), String>`
  - `pub struct Installed { pub release: String, pub dir: PathBuf, pub manifest: Manifest, pub variants: Vec<String> }`; `pub fn installed(images: &Path, arch: &str) -> Vec<Installed>` (newest tag first; a variant counts only when every file it needs exists with the manifest size).
  - `pub fn client() -> Result<Client, String>` (blocking, User-Agent, 30 s connect timeout, no overall timeout).

- [ ] **Step 1: Tests** (`#[cfg(test)]`, fake server = `std::net::TcpListener` thread answering scripted responses):
  - `manifest_rejects_paths_other_arch_and_newer_guest_api`
  - `variant_files_are_chain_then_support_and_sizes_add_up`
  - `pick_release_skips_incompatible_and_non_image_releases`
  - `pick_release_errors_when_nothing_is_compatible` (message contains "update the app")
  - `download_resumes_partial_file_with_range` (server asserts `Range: bytes=N-`, returns 206)
  - `download_restarts_when_server_ignores_range` (server returns 200 full body)
  - `download_rejects_checksum_mismatch_and_removes_part`
  - `installed_counts_only_complete_variants`
- [ ] **Step 2:** `cargo test --manifest-path src-tauri/Cargo.toml images` FAIL.
- [ ] **Step 3:** Implement. Download: if final file exists with right size and hash → done; open `<name>.part`, send `Range: bytes=<len>-` when non-empty; 206 → append, 200 → truncate; stream in 1 MiB chunks, checking `cancel`; verify size + sha256 of the whole part, rename. Release ordering in `installed`: sort by tag with numeric-aware compare of `images-vX.Y.Z`.
- [ ] **Step 4:** PASS; `cargo clippy --all-targets -- -D warnings` clean.
- [ ] **Step 5:** Commit `app: discover, download and verify image releases`.

### Task 5: App — commands, status layers, wiring

**Files:**
- Modify: `src-tauri/src/main.rs`, `src-tauri/src/vm.rs`

**Interfaces:**
- Consumes: Task 4.
- Produces Tauri commands:
  - `image_catalog() -> {installed: [{release, variants, versions, path: {variant: top path}}], available: {release, versions, variants: {name: {size, missing}}} | null, error: string | null}` — `missing` is bytes still to download given what is installed for that release.
  - `download_variant(release: String, variant: String) -> String` (top image path); emits `image-progress` `{done, total}`; manifest saved as `manifest-<arch>.json` in the release folder before files.
  - `cancel_download()`
  - `prune_images(keep: String)` — deletes other release folders; never the one `base-image.txt` points into.
  - `Status` gains `layers: Vec<String>` from the guest reply.
- [ ] **Step 1: Tests:** `guest_status_reports_layers` in `vm.rs` (`layers` present → copied, absent → empty).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement; downloads run in `spawn_blocking`, guarded by a `Downloads(AtomicBool busy, AtomicBool cancel)` state; `choose_image` filter stays qcow2.
- [ ] **Step 4:** `cargo test` + clippy PASS.
- [ ] **Step 5:** Commit `app: expose image catalog and downloads`.

### Task 6: UI — chooser, progress, update, rail visibility

**Files:**
- Modify: `ui/index.html`, `ui/app.js`, `ui/style.css`

- [ ] **Step 1:** Demo machine section: two cards `#variant-minimal` ("IG only — Kubernetes, Inspektor Gadget, local registry") and `#variant-observability` ("IG + Headlamp + Grafana — adds Prometheus and node-exporter"), each showing size/state and a button (Download / Add / Use); progress bar `#progress` with Cancel; `#update` banner "Update available: <release>" with button; "Choose image file…" keeps `#browse`; `#image` shows the selected path.
- [ ] **Step 2:** `app.js`: `refreshCatalog()` on load and after downloads; selecting a card sets `#image` to the installed top path; Download/Add/Update call `download_variant` and listen to `image-progress`; after the first ready following a start from a downloaded release, `prune_images({keep})`; rail Headlamp/Grafana are `hidden` unless `state.layers.includes("observability")`.
- [ ] **Step 3:** `npm run check` passes.
- [ ] **Step 4:** Commit `ui: choose and download image variants`.

### Task 7: Verification

- [ ] `python3 -m unittest discover tests -v`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`, `npm run check`, `shellcheck guest/*.sh` all pass.
- [ ] Full arm64 build: `python3 scripts/build-image.py --arch arm64 --output build/verify-arm64 --release local` passes and prints both variants; `manifest-arm64.json` lists both; `qemu-img info` of the layer shows `backing file: ig-minimal-arm64.qcow2`.
- [ ] Manual (user host): first-run download of each variant, Add Headlamp + Grafana later, update banner.
