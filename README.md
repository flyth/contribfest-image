# Inspektor Gadget ContribFest: your own cluster

Run your own gadgets on a real Kubernetes cluster on your laptop. One
command starts a small VM with a single-node Kubernetes cluster and
[Inspektor Gadget](https://inspektor-gadget.io) already installed. It sets up
`kubectl` and `kubectl-gadget` for you.

- It works on **macOS, Windows (WSL2) and Linux**, on x86_64 and Arm.
- It doesn't touch your other clusters: it has its own kubeconfig and leaves
  `~/.kube/config` alone.
- Nothing from your computer is shared with the VM. Only the Kubernetes API is
  reachable, and only from your computer, at `127.0.0.1:18443`.

> [!IMPORTANT]
> **Before the event:** do [step 1](#1-download-the-image) now. The image is
> about 600 MB, and conference Wi-Fi is slow.

## Contents

1. [Download the image](#1-download-the-image)
2. [Get the scripts](#2-get-the-scripts)
3. [Set up your computer and start the cluster](#3-set-up-your-computer-and-start-the-cluster):
   [macOS](#macos) · [Windows (WSL2)](#windows-wsl2) · [Linux](#linux)
4. [Run a gadget](#4-run-a-gadget)
5. [Stop, restart and clean up](#5-stop-restart-and-clean-up)
6. [Troubleshooting](#troubleshooting)

**What you need:**
- A 64-bit Intel/AMD or Arm computer with virtualization enabled.
- About 4 GB of free memory: the VM gets 4 GiB, so 8 GB total is the minimum
  and 16 GB is comfortable.
- 2 CPU cores.
- About 5 GB of free disk space.

## 1. Download the image

Download the image that matches your computer. Optionally, also download the
checksum file into the same folder, and `start` verifies the image before
using it.

| Your computer | Image | Checksum |
|---|---|---|
| Mac with Apple Silicon (M1 or newer) | [ig-minimal-arm64.qcow2][arm64] (about 530 MB) | [SHA256SUMS-arm64][arm64-sums] |
| Mac with Intel CPU | [ig-minimal-amd64.qcow2][amd64] (about 610 MB) | [SHA256SUMS-amd64][amd64-sums] |
| Windows PC (x86_64) | [ig-minimal-amd64.qcow2][amd64] | [SHA256SUMS-amd64][amd64-sums] |
| Linux, x86_64 | [ig-minimal-amd64.qcow2][amd64] | [SHA256SUMS-amd64][amd64-sums] |
| Linux, Arm (aarch64) | [ig-minimal-arm64.qcow2][arm64] | [SHA256SUMS-arm64][arm64-sums] |

Not sure? Run `uname -m` in a terminal (on Windows, in your WSL terminal).
`x86_64` means **amd64**; `arm64` or `aarch64` means **arm64**.

All files are on the [release page][release]. Keep the file where your browser
saved it. You'll point the start command at it in step 3, so it isn't
downloaded again.

## 2. Get the scripts

You need the two files in [`lima/`](lima). Clone this repository:

```sh
git clone https://github.com/flyth/contribfest-image.git contribfest
cd contribfest
```

Or download it as a [ZIP file][zip], unpack it, and open a terminal in the
unpacked folder. On Windows, do this inside WSL; see
[Windows (WSL2)](#windows-wsl2).

All commands below run from this folder.

## 3. Set up your computer and start the cluster

Pick your system: [macOS](#macos) · [Windows (WSL2)](#windows-wsl2) · [Linux](#linux)

The first start takes one to two minutes. It also downloads `kubectl` and
`kubectl-gadget` (about 90 MB) into `~/.ig-demo/bin`. When it prints
**The cluster is ready**, continue with [Run a gadget](#4-run-a-gadget).

### macOS

Needs macOS 13 or newer.

1. Install [Lima](https://lima-vm.io) with [Homebrew](https://brew.sh):

   ```sh
   brew install lima
   ```

2. Start the cluster with the image from step 1:

   ```sh
   lima/ig-demo start --image ~/Downloads/ig-minimal-arm64.qcow2   # Intel Mac: ig-minimal-amd64.qcow2
   ```

That's it: macOS has the virtualization Lima needs built in.

### Windows (WSL2)

Needs **Windows 11** on an x86_64 PC. Windows 10 and Windows on Arm can't run
the VM inside WSL2. Everything below runs in your **Ubuntu (WSL) terminal**,
except where it says PowerShell.

1. **Install WSL2 with Ubuntu**, if you don't have it yet. In PowerShell, run
   `wsl --install -d Ubuntu`, restart Windows, then open **Ubuntu** from the
   Start menu.

2. **Give WSL2 enough memory and nested virtualization.** In Windows, create
   or edit the file `C:\Users\<you>\.wslconfig`:

   ```ini
   [wsl2]
   nestedVirtualization=true
   # The VM needs 4 GiB on top of WSL itself. 8GB suits a 16 GB laptop.
   memory=8GB
   ```

   Then, in PowerShell, run `wsl --shutdown` and reopen Ubuntu.

3. **Install QEMU and allow yourself to use KVM:**

   ```sh
   sudo apt update
   sudo apt install qemu-system-x86 qemu-utils
   ls -l /dev/kvm                 # must exist; see Troubleshooting if not
   sudo usermod -aG kvm $USER
   ```

   The new group only applies after a full restart of WSL. In PowerShell, run
   `wsl --shutdown`, then reopen Ubuntu. Check with `id`: the list must include
   `kvm`. (`newgrp` isn't enough, and isn't available on newer Ubuntu.)

4. **Install Lima** (no root needed):

   ```sh
   mkdir -p ~/.local
   curl -fsSL "https://github.com/lima-vm/lima/releases/download/v2.3.0/lima-2.3.0-Linux-$(uname -m).tar.gz" |
     tar -xz -C ~/.local
   export PATH="$HOME/.local/bin:$PATH"   # new terminals do this by themselves
   limactl --version
   ```

5. **Get the scripts inside WSL:** run `cd ~`, then the `git clone` and `cd`
   commands from [step 2](#2-get-the-scripts). That keeps them in your Linux
   home folder rather than on `C:`.

6. **Start the cluster** with the image your Windows browser downloaded. WSL
   sees your `C:` drive at `/mnt/c`. Replace `<you>` with your Windows user name
   (`ls /mnt/c/Users` lists them):

   ```sh
   lima/ig-demo start --image "/mnt/c/Users/<you>/Downloads/ig-minimal-amd64.qcow2"
   ```

### Linux

1. **Install QEMU.** Debian and Ubuntu:

   ```sh
   sudo apt install qemu-system-x86 qemu-utils                     # x86_64
   sudo apt install qemu-system-arm qemu-efi-aarch64 qemu-utils    # Arm
   ```

   Fedora: `sudo dnf install qemu-kvm qemu-img`. On other distributions, install
   QEMU with `qemu-img` and the system emulator for your CPU
   (`qemu-system-x86_64` or `qemu-system-aarch64`).

2. **Make sure you can use KVM:**

   ```sh
   ls -l /dev/kvm                 # must exist; see Troubleshooting if not
   sudo usermod -aG kvm $USER
   ```

   Log out and back in (or reboot), then check with `id`: the list must include
   `kvm`. Some distributions already give you access; `start` checks it either
   way.

3. **Install Lima 2.0 or newer.** Distribution packages are often older, so use
   the release tarball (no root needed):

   ```sh
   mkdir -p ~/.local
   curl -fsSL "https://github.com/lima-vm/lima/releases/download/v2.3.0/lima-2.3.0-Linux-$(uname -m).tar.gz" |
     tar -xz -C ~/.local
   export PATH="$HOME/.local/bin:$PATH"   # most distributions add this at login
   limactl --version
   ```

   With Homebrew on Linux, `brew install lima` works too.

4. **Start the cluster** with the image from step 1:

   ```sh
   lima/ig-demo start --image ~/Downloads/ig-minimal-amd64.qcow2   # Arm: ig-minimal-arm64.qcow2
   ```

## 4. Run a gadget

Point `kubectl` at the cluster, in **every new terminal**:

```sh
eval "$(lima/ig-demo env)"
kubectl get nodes              # one node "demo", Ready
```

**Try a built-in gadget.** `trace_open` shows files that containers open, so
start a container that opens files, then trace it:

```sh
kubectl run opener --image=busybox:1.36 --restart=Never -- \
  sh -c 'while true; do cat /etc/hostname >/dev/null; sleep 1; done'
kubectl gadget run trace_open:v0.57.0     # Ctrl-C to stop
kubectl delete pod opener
```

**Run your own gadget.** Create your gadget from
[gadget-template](https://github.com/inspektor-gadget/gadget-template). Each push
to its default branch publishes `ghcr.io/<owner>/<repository>:latest`:

```sh
kubectl gadget run ghcr.io/<owner>/<repository>:latest
```

Use lowercase owner and repository names. The cluster pulls straight from
GHCR. **Make the package public first**; see
[Pulling my gadget fails](#pulling-my-gadget-fails). To pick up a new
push, add `--pull=always`, or run the exact `:sha-<commit>` tag.

## 5. Stop, restart and clean up

```sh
lima/ig-demo stop      # shut the VM down; the cluster is kept
lima/ig-demo start     # start it again (no --image needed)
lima/ig-demo status    # is it running?
lima/ig-demo shell     # a shell inside the VM
lima/ig-demo delete    # remove the VM; the next start begins fresh
```

To remove everything after the event: `lima/ig-demo delete`, then
`rm -rf ~/.ig-demo` (kubeconfig and tools), and delete the downloaded image.

## Troubleshooting

Start with the error `ig-demo` prints. It checks the common problems before
doing anything and says how to fix them. Lima's own log is
`~/.lima/ig-demo/ha.stderr.log`, and the VM's console is
`~/.lima/ig-demo/serial*.log`.

- [Stuck at "Waiting for the essential requirement… ssh"](#stuck-waiting-for-ssh)
- [`no access to /dev/kvm`](#no-access-to-devkvm)
- [`no /dev/kvm`](#no-devkvm)
- [The VM stops right away, or WSL becomes very slow](#not-enough-memory)
- [`the VM already exists`](#the-vm-already-exists)
- [`kubectl` can't connect, or talks to another cluster](#kubectl-cant-connect)
- [Pulling my gadget fails](#pulling-my-gadget-fails)
- [The gadget runs, but shows nothing](#the-gadget-shows-nothing)
- [`checksum mismatch` or `does not match`](#checksum-mismatch)
- [`limactl: command not found`](#limactl-command-not-found)

### Stuck waiting for SSH

- **You downloaded the wrong image.** An Arm image doesn't boot on x86_64,
  and an amd64 image doesn't boot on Arm. Compare with
  [the table](#1-download-the-image).
- **The VM was created by an older version of the scripts**, for example
  during a test run before the event. Update the scripts (`git pull`), then
  start fresh:
  ```sh
  lima/ig-demo delete
  lima/ig-demo start --image <your image>
  ```

### `no access to /dev/kvm`

Add yourself to the `kvm` group, then log in again (on WSL2: run
`wsl --shutdown` in PowerShell and reopen Ubuntu):

```sh
sudo usermod -aG kvm $USER
```

Need it right now? `sudo chmod 666 /dev/kvm` works until the next reboot.

### `no /dev/kvm`

The CPU's virtualization support is off, or not passed through.

- **Linux:** enable Intel VT-x or AMD-V/SVM in your BIOS/UEFI settings. If
  you are inside a VM yourself, enable nested virtualization for it.
- **WSL2:** you need Windows 11 and `nestedVirtualization=true` in `.wslconfig`
  ([step 2 for Windows](#windows-wsl2)), then `wsl --shutdown`. Virtualization
  must also be on in the BIOS/UEFI. Task Manager → Performance → CPU shows
  "Virtualization: Enabled".

### Not enough memory

The VM needs 4 GiB on top of what your system already uses.

- **WSL2:** raise `memory=` in `.wslconfig` (see
  [Windows step 2](#windows-wsl2)), then run `wsl --shutdown`.
- **Everywhere:** close memory-hungry apps, then `lima/ig-demo delete` and
  start again.

### The VM already exists

`--image` only applies when the VM is created. To keep the VM, run
`lima/ig-demo start` without `--image`. To switch images, run
`lima/ig-demo delete` first.

### `kubectl` can't connect

- **Wrong shell:** `eval "$(lima/ig-demo env)"` only affects the terminal you
  ran it in. Run it again in each new terminal.
- **VM not running:** `lima/ig-demo status` shows whether it's running.
  `lima/ig-demo start` starts it.
- **Port taken:** something else may be using port 18443: another Lima VM, a
  local cluster, or a second copy of this one. Check with
  `lsof -nP -iTCP:18443 -sTCP:LISTEN` and stop the other program.

### Pulling my gadget fails

Errors look like `unauthorized`, `denied` or `not found`.

- **Package is private:** GHCR packages start out private, and the cluster pulls
  anonymously. On GitHub, open your profile → **Packages** → your gadget →
  **Package settings** → **Change visibility** → **Public**.
- **Not published:** check that the publish job in your repository's
  **Actions** tab succeeded. It runs only on pushes to the default branch,
  after the **Initialize gadget** workflow has run.
- **Wrong name:** the image name is all lowercase:
  `ghcr.io/<owner>/<repository>:latest`.
- **Old version runs:** the cluster keeps the first `:latest` it pulled. Add
  `--pull=always`, or use the `:sha-<commit>` tag.

### The gadget shows nothing

Most gadgets only report events from containers. With no workload, there's
nothing to show. Start a pod that does what your gadget traces, like
the `opener` pod in [Run a gadget](#4-run-a-gadget).

### Checksum mismatch

The download is incomplete or damaged. Download the image again; the
checksum file must match the image's architecture.

### `limactl: command not found`

`~/.local/bin` isn't on your `PATH` yet. Run
`export PATH="$HOME/.local/bin:$PATH"`, or open a new terminal.

[release]: ../../releases/tag/images-v0.1.0
[arm64]: ../../releases/download/images-v0.1.0/ig-minimal-arm64.qcow2
[arm64-sums]: ../../releases/download/images-v0.1.0/SHA256SUMS-arm64
[amd64]: ../../releases/download/images-v0.1.0/ig-minimal-amd64.qcow2
[amd64-sums]: ../../releases/download/images-v0.1.0/SHA256SUMS-amd64
[zip]: ../../archive/refs/heads/main.zip
