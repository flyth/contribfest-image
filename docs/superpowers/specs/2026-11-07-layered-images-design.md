# Layered demo images and in-app download

Date: 2026-11-07
Status: approved design, not yet planned

## Goal

Users choose, before downloading anything, between two variants of the demo VM:

- **IG only** — Kubernetes, Inspektor Gadget, and the local registry.
- **IG + Headlamp + Grafana** — the above plus Headlamp, Prometheus,
  node-exporter, and Grafana.

The app downloads the chosen variant from GitHub releases, or the user points
it at image files already on disk. Image releases ship independently of app
releases.

## Decisions

- The registry belongs to the base variant.
- The full variant is a qcow2 layer backed by the base, not a second full image.
  A user who starts with "IG only" can add the layer later by downloading only
  the layer.
- Images are released from this repository under `images-v*` tags. App
  releases keep their `v*` tags.
- The app picks the newest image release whose `guest_api` it supports, rather
  than pinning an exact release or following "latest" blindly.
- Updates are never automatic. The app offers "Update available (resets
  cluster)" and acts only on click.
- Image releases are published, not drafts, so the app can download without
  GitHub credentials.

## Images

### Files per architecture

| File | Contents |
|------|----------|
| `ig-minimal-<arch>.qcow2` | OS, Kubernetes, IG, registry |
| `ig-observability-<arch>.qcow2` | Layer: Headlamp, Prometheus, node-exporter, Grafana |
| `ig-registry-seed-<arch>.qcow2` | Empty registry disk (unchanged) |
| `QEMU_EFI-arm64.fd` | arm64 firmware (unchanged, arm64 only) |
| `versions-<arch>.env` | Pinned component versions (unchanged) |
| `manifest-<arch>.json` | Release description, see below |
| `SHA256SUMS-<arch>` | Kept for manual verification |

The layer is produced with
`qemu-img convert -c -O qcow2 -B ig-minimal-<arch>.qcow2 -F qcow2`, after
provisioning the observability components on top of a booted base. Its backing
reference is the bare filename, so the release folder can be moved as a unit.
A layer only works on top of the exact base from the same release.

### Guest layer marker

Provisioning the observability layer writes `/opt/demo/layers/observability`.
`guest/api.py`:

- requires the `prometheus` and `grafana` deployments, the `node-exporter`
  daemonset, and the Headlamp and Grafana NodePort probes only when the marker
  exists;
- reports the installed layers in its status reply, e.g.
  `"layers": ["observability"]`.

The base variant is ready once IG, the cluster, and the registry are ready.

### Manifest

```json
{
  "guest_api": 1,
  "release": "images-v1.2.0",
  "arch": "arm64",
  "versions": {"inspektor_gadget": "v0.57.0"},
  "files": {
    "ig-minimal-arm64.qcow2": {"size": 0, "sha256": "..."},
    "ig-observability-arm64.qcow2": {"size": 0, "sha256": "..."},
    "ig-registry-seed-arm64.qcow2": {"size": 0, "sha256": "..."},
    "QEMU_EFI-arm64.fd": {"size": 0, "sha256": "..."}
  },
  "variants": {
    "minimal": {
      "chain": ["ig-minimal-arm64.qcow2"],
      "support": ["ig-registry-seed-arm64.qcow2", "QEMU_EFI-arm64.fd"]
    },
    "observability": {
      "chain": ["ig-minimal-arm64.qcow2", "ig-observability-arm64.qcow2"],
      "support": ["ig-registry-seed-arm64.qcow2", "QEMU_EFI-arm64.fd"]
    }
  }
}
```

`guest_api` is bumped whenever a guest change would break an older app: ports,
API commands or replies, readiness semantics, or file layout. `chain` lists
disks from base to top.

### Builder and CI

- `scripts/build-image.py` builds the base, smoke-tests it, then boots it
  again to provision the observability layer and smoke-tests that. Cold-boot
  offline checks, registry persistence, and OS reset run for both variants;
  the Grafana monitoring check runs only for the observability variant.
- The builder writes `manifest-<arch>.json` and `SHA256SUMS-<arch>`.
- `.github/workflows/images.yml` triggers on `images-v*` tags and publishes a
  non-draft release with all files for both architectures.

## App

### First run: choose and download

When no image is installed, the main view shows two cards, "IG only" and "IG +
Headlamp + Grafana", each with its download size from the manifest, plus
"Choose image…".

Release discovery:

1. List releases through the public GitHub API, keeping `images-v*` tags.
2. For each, newest first, fetch `manifest-<arch>.json` and take the first
   whose `guest_api` the app supports.
3. If none is compatible, explain that the app needs an update.

Download:

- Files go to `<app data>/images/<release>/`.
- Each file downloads to `<name>.part` and resumes via HTTP `Range` after an
  interruption.
- Each file is checked against the manifest sha256 before it is renamed into
  place. A mismatch deletes the partial file and reports an error.
- The UI shows overall progress and allows cancel.

Adding Headlamp + Grafana later downloads only the layer file into the same
release folder.

### Local images

"Choose image file…" keeps picking a qcow2: `ig-minimal-<arch>.qcow2` or
`ig-observability-<arch>.qcow2`, with the seed, firmware and (for the layer)
the base beside it. The layer finds its base through its relative backing
reference, so local builds and copied release folders work unchanged.
(Planning simplification: no manifest picker; the guest reports its layers,
which drives the rail icons either way.)

### Running

- `session.qcow2` is created with the top of the chosen variant's chain as its
  backing file.
- `registry.qcow2` is unchanged: created once from the seed, kept across
  variant switches, updates, and resets.
- The Headlamp and Grafana rail icons appear only when the guest status lists
  the `observability` layer. Their webviews close when the VM stops.

### Switching and updating

- Switching variant or updating to a newer release only changes which disk
  the next `session.qcow2` is backed by. The cluster is already fresh on every
  start (the session overlay is recreated each time); the registry is kept.
- On start, the app checks for a newer compatible release and, if one exists,
  shows "Update available". Nothing happens until the user clicks it.
- After the updated VM reaches ready, the previous release folder is deleted.

## Testing

- **Python**: manifest generation and sizes/checksums; readiness with and
  without the layer marker; the layer's backing reference is the base filename;
  workflow triggers on `images-v*` and does not create drafts.
- **Rust**: compatible-release selection (skips newer incompatible releases,
  errors when none fit); manifest parsing and validation; download resume and
  checksum rejection against a local fake HTTP server; session chain
  recreation on variant switch and update; registry disk survives both.
- **Full build**: one local arm64 build covering both variants before release.
- **Manual**: real desktop app on a host with acceleration — first-run
  download of each variant, adding the layer later, update flow.

## Out of scope

- More than one optional layer, or layers combinable in any order.
- Delta updates between releases.
- Gadget data or instances shipped by default.
