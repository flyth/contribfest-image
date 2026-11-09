# IG Demo

A small, disposable Kubernetes demo appliance and a Tauri 2 launcher. The guest
has **2 vCPUs / 4 GiB RAM**, Ubuntu Minimal 24.04, containerd, a single-node
Kubernetes cluster bootstrapped with kubeadm, Inspektor Gadget through its Helm
chart, and a persistent, single-pod OCI registry. An optional layer adds
in-cluster Headlamp, Prometheus with node-exporter, and Grafana. No nested VM,
ingress, log stack, Docker daemon, or frontend framework.

Two variants share one base disk:

| Variant | Files |
|---------|-------|
| IG only | `ig-minimal-ARCH.qcow2` |
| IG + Headlamp + Grafana | `ig-minimal-ARCH.qcow2` and the layer `ig-observability-ARCH.qcow2` |

Both also need `ig-registry-seed-ARCH.qcow2` and, on ARM, `QEMU_EFI-arm64.fd`.
The layer stores only its changes and refers to the base by bare filename, so
keep the files in one folder.

## Contribfest: run the cluster with Lima (macOS, Linux)

[Lima](https://lima-vm.io) runs the minimal image (Kubernetes, IG, registry)
without the launcher. Build gadgets from
[gadget-template](https://github.com/inspektor-gadget/gadget-template): its CI
publishes them to `ghcr.io`, and the cluster pulls them from there.

```sh
brew install lima          # Linux: install QEMU first, then Lima
lima/ig-demo start         # downloads the image once, then about 5 minutes
eval "$(lima/ig-demo env)" # kubectl, kubectl-gadget and KUBECONFIG for this shell
kubectl gadget run ghcr.io/<owner>/<repository>:latest
```

`ig-demo start` takes the newest `images-v*` release, or `--image FILE` for a
copy you already have (a USB stick at the event, or a local build; a
`SHA256SUMS-<arch>` file beside it is checked). It verifies every download
against its published SHA256, creates the VM from `lima/ig-demo.yaml`, waits
for the cluster, writes `~/.ig-demo/kubeconfig` (API at `127.0.0.1:18443`),
and installs `kubectl` and `kubectl-gadget` matching the VM into
`~/.ig-demo/bin`. Nothing needs root, and nothing from your computer is shared
with the VM. Only the API server is forwarded. The VM keeps its cluster and
registry across `ig-demo stop` and `start`; `ig-demo delete` removes it.

The same image boots under the launcher and under Lima. Lima attaches a NoCloud
seed, so cloud-init creates its user and SSH key; without one, cloud-init stays
off. Outside the launcher, the node keeps its baked address `10.0.2.15` on the
loopback link, and the registry lives on the OS disk.

## Try it

1. **macOS and Windows:** nothing to install; the app bundles a minimal QEMU.
   The app is not signed. On macOS, the first launch is blocked: open **System
   Settings → Privacy & Security** and click **Open Anyway**, or run
   `xattr -dr com.apple.quarantine "/Applications/IG Demo.app"`. On Windows,
   SmartScreen warns about the installer: click **More info → Run anyway**.
   Windows also needs **Windows Hypervisor Platform** enabled (Windows Features),
   then a restart.
   **Linux:** install [QEMU](https://www.qemu.org/download/), including
   `qemu-img`. Ubuntu: `sudo apt install qemu-system qemu-utils`.
   The launcher uses its bundled QEMU first, then searches `QEMU_DIR`, PATH,
   Homebrew, and `%ProgramFiles%\qemu`.
2. Open the launcher and pick a variant. It downloads the newest compatible
   image release from GitHub, resuming interrupted downloads and checking every
   file's SHA256. You can add Headlamp + Grafana later; only the layer downloads.
   When a newer compatible release appears, the launcher offers it and changes
   nothing until you click **Update**. To use files you already have (a local
   build, an offline copy), use **Choose image file…** and pick either qcow2.
3. **Start demo**. When ready, open
   Headlamp or Grafana from the sidebar (full variant), already signed in, or export the
   kubeconfig for IG Desktop. Each keeps its page and history while you switch.
   Every launch starts a fresh cluster. **Stop safely** preserves pushed registry
   images; **Reset cluster** keeps them too. Only **Delete registry images** removes
   them, after explicit confirmation; the next start restores the bundled demo images.

The host needs more than 4 GiB total RAM: **4 GiB is allocated to the guest**.
Allow about 6 GiB free memory including QEMU and the desktop. Hardware
acceleration is required by the launcher (KVM, HVF, or WHPX). Use the host's
native architecture: Linux/macOS x64 and ARM64, Windows x64. Windows ARM64 is
not yet supported. Linux users need permission to access `/dev/kvm`.

| Host endpoint | Purpose |
|---|---|
| `https://127.0.0.1:18443` | Kubernetes API; exported kubeconfig contains CA/client credentials |
| `http://127.0.0.1:18080` | Headlamp; signed in automatically inside the app, a normal browser still asks for a token |
| `http://127.0.0.1:18090` | Grafana; anonymous admin with no login, Prometheus preconfigured |
| `http://127.0.0.1:15000` | OCI registry push endpoint |
| `http://127.0.0.1:18081` | Private launcher bridge; random per-boot bearer token |

All QEMU forwards bind **127.0.0.1**, not the LAN. Ports are fixed; the launcher
reports collisions. It never silently falls back to slow emulation.

## Registry persistence and migration

Only registry contents survive VM sessions. Kubernetes changes, etcd data, logs
inside the guest, and container runtime caches revert to the preconfigured base
on each launch. Push anything worth keeping to the registry before stopping.

The launcher's private data directory contains a standalone `registry.qcow2`,
copied once from the bundled seed, and a disposable `session.qcow2` OS overlay.
The registry disk is mounted at `/var/lib/demo-registry` before Kubernetes starts;
a missing, invalid, or failed mount blocks startup instead of falling back to
ephemeral storage. Changing the OS image does not replace the registry disk.
The OS and registry each have 16 GiB virtual capacity; allocated space grows with use.

Older launchers kept registry data inside `disk.qcow2`. On first use, the new
launcher migrates that registry through a temporary copy-on-write disk, without
modifying the original. A durable marker prevents retries from overwriting newer
pushes. The old overlay is deleted only after migration is reported ready and
**Stop safely** completes successfully. Until then, reset and deletion refuse to
discard it. Keep the old base image available until migration finishes, because
the old overlay may still depend on it. Migration errors preserve the source.


## Push and run your own gadget

For example, with [ORAS](https://oras.land/docs/installation/) and the
`kubectl-gadget` plugin on the host (both available for Linux, macOS and Windows):

```sh
oras cp ghcr.io/inspektor-gadget/gadget/trace_open:v0.57.0 \
  127.0.0.1:15000/demo/trace_open:demo \
  --to-plain-http

export KUBECONFIG=/path/shown/by/the/launcher/kubeconfig
kubectl gadget run -n demo registry.demo.svc.cluster.local:5000/demo/trace_open:demo \
  --timeout=10
```

Or `ig image push 127.0.0.1:15000/YOUR_GADGET:TAG
--insecure-registries=127.0.0.1:15000` after building a gadget. In IG Desktop,
choose its Kubernetes environment with the exported kubeconfig, select the
`demo` namespace, and use the
**in-cluster registry name**, not host localhost. The two addresses refer to the
same registry. Normal Docker image pushes also work; non-localhost clients may
need explicit insecure-registry configuration.

### Build from source on your computer

Start the demo, export its kubeconfig, then use Python 3.11+ and `kubectl`:

```sh
python3 scripts/build-gadget.py ./my-gadget \
  --kubeconfig /path/exported/by/the/launcher/kubeconfig \
  --tag demo/my-gadget:dev

kubectl --kubeconfig /path/exported/by/the/launcher/kubeconfig gadget run \
  -n demo registry.demo.svc.cluster.local:5000/demo/my-gadget:dev --pull=always --timeout=10
```

Edit normally in your host checkout and rerun the build command. It snapshots
the selected directory, streams it and a SHA256-verified Linux `ig` binary into
a disposable pod, runs `ig image build --local`, and pushes to the persistent
registry. Both `ig` and the official builder image follow `IG_VERSION` in
`versions.env`; no host Docker, Docker-in-Docker, privileged pod, or VM rebuild
is needed. Build logs are streamed to the terminal; failure, timeout, or
Ctrl+C triggers pod cleanup. The gadget is **not run automatically**.
Use `--pull=always` when running a development tag so an older daemon cache does
not hide a newly pushed build.

The command requires `--kubeconfig` or an explicitly set `KUBECONFIG`; it never
uses the global default context implicitly. Keep the kubeconfig outside the
source directory. `.git` is excluded; uploads are limited to regular files, directories,
and internal links. Escaping links are rejected. No Kubernetes service-account
token or host directory is mounted in the builder. Use the standard
`program.bpf.c`/`gadget.yaml` layout or an optional `build.yaml`; include headers
and module inputs under the selected directory. Other host files are not uploaded.
Build only trusted source.

The first build in a fresh VM session pulls the toolchain image; it is not added
to the appliance base. Linux `ig` archives are cached under `build/gadget-tools/`.
Builders have a 2 GiB memory limit and a default 1,800-second lifetime; use
`--timeout SECONDS` for slower builds. Go/Rust Wasm builds may download modules
or crates. Offline builds require the toolchain image and dependencies already
available, while pushed gadgets survive VM restarts independently of those caches.


IG explicitly disables signature verification and allows HTTP for
`registry.demo.svc.cluster.local:5000`. This is intentional and **demo-only**.
The registry is unauthenticated, Grafana grants anonymous admin to anything
that can reach its loopback port, Headlamp tokens and kubeconfigs are
cluster-admin, and baked images share their Kubernetes CA and initial
credentials. Never attach this appliance to a bridged/public network or use
real credentials/workloads. The bearer-protected launcher bridge exports
credentials without publishing them in console logs. Import only trusted
appliance images; Kubernetes and IG run privileged code.

## Build an image

Step-by-step recipe for a fresh host, plus how to test the result under Lima
and the launcher: [BUILD_IMAGE.md](BUILD_IMAGE.md).

Use Linux (Python 3.11+, QEMU, `cloud-localds`, and `mkfs.ext4`); no host root is needed
after installing packages. KVM is strongly recommended. The builder can use
TCG when KVM is unavailable or for cross-architecture builds, but baking can take
much longer. Prefer native runners in CI.

```sh
sudo apt-get install --no-install-recommends \
  qemu-system-x86 qemu-system-arm qemu-utils qemu-efi-aarch64 cloud-image-utils e2fsprogs
python3 scripts/build-image.py --arch amd64   # or arm64 on an ARM runner
```

`dist/ubuntu-registry/` contains the zlib-compressed base, observability layer
and registry-seed qcow2 images, ARM firmware, `SHA256SUMS-ARCH`, `versions-ARCH.env`,
and `manifest-ARCH.json` (written last; it lists every file's size and SHA256 and
the disk chain for each variant). Build logs and intermediate
disks are in `build/ubuntu-registry-ARCH/`. To rebuild, move aside `demo.qcow2`
and `registry-seed.qcow2` there. `--resume` repeats only verification and packaging;
it rejects changed guest files or version pins rather than silently mixing builds.
The Ubuntu base is cached and verified against its HTTPS SHA256 list. Guest tool
downloads use upstream checksums. Apt packages are resolved at build time:
builds are **version-pinned, not bit-for-bit reproducible**.

The builder boots Ubuntu with a generated NoCloud seed, installs and pre-pulls
the workloads, checks BTF, pushes a gadget through the registry NodePort, and
asserts real file-open events through the IG daemon. It then boots an overlay of
that base with an SMBIOS flag that makes `demo-layer.service` install Headlamp,
Prometheus and Grafana; that overlay becomes the layer. It then boots
three times without provisioning or WAN access, with a fresh OS overlay each time and
one standalone registry disk: twice on the base, once on the layer. It verifies Kubernetes TLS access
(and, on the layer, Headlamp and Grafana metrics),
uploads a unique registry blob and creates a Kubernetes namespace, then confirms
that the blob survives the next launch but the namespace does not. Clean shutdown
and image checks must pass before publishing either compressed image.
Preloaded components work on subsequent boots without downloading them; pulling
new gadget images still needs a network. Kubernetes startup is not instantaneous;
measure cold boot and resource use on target hardware before promising timings.

Guest tools live under `/opt`; the launcher bridge uses Python. The single-node
bridge CNI needs no overlay daemon. Docker is not installed; unnecessary Ubuntu
packages and automatic apt update timers are removed or disabled. Rebuild images
to update the pinned appliance. Existing build directories and experimental images
are left untouched.

The image workflow builds amd64 and arm64 independently, retains logs and
artifacts, and publishes a public, non-"latest" release on `images-v*` tags
(launcher releases keep `v*`). Each architecture's manifest is uploaded after its
other files, so the launcher never sees a half-uploaded architecture. The
manifest's `guest_api` is bumped whenever a guest change (ports, bridge API,
readiness, file layout) would break older launchers; launchers only offer releases
whose `guest_api` they support. ARM hosted runners
require an eligible GitHub repository/account. VM images are never embedded in
the launcher installer; the launcher downloads them.

## Build the launcher

Install Rust, Node.js, and the [Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/).

```sh
npm ci
npm run dev
npm run build
```

macOS and Windows bundle QEMU from `src-tauri/qemu/`, built from the source
pinned in `scripts/qemu.env`. On macOS, `npm run dev` and `npm run build` first run
`scripts/build-qemu-macos.sh --if-missing`, which builds it (about 10 minutes, once). It needs the Xcode command line
tools and `brew install ninja pkg-config glib pixman libslirp dtc`. It builds
pinned QEMU source with only what the demo uses (one HVF system emulator and
`qemu-img`), copies the Homebrew libraries they link into the bundle, and
ad-hoc signs everything, giving the emulator the `com.apple.security.hypervisor`
entitlement HVF needs. No Apple account is involved. Those libraries require
the macOS version they were built on or newer, so CI builds on the oldest
supported runner. `src-tauri/qemu/licenses/` holds the QEMU and library
licenses and source versions that redistribution requires. Delete
`src-tauri/qemu/` to rebuild.

On Windows, build it first in an [MSYS2](https://www.msys2.org) UCRT64 shell,
then run `npm run build` as usual:

```sh
pacman -S --needed diffutils mingw-w64-ucrt-x86_64-{gcc,glib2,pixman,libslirp,dtc,ninja,pkgconf,python}
scripts/build-qemu-windows.sh
```

It builds the x86_64 emulator with WHPX and `qemu-img`, and copies the MSYS2
DLLs they use beside them, where Windows looks first. Nothing is signed, and the
licenses and package versions go to `src-tauri/qemu/licenses/` as on macOS.

No web dev server is needed. The UI is static HTML/CSS/JavaScript. QEMU is a
child process with a private per-user OS overlay and a separate persistent registry.
Keep the selected base image at its original path while its VM is running.
The data/log location appears in the UI. Closing while running requests a clean
shutdown and keeps the window open; close again once stopped. If Linux is not
responding yet, wait and use Stop safely. Force-killing the app/VM can lose registry data.
On Windows, the app data directory inherits the current user's profile ACLs.

Desktop CI compiles/tests/packages Linux x64/ARM64, macOS Intel/Apple Silicon,
and Windows x64. Installers are unsigned (macOS: ad-hoc signed only); signing and
notarization are required before public consumer distribution. IG Desktop itself is deliberately not changed or bundled yet.

## Metrics

Prometheus scrapes the IG daemon through the chart's pod annotations, plus
node-exporter and Kubernetes itself, keeping up to 2 days or 512 MB. Grafana opens
on an **IG Demo** dashboard. IG exports Go runtime metrics from the start, but its
own `ig_*` series appear only after the first gadget runs. No gadget instances run
by default, so there is no gadget event data until you enable some.

## Maintenance and checks

`versions.env` is the guest's single release input. Headlamp, Prometheus and
Grafana are pinned to the latest stable chart at implementation time, not a
mutable `latest` tag.
Run `python3 scripts/update-versions.py`, review the diff, then build both
images. It tracks stable Kubernetes and keeps CRI tools aligned with that minor.
Ubuntu base updates remain explicit: change `UBUNTU_BUILD` to a published snapshot
and rebuild both architectures. Launcher tests require QEMU's `qemu-img` and
`qemu-io` tools as well as Rust.

Dependabot updates Cargo, npm and GitHub Actions dependencies. **Dependabot
does not support arbitrary binary/Helm version pins in env files**; the small
update script handles those instead of disguising them as unused dependencies.

```sh
shellcheck guest/*.sh
python3 -m unittest discover -s tests -v
npm run check
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
```
