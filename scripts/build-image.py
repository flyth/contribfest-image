#!/usr/bin/env python3
"""Bake and cold-boot-test a demo appliance on Linux (native KVM or QEMU TCG)."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def versions():
    return dict(line.split("=", 1) for line in (ROOT / "versions.env").read_text().splitlines()
                if line and not line.startswith("#"))


def run(*args):
    subprocess.run([str(x) for x in args], check=True)


def download(url, target):
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)


def qemu_args(arch, disk, firmware, port, token, seed=None, offline=False):
    network = (f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:8081,"
               "hostfwd=tcp:127.0.0.1:18443-:8443,"
               "hostfwd=tcp:127.0.0.1:18080-:30080,"
               "hostfwd=tcp:127.0.0.1:15000-:30500")
    if offline:
        network += ",restrict=on"
    args = [f"qemu-system-{'x86_64' if arch == 'amd64' else 'aarch64'}",
            "-m", "4096", "-smp", "2", "-display", "none", "-serial", "stdio",
            "-monitor", "none", "-no-reboot",
            "-smbios", f"type=11,value=io.inspektor-gadget.demo.token={token}",
            "-drive", f"file={disk},format=qcow2,if=virtio,discard=unmap,detect-zeroes=unmap",
            "-netdev", network,
            "-device", "virtio-net-pci,netdev=net0"]
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    kvm = arch == native and os.access("/dev/kvm", os.R_OK | os.W_OK)
    args += ["-accel", "kvm" if kvm else "tcg,thread=multi",
             "-cpu", "host" if kvm else "max"]
    if arch == "arm64":
        args += ["-machine", "virt,gic-version=3", "-bios", str(firmware)]
    else:
        args += ["-machine", "q35"]
    if seed:
        args += ["-drive", f"file={seed},format=raw,if=virtio,readonly=on"]
    return args


def request(port, token, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def wait_ready(process, port, token, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError("VM exited before becoming ready; inspect boot.log")
        try:
            result = request(port, token, "status")
            print(result["message"], flush=True)
            if result["ready"]:
                return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(5)
    raise TimeoutError("Cold boot did not become ready; inspect boot.log")


def check_host_access(credentials):
    config = credentials["kubeconfig"]
    cluster = config["clusters"][0]["cluster"]
    user = config["users"][0]["user"]
    context = ssl.create_default_context(cadata=base64.b64decode(cluster["certificate-authority-data"]).decode())
    with tempfile.TemporaryDirectory() as directory:
        cert, key = Path(directory) / "cert", Path(directory) / "key"
        cert.write_bytes(base64.b64decode(user["client-certificate-data"]))
        key.write_bytes(base64.b64decode(user["client-key-data"]))
        context.load_cert_chain(cert, key)
        with urllib.request.urlopen("https://127.0.0.1:18443/readyz", context=context, timeout=30) as response:
            assert response.read() == b"ok"
    with urllib.request.urlopen("http://127.0.0.1:18080/", timeout=30) as response:
        assert response.status == 200
    registry = "http://127.0.0.1:15000"
    req = urllib.request.Request(f"{registry}/v2/demo/build-smoke/blobs/uploads/", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        location = urllib.parse.urlsplit(response.headers["Location"])
    blob = b"IG demo external registry smoke test\n"
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    query = urllib.parse.parse_qsl(location.query) + [("digest", digest)]
    url = f"{registry}{location.path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, data=blob, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as response:
        assert response.status == 201
    with urllib.request.urlopen(f"{registry}/v2/demo/build-smoke/blobs/{digest}", timeout=30) as response:
        assert response.read() == blob


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--resume", action="store_true",
                        help="Retest and package an already successfully provisioned disk")
    args = parser.parse_args()
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux":
        parser.error("Build on Linux; the launcher supports Linux, macOS and Windows")
    if native != args.arch:
        print("Cross-architecture build: using slow software emulation; prefer a native CI runner.", flush=True)
    for tool in [f"qemu-system-{'x86_64' if args.arch == 'amd64' else 'aarch64'}",
                 "qemu-img", "cloud-localds"]:
        if not shutil.which(tool):
            parser.error(f"Install missing build dependency: {tool}")
    work = ROOT / "build" / args.arch
    work.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    v = versions()
    filename = f"ubuntu-{v['UBUNTU_RELEASE']}-minimal-cloudimg-{args.arch}.img"
    base = work / filename
    url = f"https://cloud-images.ubuntu.com/minimal/releases/{v['UBUNTU_CODENAME']}/release-{v['UBUNTU_BUILD']}"
    sums = work / "SHA256SUMS"
    download(f"{url}/SHA256SUMS", sums)
    expected = next(line.split()[0] for line in sums.read_text().splitlines()
                    if line.split()[-1].lstrip("*") == filename)
    actual = None
    if base.exists():
        with base.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        download(f"{url}/{filename}", base)
    with base.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise RuntimeError("Ubuntu image checksum mismatch")
    disk = work / "demo.qcow2"
    if args.resume:
        if not disk.exists():
            parser.error("--resume requires an existing provisioned demo.qcow2")
        previous = json.loads((work / "user-data").read_text().removeprefix("#cloud-config\n"))
        pinned = next(f for f in previous["write_files"] if f["path"] == "/opt/demo/versions.env")
        if base64.b64decode(pinned["content"]) != (ROOT / "versions.env").read_bytes():
            parser.error("Versions changed; rebuild rather than resume")
    elif disk.exists():
        parser.error(f"{disk} already exists; move it aside before rebuilding")
    if not args.resume:
        run("qemu-img", "convert", "-O", "qcow2", base, disk)
        run("qemu-img", "resize", disk, "16G")
    files = []
    for path in [*sorted(p for p in (ROOT / "guest").iterdir() if p.is_file()), ROOT / "versions.env"]:
        dest = f"/etc/systemd/system/{path.name}" if path.suffix == ".service" else f"/opt/demo/{path.name}"
        files.append({"path": dest, "encoding": "b64",
                      "content": base64.b64encode(path.read_bytes()).decode(),
                      "permissions": "0755" if path.suffix == ".sh" else "0644"})
    cloud = {"hostname": "ig-demo", "manage_etc_hosts": True,
             "ssh_pwauth": False, "disable_root": True,
             "write_files": files, "runcmd": [["bash", "/opt/demo/provision.sh"]]}
    (work / "user-data").write_text("#cloud-config\n" + json.dumps(cloud))
    (work / "meta-data").write_text("instance-id: ig-demo\nlocal-hostname: ig-demo\n")
    seed = work / "seed.img"
    run("cloud-localds", seed, work / "user-data", work / "meta-data")
    firmware = work / "QEMU_EFI.fd"
    if args.arch == "arm64":
        shutil.copyfile("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", firmware)
    token = secrets.token_hex(32)
    if not args.resume:
        print(f"Provisioning VM; log: {work / 'provision.log'}", flush=True)
        with (work / "provision.log").open("w") as log:
            process = subprocess.Popen(qemu_args(args.arch, disk, firmware, args.port, token, seed),
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                if process.wait(timeout=args.timeout):
                    raise RuntimeError("Provisioning VM failed")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    if "DEMO_IMAGE_READY" not in (work / "provision.log").read_text(errors="replace"):
        raise RuntimeError("Provisioning failed; inspect provision.log")
    print(f"Cold boot with WAN blocked; log: {work / 'boot.log'}", flush=True)
    with (work / "boot.log").open("w") as log:
        process = subprocess.Popen(qemu_args(args.arch, disk, firmware, args.port, token, offline=True),
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_ready(process, args.port, token, args.timeout)
            credentials = request(args.port, token, "credentials")
            assert credentials["token"]
            check_host_access(credentials)
            request(args.port, token, "shutdown")
            if process.wait(timeout=120):
                raise RuntimeError("Cold-boot shutdown failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    image = output / f"ig-demo-{args.arch}.qcow2"
    run("qemu-img", "check", disk)
    run("qemu-img", "convert", "-c", "-O", "qcow2", disk, image)
    release_files = [image]
    if args.arch == "arm64":
        target = output / "QEMU_EFI-arm64.fd"
        shutil.copyfile(firmware, target)
        release_files.append(target)
    sums = []
    for path in release_files:
        with path.open("rb") as stream:
            sums.append(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {path.name}")
    (output / f"SHA256SUMS-{args.arch}").write_text("\n".join(sums) + "\n")
    (output / f"versions-{args.arch}.env").write_text((ROOT / "versions.env").read_text())
    print(f"Verified image: {image}", flush=True)


if __name__ == "__main__":
    main()
