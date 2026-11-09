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
# Available after every cold boot; IG's own ig_* series only appear once a gadget has run.
MONITORING_QUERIES = ['go_goroutines{namespace="gadget"}', "node_load1"]
# Bump when a guest change would break launchers built for the previous value.
GUEST_API = 1


def versions():
    return dict(line.split("=", 1) for line in (ROOT / "versions.env").read_text().splitlines()
                if line and not line.startswith("#"))


def run(*args):
    subprocess.run([str(x) for x in args], check=True)


def download(url, target):
    print(f"Downloading {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ig-demo-image-builder"})
    with urllib.request.urlopen(req, timeout=120) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)


def sha256_digest(contents, filename):
    for line in contents.splitlines():
        fields = line.split()
        if (len(fields) == 2 and fields[1].removeprefix("*") == filename and len(fields[0]) == 64
                and all(c in "0123456789abcdef" for c in fields[0].lower())):
            return fields[0].lower()
    raise ValueError(f"No SHA256 digest for {filename}")


def cloud_config():
    files = []
    for path in [*sorted(p for p in (ROOT / "guest").iterdir() if p.is_file()), ROOT / "versions.env"]:
        dest = f"/etc/systemd/system/{path.name}" if path.suffix == ".service" else f"/opt/demo/{path.name}"
        files.append({"path": dest, "permissions": "0755" if path.suffix == ".sh" else "0644",
                      "encoding": "b64", "content": base64.b64encode(path.read_bytes()).decode()})
    return {"hostname": "ig-demo", "manage_etc_hosts": True, "ssh_pwauth": False,
            "disable_root": True, "write_files": files, "runcmd": [["/opt/demo/provision.sh"]]}


def qemu_args(arch, disk, registry, firmware, port, token, seed=None, offline=False, legacy=None,
              provision=None):
    if registry is None:
        raise ValueError("A formatted registry disk is required")
    network = (f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:8081,"
               "hostfwd=tcp:127.0.0.1:18443-:8443,"
               "hostfwd=tcp:127.0.0.1:18080-:30080,"
               "hostfwd=tcp:127.0.0.1:18090-:30090,"
               "hostfwd=tcp:127.0.0.1:15000-:30500")
    if offline:
        network += ",restrict=on"
    smbios = f"type=11,value=io.inspektor-gadget.demo.token={token}"
    if provision:
        smbios += f",value=io.inspektor-gadget.demo.provision={provision}"
    args = [f"qemu-system-{'x86_64' if arch == 'amd64' else 'aarch64'}",
            "-m", "4096", "-smp", "2", "-display", "none", "-serial", "stdio",
            "-monitor", "none", "-no-reboot",
            "-smbios", smbios,
            "-netdev", network, "-device", "virtio-net-pci,netdev=net0"]
    disks = [("os", disk, "qcow2", "ig-os"), ("registry", registry, "qcow2", "ig-registry")]
    if legacy is not None:
        disks.append(("legacy", legacy, "qcow2", "ig-legacy"))
    if seed is not None:
        disks.append(("seed", seed, "raw", None))
    for name, path, driver, serial in disks:
        block = {"driver": driver, "node-name": name,
                 "file": {"driver": "file", "filename": str(path)}}
        if name == "seed":
            block["read-only"] = True
        else:
            block.update({"discard": "unmap", "detect-zeroes": "unmap"})
        device = f"virtio-blk-pci,drive={name}"
        if serial:
            device += f",serial={serial}"
        args += ["-blockdev", json.dumps(block), "-device", device]
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    kvm = arch == native and os.access("/dev/kvm", os.R_OK | os.W_OK)
    args += ["-accel", "kvm" if kvm else "tcg,thread=multi",
             "-cpu", "host" if kvm else "max"]
    if arch == "arm64":
        args += ["-machine", "virt,gic-version=3", "-bios", str(firmware)]
    else:
        args += ["-machine", "q35"]
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
            raise RuntimeError("VM exited before becoming ready; inspect cold-boot logs")
        try:
            result = request(port, token, "status")
            print(result["message"], flush=True)
            if result["ready"]:
                if result.get("registry_persistent") is not True:
                    raise RuntimeError("Ready image does not have a mounted persistent registry")
                return result
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(5)
    raise TimeoutError("Cold boot did not become ready; inspect cold-boot logs")


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
    return context


def check_headlamp():
    with urllib.request.urlopen("http://127.0.0.1:18080/", timeout=30) as response:
        assert response.status == 200


def check_monitoring(base="http://127.0.0.1:18090", timeout=300):
    """Prove Grafana needs no login and Prometheus scrapes IG and the node through it."""
    end = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{base}/api/org", timeout=30):
                pass
            if all(_has_series(base, query) for query in MONITORING_QUERIES):
                return
        except (OSError, ValueError):
            pass
        if time.monotonic() >= end:
            raise TimeoutError("Grafana did not serve IG and node metrics without a login")
        time.sleep(5)


def _has_series(base, query):
    path = f"/api/datasources/proxy/uid/prometheus/api/v1/query?query={urllib.parse.quote(query)}"
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return bool(json.load(response)["data"]["result"])


