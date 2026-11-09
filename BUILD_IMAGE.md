# Build and test the demo image

This builds the VM images from scratch on any Linux host and tests them. One
run produces both variants for one architecture:

| File | What it is |
|------|------------|
| `ig-minimal-<arch>.qcow2` | Ubuntu 24.04 minimal, single-node Kubernetes, Inspektor Gadget, local registry. This is the image Lima runs. |
| `ig-observability-<arch>.qcow2` | Layer on top of the minimal image: Headlamp, Prometheus, node-exporter, Grafana (launcher only). |
| `ig-registry-seed-<arch>.qcow2` | Empty registry disk for the launcher. |
| `QEMU_EFI-arm64.fd` | arm64 firmware for the launcher (arm64 only). |
| `versions-<arch>.env`, `SHA256SUMS-<arch>`, `manifest-<arch>.json` | Pinned versions, checksums, and the release manifest. |

## 1. Host requirements

- **Linux**, x86_64 for `amd64` images or arm64 for `arm64` images. Building
  for the other architecture works but uses software emulation and takes many
  hours. macOS and Windows cannot build; use a Linux VM there (on Apple Silicon,
  a Lima VM with `nestedVirtualization: true` gives it KVM on M3 or newer).
- **KVM** (`/dev/kvm` readable and writable by your user) strongly recommended.
  Without it the builder falls back to TCG, which is several times slower.
- **Disk:** about 15 GB free (work disks, logs and outputs).
- **Memory:** about 6 GB free; each test VM gets 4 GiB.
- **Network:** about 2 GB of downloads (Ubuntu packages, Kubernetes, IG,
  container images, Helm charts). On a slow connection this dominates the
  build time.
- **Free host ports** while it runs: 18081, 18443, 18080, 18090, 15000 (all on
  127.0.0.1). Stop the launcher and any other demo VM first.
- No root needed to run the build once the packages are installed.

## 2. Install the build tools

Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends \
  python3 git qemu-system-x86 qemu-system-arm qemu-utils qemu-efi-aarch64 \
  cloud-image-utils e2fsprogs ipxe-qemu
```

Other distributions need the same pieces: Python 3.11+, `qemu-system-x86_64`
or `qemu-system-aarch64`, `qemu-img`, the aarch64 UEFI firmware at
`/usr/share/qemu-efi-aarch64/QEMU_EFI.fd` (arm64 only), `cloud-localds`, and
`mkfs.ext4`.

Optional, for the checks in section 5: `shellcheck`.

## 3. Build

```sh
git clone -b layered-images-lima https://github.com/flyth/ig-launcher.git ig-demo
cd ig-demo
python3 scripts/build-image.py --arch arm64     # or: --arch amd64
```

Options:

- `--output DIR` — where the finished files go (default `dist/ubuntu-registry/`).
- `--release NAME` — release name recorded in the manifest (default `local`;
  CI passes the `images-v*` tag).
- `--timeout SECONDS` — per-boot timeout (default 3600). Raise it on slow
  networks or without KVM.
- `--resume` — skip provisioning and only re-run the tests and packaging on an
  already provisioned disk. It refuses if guest files or `versions.env` changed.

Expect roughly an hour on a native host with KVM and a good connection.

### What the build does

1. Downloads the pinned Ubuntu minimal cloud image (`UBUNTU_BUILD` in
   `versions.env`) and checks it against Ubuntu's SHA256 list.
2. **Provisioning boot:** boots it with a NoCloud seed that copies `guest/`
   into the VM and runs `guest/provision.sh`. That installs containerd,
   Kubernetes, IG and the registry, pre-pulls every image, runs
   `guest/smoke.sh` (BTF present, a gadget pushed through the registry, real
   file-open events through the IG daemon), cleans up, and powers off. The log
   must end with `DEMO_IMAGE_READY`.
3. **Layer boot:** boots an overlay of that disk with an SMBIOS flag that runs
   `guest/provision-layer.sh` (Headlamp, Prometheus, Grafana) and
   `guest/smoke-observability.sh`. The log must end with `DEMO_LAYER_READY`.
4. **Three cold boots** with WAN access blocked, each on a fresh overlay,
   sharing one registry disk: twice on the minimal image, once on the layer.
   Each checks Kubernetes TLS access through the forwarded API, that a
   namespace created on the first boot is gone on the next (fresh cluster each
   launch), and that a registry blob pushed on the first boot survives. The
   layer boot also checks Headlamp and Grafana metrics.
5. **Packaging:** `qemu-img check`, zlib-compressed copies, the layer
   repacked against `ig-minimal-<arch>.qcow2` by bare filename, then
   `SHA256SUMS-<arch>` and finally `manifest-<arch>.json`.

### Where to look when it fails

Everything intermediate is in `build/ubuntu-registry-<arch>/`:

| File | Boot |
|------|------|
| `provision.log` | Provisioning boot (serial console). Search for `DEMO_IMAGE_FAILED`; the lines before it show the journal and pod state. |
| `layer-provision.log` | Layer boot. Search for `DEMO_LAYER_FAILED`. |
| `cold-boot-1.log` … `cold-boot-3.log` | Offline cold boots. |

### Rebuilding

The builder refuses to overwrite a previous build. Move the old directory
aside first (the downloaded Ubuntu image can be copied back to skip that
download):

```sh
mv build/ubuntu-registry-arm64 build/ubuntu-registry-arm64.old
mkdir build/ubuntu-registry-arm64
cp build/ubuntu-registry-arm64.old/ubuntu-24.04-minimal-cloudimg-arm64.img build/ubuntu-registry-arm64/
python3 scripts/build-image.py --arch arm64
```

Builds are version-pinned (`versions.env`), not bit-for-bit reproducible: apt
packages resolve at build time.

## 4. Test the image

The build already tests the launcher path (step 4 above). Lima is a
different path and is **not** covered by the build, so test it by hand.

### Lima (macOS or Linux)

Needs Lima 2.0+ (`brew install lima`; on Linux also QEMU). Copy
`dist/ubuntu-registry/ig-minimal-<arch>.qcow2` and `SHA256SUMS-<arch>` to the
test machine, then from a checkout of this repository:

```sh
lima/ig-demo start --image /path/to/ig-minimal-arm64.qcow2
eval "$(lima/ig-demo env)"
kubectl get nodes                                   # one node "demo", Ready
kubectl -n gadget get pods                          # gadget pod Running

