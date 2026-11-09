#!/usr/bin/env python3
"""Build a host source directory in a disposable pod and push to the demo registry."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "registry.demo.svc.cluster.local:5000"
NAMESPACE = "demo"
REPOSITORY = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
BUILD = """mkdir -p /work/input
 tar --no-same-owner -xf - -C /work/input
 cd /work/input/source
 /work/input/ig image build . --local --tag "$1"
 /work/input/ig image push "$1" --insecure-registries "$2"
"""


def image_reference(value):
    local = value.startswith(REGISTRY + "/")
    name = value.removeprefix(REGISTRY + "/")
    repository, separator, tag = name.rpartition(":")
    first = repository.split("/", 1)[0]
    if (not separator or not re.fullmatch(REPOSITORY, repository)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)
            or len(REGISTRY + "/" + repository) > 255
            or (not local and "/" in repository and ("." in first or first == "localhost"))):
        raise ValueError("Use a demo registry repository:tag, for example demo/my-gadget:dev")
    return REGISTRY + "/" + name


def sha256_digest(contents, filename):
    for line in contents.splitlines():
        fields = line.split()
        if (len(fields) == 2 and fields[1].removeprefix("*") == filename
                and re.fullmatch(r"[0-9a-fA-F]{64}", fields[0])):
            return fields[0].lower()
    raise ValueError(f"No SHA256 digest for {filename}")


def download(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "ig-demo-gadget-builder"})
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(request, timeout=120) as response:
                shutil.copyfileobj(response, output)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ig_archive(version, architecture):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"IG_VERSION must be a stable release: {version}")
    cache = ROOT / "build" / "gadget-tools" / version / architecture
    cache.mkdir(parents=True, exist_ok=True)
    filename = f"ig-linux-{architecture}-{version}.tar.gz"
    archive = cache / filename
    sums = cache / "SHA256SUMS"
    release = f"https://github.com/inspektor-gadget/inspektor-gadget/releases/download/{version}"
    if not sums.exists():
        download(f"{release}/SHA256SUMS", sums)
    expected = sha256_digest(sums.read_text(), filename)
    actual = None
    if archive.exists():
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        print(f"Downloading {filename}", flush=True)
        download(f"{release}/{filename}", archive)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"IG archive checksum mismatch: {archive}")
    return archive


def source_member(source, member):
    name = PurePosixPath(member.name)
    if ".git" in name.parts:
        return None
    path = source.joinpath(*name.relative_to("source").parts)
    target = path.resolve(strict=True)
    if not target.is_relative_to(source):
        raise ValueError(f"Source path escapes the uploaded directory: {path}")
    if member.issym():
        member.linkname = Path(os.path.relpath(target, path.parent)).as_posix()
    elif member.islnk():
        target = PurePosixPath(member.linkname)
        if not target.is_relative_to("source") or ".git" in target.parts:
            raise ValueError(f"Source hard link targets omitted files: {path}")
    elif not (member.isfile() or member.isdir()):
        raise ValueError(f"Unsupported source file type: {path}")
    return member


def source_archive(source, binary_archive, target):
    source = source.resolve(strict=True)
    with tarfile.open(binary_archive, "r:gz") as tools, tarfile.open(target, "w") as upload:
        executable = tools.getmember("ig")
        if not executable.isfile() or executable.size == 0:
            raise ValueError("IG release archive must contain a regular ig executable")
        member = tarfile.TarInfo("ig")
        member.mode = 0o755
        member.size = executable.size
        with tools.extractfile(executable) as stream:
            upload.addfile(member, stream)
        upload.add(source, arcname="source", filter=lambda item: source_member(source, item))


def node_architecture(nodes):
    available = set()
    for node in nodes.get("items", []):
        labels = node.get("metadata", {}).get("labels", {})
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in node.get("status", {}).get("conditions", []))
        if (ready and not node.get("spec", {}).get("unschedulable", False)
                and labels.get("kubernetes.io/os") == "linux"
                and labels.get("kubernetes.io/arch") in ("amd64", "arm64")):
            available.add(labels["kubernetes.io/arch"])
    if not available:
        raise ValueError("No ready, schedulable Linux amd64/arm64 node; start the demo first")
    return sorted(available)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Source directory and dependencies; build.yaml is optional")
    parser.add_argument("--tag", "-t", required=True, help="Demo registry repository:tag, e.g. demo/my-gadget:dev")
    parser.add_argument("--kubeconfig", type=Path, help="Exported kubeconfig; alternatively set KUBECONFIG explicitly")
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum pod lifetime in seconds (default: 1800)")
    args = parser.parse_args()
    try:
        source = args.source.resolve(strict=True)
        if not source.is_dir():
            raise ValueError("Source must be a directory")
        image = image_reference(args.tag)
        if args.timeout <= 0:
            raise ValueError("Timeout must be positive")
        configurations = ([args.kubeconfig] if args.kubeconfig is not None else
                          [Path(path) for path in os.environ.get("KUBECONFIG", "").split(os.pathsep) if path])
        if not configurations:
            raise ValueError("Pass the exported --kubeconfig or set KUBECONFIG; the global default context is not used")
        for configuration in configurations:
            if not configuration.is_file():
                raise ValueError(f"Kubeconfig must be a readable file: {configuration}")
            if configuration.resolve().is_relative_to(source):
                raise ValueError("Keep the kubeconfig outside the uploaded source directory")
        if not shutil.which("kubectl"):
            raise ValueError("Install kubectl and put it on PATH")
        pins = dict(line.split("=", 1) for line in (ROOT / "versions.env").read_text().splitlines()
                    if line and not line.startswith("#"))
        version = pins["IG_VERSION"]
        kubectl = ["kubectl"]
        if args.kubeconfig is not None:
            kubectl += ["--kubeconfig", str(args.kubeconfig.resolve())]
        nodes = json.loads(subprocess.check_output(kubectl + ["get", "nodes", "-o", "json"], timeout=30))
        architecture = node_architecture(nodes)
        tools = ig_archive(version, architecture)
        with tempfile.TemporaryDirectory(prefix="ig-source-") as directory:
            payload = Path(directory) / "source.tar"
            source_archive(source, tools, payload)
            name = "gadget-build-" + uuid.uuid4().hex[:12]
            overrides = {"apiVersion": "v1", "kind": "Pod", "metadata": {"labels": {"app": "ig-gadget-builder"}},
                         "spec": {"automountServiceAccountToken": False, "activeDeadlineSeconds": args.timeout,
                                  "nodeSelector": {"kubernetes.io/os": "linux", "kubernetes.io/arch": architecture},
                                  "containers": [{"name": name, "image": f"ghcr.io/inspektor-gadget/gadget-builder:{version}",
                                                  "imagePullPolicy": "IfNotPresent", "stdin": True, "stdinOnce": True,
                                                  "command": ["/bin/sh", "-ec", BUILD, "build", image, REGISTRY],
                                                  "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False,
                                                                      "capabilities": {"drop": ["ALL"]},
                                                                      "seccompProfile": {"type": "RuntimeDefault"}},
                                                  "resources": {"requests": {"cpu": "250m", "memory": "256Mi"},
                                                                "limits": {"memory": "2Gi"}}}]}}
            print(f"Building {image} on {architecture} in pod {name}", flush=True)
            try:
                with payload.open("rb") as stream:
                    subprocess.run(kubectl + ["run", name, "--namespace", NAMESPACE,
                                             "--image", f"ghcr.io/inspektor-gadget/gadget-builder:{version}",
                                             "--restart=Never", "--stdin", "--attach=true",
                                             f"--pod-running-timeout={args.timeout}s", "--overrides", json.dumps(overrides)],
                                   stdin=stream, check=True, timeout=args.timeout)
            finally:
                # Leave time for Kubernetes' default 30-second termination grace period.
                failed = sys.exc_info()[0] is not None
                try:
                    subprocess.run(kubectl + ["delete", "pod", name, "--namespace", NAMESPACE,
                                              "--ignore-not-found=true", "--timeout=60s"], check=True, timeout=90)
                except (OSError, subprocess.SubprocessError) as error:
                    print(f"Builder pod {name} cleanup failed: {error}", file=sys.stderr)
                    if not failed:
                        raise
        print(f"Built and pushed {image}", flush=True)
    except subprocess.CalledProcessError as error:
        parser.exit(error.returncode if error.returncode > 0 else 1, f"Build command failed (exit {error.returncode})\n")
    except KeyboardInterrupt:
        parser.exit(130, "Build interrupted\n")
    except subprocess.TimeoutExpired as error:
        parser.exit(1, f"Command timed out after {error.timeout} seconds\n")
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