def sha256_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_layer(layer, output, base_name):
    """Store only the layer's changes, backed by the base by bare filename so folders can move."""
    arch = base_name.removeprefix("ig-minimal-").removesuffix(".qcow2")
    target = output / f"ig-observability-{arch}.qcow2"
    run("qemu-img", "convert", "-c", "-O", "qcow2", "-B", base_name, "-F", "qcow2", layer, target)
    run("qemu-img", "check", target)
    return target


def write_manifest(output, arch, release, files):
    names = [path.name for path in files]
    support = [name for name in [f"ig-registry-seed-{arch}.qcow2", "QEMU_EFI-arm64.fd"] if name in names]
    base, layer = f"ig-minimal-{arch}.qcow2", f"ig-observability-{arch}.qcow2"
    manifest = {
        "guest_api": GUEST_API,
        "release": release,
        "arch": arch,
        "versions": versions(),
        "files": {path.name: {"size": path.stat().st_size, "sha256": sha256_file(path)} for path in files},
        "variants": {
            "minimal": {"chain": [base], "support": support},
            "observability": {"chain": [base, layer], "support": support},
        },
    }
    path = output / f"manifest-{arch}.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def boot_provisioning(args, disk, registry, firmware, token, log_path, marker, seed=None, provision=None):
    print(f"Provisioning VM; log: {log_path}", flush=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(qemu_args(args.arch, disk, registry, firmware, args.port, token, seed,
                                             provision=provision),
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            if process.wait(timeout=args.timeout):
                raise RuntimeError("Provisioning VM failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if marker not in log_path.read_text(errors="replace"):
        raise RuntimeError(f"Provisioning failed; inspect {log_path.name}")


def cold_boot(args, session, registry, firmware, token, log_path, checks):
    print(f"Cold boot with WAN blocked; log: {log_path}", flush=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            qemu_args(args.arch, session, registry, firmware, args.port, token, offline=True),
            stdout=log, stderr=subprocess.STDOUT)
        try:
            state = wait_ready(process, args.port, token, args.timeout)
            credentials = request(args.port, token, "credentials")
            assert credentials["token"]
            checks(state, check_host_access(credentials))
            request(args.port, token, "shutdown")
            if process.wait(timeout=120):
                raise RuntimeError(f"Cold boot shutdown failed; inspect {log_path.name}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def upload_registry_blob(blob):
    registry = "http://127.0.0.1:15000"
    req = urllib.request.Request(f"{registry}/v2/demo/build-smoke/blobs/uploads/", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        location = urllib.parse.urlsplit(response.headers["Location"])
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    query = urllib.parse.parse_qsl(location.query) + [("digest", digest)]
    url = f"{registry}{location.path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, data=blob, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as response:
        assert response.status == 201


def check_registry_blob(blob):
    registry = "http://127.0.0.1:15000"
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    with urllib.request.urlopen(f"{registry}/v2/demo/build-smoke/blobs/{digest}", timeout=30) as response:
        assert response.read() == blob


def check_os_reset(context, namespace, first_boot):
    url = "https://127.0.0.1:18443/api/v1/namespaces"
    if first_boot:
        body = json.dumps({"apiVersion": "v1", "kind": "Namespace",
                           "metadata": {"name": namespace}}).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=context, timeout=30) as response:
            assert response.status == 201
    else:
        try:
            with urllib.request.urlopen(f"{url}/{namespace}", context=context, timeout=30):
                pass
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            raise RuntimeError("OS state persisted after discarding its session overlay")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "ubuntu-registry")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--release", default=os.environ.get("IMAGE_RELEASE", "local"),
                        help="Release name recorded in the manifest")
    parser.add_argument("--resume", action="store_true",
                        help="Retest and package an already successfully provisioned disk")
    args = parser.parse_args()
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux":
        parser.error("Build on Linux; the launcher supports Linux, macOS and Windows")
    if native != args.arch:
        print("Cross-architecture build: using slow software emulation; prefer a native CI runner.", flush=True)
    for tool in [f"qemu-system-{'x86_64' if args.arch == 'amd64' else 'aarch64'}", "qemu-img",
                 "cloud-localds", "mkfs.ext4"]:
        if not shutil.which(tool):
            parser.error(f"Install missing build dependency: {tool}")
    work = ROOT / "build" / f"ubuntu-registry-{args.arch}"
    work.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    v = versions()
    disk = work / "demo.qcow2"
    registry_seed = work / "registry-seed.qcow2"
    raw = work / "registry.raw"
    config = work / "user-data"
    seed = work / "seed.img"
    rendered = "#cloud-config\n" + json.dumps(cloud_config(), indent=2) + "\n"
    if args.resume:
        if not all(path.exists() for path in [disk, registry_seed, config, work / "provision.log"]):
            parser.error("--resume requires successfully provisioned OS and registry disks, user-data and provision.log")
        if config.read_text() != rendered:
            parser.error("Guest files or versions changed; rebuild rather than resume")
    elif any(path.exists() for path in [disk, registry_seed, raw]):
        parser.error(f"Image disks already exist in {work}; move them aside before rebuilding")
    if not args.resume:
        release_url = (f"https://cloud-images.ubuntu.com/minimal/releases/{v['UBUNTU_CODENAME']}/"
                       f"release-{v['UBUNTU_BUILD']}")
        filename = f"ubuntu-{v['UBUNTU_RELEASE']}-minimal-cloudimg-{args.arch}.img"
        base = work / filename
        checksums = work / "SHA256SUMS"
        download(f"{release_url}/SHA256SUMS", checksums)
        expected = sha256_digest(checksums.read_text(), filename)
        actual = None
        if base.exists():
            with base.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            download(f"{release_url}/{filename}", base)
        with base.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise RuntimeError("Ubuntu image SHA256 checksum mismatch")
        config.write_text(rendered)
        metadata = work / "meta-data"
        metadata.write_text(f"instance-id: ig-demo-{args.arch}-{v['UBUNTU_BUILD']}\nlocal-hostname: ig-demo\n")
        run("cloud-localds", seed, config, metadata)
        run("qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", base, disk)
        run("qemu-img", "resize", disk, "16G")
        with raw.open("xb") as stream:
            stream.truncate(16 * 1024 ** 3)
        run("mkfs.ext4", "-F", "-L", "ig-registry", raw)
        run("qemu-img", "convert", "-f", "raw", "-O", "qcow2", raw, registry_seed)
        raw.unlink()
    firmware = work / "QEMU_EFI-arm64.fd"
    if args.arch == "arm64" and not firmware.exists():
        shutil.copyfile("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", firmware)
    token = secrets.token_hex(32)
    if not args.resume:
        boot_provisioning(args, disk, registry_seed, firmware, token, work / "provision.log",
                          "DEMO_IMAGE_READY", seed=seed)
    if "DEMO_IMAGE_READY" not in (work / "provision.log").read_text(errors="replace"):
        raise RuntimeError("Provisioning failed; inspect provision.log")
    disk.chmod(0o444)
    registry_seed.chmod(0o444)
    layer = work / "observability.qcow2"
    layer_log = work / "layer-provision.log"
    if not (args.resume and layer.exists() and layer_log.exists()
            and "DEMO_LAYER_READY" in layer_log.read_text(errors="replace")):
        if layer.exists():
            layer.chmod(0o644)
            layer.unlink()
        run("qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", disk, layer)
        with tempfile.TemporaryDirectory(prefix="layer-", dir=work) as directory:
            # The seed ships with the base smoke test's trace_open:demo; keep the layer boot's
            # registry writes out of it.
            scratch = Path(directory) / "registry.qcow2"
            run("qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", registry_seed, scratch)
            boot_provisioning(args, layer, scratch, firmware, token, layer_log, "DEMO_LAYER_READY",
                              provision="observability")
    layer.chmod(0o444)
    blob = f"IG demo persistent registry smoke test {secrets.token_hex(32)}\n".encode()
    namespace = f"ig-session-{secrets.token_hex(12)}"
    with tempfile.TemporaryDirectory(prefix="cold-", dir=work) as directory:
        cold = Path(directory)
        registry = cold / "registry.qcow2"
        run("qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", registry_seed, registry)
        boots = [(1, disk, []), (2, disk, []), (3, layer, ["observability"])]
        for boot, image, expected_layers in boots:
            session = cold / f"session-{boot}.qcow2"
            run("qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", image, session)

            def checks(state, context, boot=boot, expected_layers=expected_layers):
                if state.get("layers") != expected_layers:
                    raise RuntimeError(f"Boot {boot} reported layers {state.get('layers')}")
                if expected_layers:
                    check_headlamp()
                    check_monitoring()
                check_os_reset(context, namespace, first_boot=boot == 1)
                if boot == 1:
                    upload_registry_blob(blob)
                check_registry_blob(blob)

            cold_boot(args, session, registry, firmware, token, work / f"cold-boot-{boot}.log", checks)
            session.unlink()
    base_name = f"ig-minimal-{args.arch}.qcow2"
    image = output / base_name
    registry_image = output / f"ig-registry-seed-{args.arch}.qcow2"
    for source, target in [(disk, image), (registry_seed, registry_image)]:
        run("qemu-img", "check", source)
        run("qemu-img", "convert", "-c", "-O", "qcow2", source, target)
    run("qemu-img", "check", layer)
    layer_image = package_layer(layer, output, base_name)
    release_files = [image, layer_image, registry_image]
    if args.arch == "arm64":
        target = output / "QEMU_EFI-arm64.fd"
        shutil.copyfile(firmware, target)
        release_files.append(target)
    versions_file = output / f"versions-{args.arch}.env"
    versions_file.write_text((ROOT / "versions.env").read_text())
    release_files.append(versions_file)
    sums = [f"{sha256_file(path)}  {path.name}" for path in release_files]
    (output / f"SHA256SUMS-{args.arch}").write_text("\n".join(sums) + "\n")
    # Written last: a published manifest means every file it lists is already uploaded.
    write_manifest(output, args.arch, args.release, release_files)
    print(f"Verified images: {image}, {layer_image}", flush=True)


if __name__ == "__main__":
    main()