# trace_open only reports containers, so give it one that opens files.
kubectl run opener --image=busybox:1.36 --restart=Never -- \
  sh -c 'while true; do cat /etc/hostname /etc/passwd >/dev/null; sleep 0.3; done'
kubectl wait --for=condition=Ready pod/opener --timeout=120s
kubectl gadget run trace_open:v0.57.0 --timeout 5   # prints file-open events
kubectl delete pod opener --now
```

`start` checks the image against `SHA256SUMS-<arch>` beside it, creates the
VM `ig-demo` from `lima/ig-demo.yaml`, waits for the cluster, writes
`~/.ig-demo/kubeconfig` (API at `127.0.0.1:18443`), and downloads `kubectl`
and `kubectl-gadget` matching the VM into `~/.ig-demo/bin` (about 90 MB).

Also check, inside the VM (`lima/ig-demo shell`):

```sh
systemctl --failed                      # expect: 0 loaded units listed
systemctl status demo-network demo-registry demo --no-pager
ip -4 addr show lo                      # 10.0.2.15/32 on lo
cloud-init status                       # status: done
```

Then restart to check persistence: `lima/ig-demo stop && lima/ig-demo start`,
and confirm the node is Ready again. Remove the VM with `lima/ig-demo delete`.

If it does not come up:

- `~/.lima/ig-demo/serial*.log` — the guest console (boot and mount errors).
- `~/.lima/ig-demo/ha.stderr.log` — Lima itself (SSH, port forwarding).
- In the VM: `sudo journalctl -b -u demo-network -u demo-registry -u demo -u kubelet`.

Most likely trouble spots: the boot waiting on a disk ID Lima does not set
(`/etc/fstab`), cloud-init not running (Lima then waits for SSH forever), or
the API port forward not appearing.

### Launcher

Start the desktop app, use **Choose image file…**, pick
`ig-minimal-<arch>.qcow2` (with `ig-registry-seed-<arch>.qcow2` and, on
arm64, `QEMU_EFI-arm64.fd` beside it), start, and check that it becomes ready.
Pick `ig-observability-<arch>.qcow2` to test the full variant: the Headlamp
and Grafana icons should appear.

## 5. Repository checks (no VM needed)

```sh
shellcheck guest/*.sh scripts/*.sh lima/ig-demo
python3 -m unittest discover -s tests -v
```
