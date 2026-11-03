# IG Demo

A small, disposable Kubernetes demo appliance and a Tauri 2 launcher. The guest
has **2 vCPUs / 4 GiB RAM**, Ubuntu Minimal 24.04, containerd, minikube (`none`
driver), Inspektor Gadget installed through its Helm chart, in-cluster Headlamp,
and a persistent, single-pod OCI registry. No nested VM, ingress, metrics stack,
Docker daemon, or frontend framework.

## Try it

1. Install [QEMU](https://www.qemu.org/download/), including `qemu-img`. macOS:
   `brew install qemu`. Ubuntu: `sudo apt install qemu-system qemu-utils`.
   Windows: install QEMU, enable **Windows Hypervisor Platform**, and restart.
   The launcher searches PATH, Homebrew, and `%ProgramFiles%\qemu`; override with
   `QEMU_DIR` if needed.
2. Download the matching `ig-demo-amd64.qcow2` or `ig-demo-arm64.qcow2` from an
   image workflow artifact/release. ARM also needs `QEMU_EFI-arm64.fd` beside it.
   Verify against `SHA256SUMS-ARCH` (`sha256sum -c …`, or `shasum -a 256` on Mac).
3. Open the launcher, choose the image, and **Start demo**. When ready, open
   Headlamp and copy its login token, or export the kubeconfig for IG Desktop.
   **Stop safely** preserves the cluster and pushed images; **Reset demo data**
   discards changes and starts again from the immutable base image.

The host needs more than 4 GiB total RAM: **4 GiB is allocated to the guest**.
Allow about 6 GiB free memory including QEMU and the desktop. Hardware
acceleration is required by the launcher (KVM, HVF, or WHPX). Use the host's
native architecture: Linux/macOS x64 and ARM64, Windows x64. Windows ARM64 is
not yet supported. Linux users need permission to access `/dev/kvm`.

| Host endpoint | Purpose |
|---|---|
| `https://127.0.0.1:18443` | Kubernetes API; exported kubeconfig contains CA/client credentials |
| `http://127.0.0.1:18080` | Headlamp; log in with the generated 24-hour admin token |
| `http://127.0.0.1:15000` | OCI registry push endpoint |
| `http://127.0.0.1:18081` | Private launcher bridge; random per-boot bearer token |

All QEMU forwards bind **127.0.0.1**, not the LAN. Ports are fixed; the launcher
reports collisions. It never silently falls back to slow emulation.

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

IG explicitly disables signature verification and allows HTTP for
`registry.demo.svc.cluster.local:5000`. This is intentional and **demo-only**.
The registry is unauthenticated, Headlamp tokens and kubeconfigs are
cluster-admin, and baked images share their Kubernetes CA and initial
credentials. Never attach this appliance to a bridged/public network or use
real credentials/workloads. The bearer-protected launcher bridge exports
credentials without publishing them in console logs. Import only trusted
appliance images; Kubernetes and IG run privileged code.

## Build an image

Use Linux (Python 3.11+, QEMU, `cloud-localds`); no host root is needed
after installing packages. KVM is strongly recommended. The builder can use
TCG when KVM is unavailable or for cross-architecture builds, but baking can take
much longer. Prefer native runners in CI.

```sh
sudo apt-get install --no-install-recommends \
  qemu-system-x86 qemu-system-arm qemu-utils qemu-efi-aarch64 cloud-image-utils
python3 scripts/build-image.py --arch amd64   # or arm64 on an ARM runner
```

`dist/` contains the compressed qcow2 (16 GiB sparse virtual capacity), firmware
for ARM, checksums and version manifest. Build logs and intermediate disks are
in `build/ARCH/`. To rebuild, move aside the previous `build/ARCH/demo.qcow2`.
Use `--resume` to repeat the cold-boot/package phase after a failed boot check
without provisioning again. It requires a completed provision and unchanged
version pins; it does **not** install updated guest scripts.
The base download is cached and verified against Ubuntu's HTTPS checksum list.
Provisioning downloads use upstream checksums. Apt packages are resolved at
build time: builds are **version-pinned, not bit-for-bit reproducible**.

The script boots Ubuntu with a generated NoCloud seed, installs and pre-pulls
the workloads, checks BTF, pushes a gadget through the registry NodePort and
asserts real file-open events from Headlamp's container via the IG daemon.
It then powers off, boots again **without the seed**,
waits for all three workloads with WAN access blocked, verifies host-side TLS
API access and Headlamp, pushes a registry blob from the host, and shuts down
cleanly before compressing. A failed provision/boot never produces a release image.
Subsequent boots start the persisted cluster with kubelet, not another minikube
bootstrap.
Preloaded components work on subsequent boots without downloading them; pulling
new gadget images still needs a network. Kubernetes startup is not instantaneous;
measure cold boot and resource use on target hardware before promising timings.

The image workflow builds amd64 and arm64 independently, retains logs and
artifacts, and uploads draft release assets on `v*` tags. ARM hosted runners
require an eligible GitHub repository/account. Neither VM images nor QEMU are
embedded in the initial launcher installer: distribute the matching assets
together. This avoids enormous installers and leaves a simple integration
point for a later IG Desktop bundle.

## Build the launcher

Install Rust, Node.js, and the [Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/).

```sh
npm ci
npm run dev
npm run build
```

No web dev server is needed. The UI is static HTML/CSS/JavaScript. QEMU is a
child process with a private per-user qcow2 overlay; the base is never modified.
Keep the base image at its original path while an overlay exists.
The data/log location appears in the UI. Closing while running requests a clean
shutdown and keeps the window open; close again once stopped. If Linux is not
responding yet, wait and use Stop safely. Force-killing the app/VM can lose data.
On Windows, the app data directory inherits the current user's profile ACLs.

Desktop CI compiles/tests/packages Linux x64/ARM64, macOS Intel/Apple Silicon,
and Windows x64. Installers are unsigned by default; signing/notarization and
redistribution of a pinned QEMU runtime are required before public consumer
distribution. IG Desktop itself is deliberately not changed or bundled yet.

## Maintenance and checks

`versions.env` is the guest's single release input. Headlamp is pinned to the
latest stable chart at implementation time, not a mutable `latest` tag.
Run `python3 scripts/update-versions.py`, review the diff, then build both
images. It keeps Kubernetes aligned with minikube's supported default and
CRI tools aligned with that Kubernetes minor. Ubuntu build-date updates are
explicit; change `UBUNTU_BUILD` to a published snapshot.

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
