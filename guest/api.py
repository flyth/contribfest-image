#!/usr/bin/env python3
"""Private launcher bridge, reachable only through QEMU's loopback forwards."""
import hmac
import json
import os.path
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_PATH = Path("/sys/firmware/dmi/entries/11-0/raw")
REGISTRY_PATH = Path("/var/lib/demo-registry")
LAYERS_PATH = Path("/opt/demo/layers")
BASE_WORKLOADS = [
    ("demo", "deployment", "registry"),
    ("gadget", "daemonset", "gadget"),
    ("kube-system", "deployment", "coredns"),
    ("kube-system", "daemonset", "kube-proxy"),
]
OBSERVABILITY_WORKLOADS = [
    ("demo", "deployment", "headlamp"),
    ("demo", "deployment", "prometheus"),
    ("demo", "deployment", "grafana"),
    ("demo", "daemonset", "node-exporter"),
]


def registry_mounted():
    return os.path.ismount(REGISTRY_PATH)


def smbios_value(key):
    """Return a value from the launcher's SMBIOS type 11 record, or None."""
    record = TOKEN_PATH.read_bytes()
    if len(record) >= 5 and record[0] == 11 and 5 <= record[1] < len(record):
        prefix = f"{key}=".encode()
        for value in record[record[1]:].split(b"\0"):
            if value.startswith(prefix) and len(value) > len(prefix):
                return value[len(prefix):].decode("ascii")
    return None


def launcher_token():
    token = smbios_value("io.inspektor-gadget.demo.token")
    if token is None:
        raise ValueError("Missing launcher token")
    return token


def layers():
    """Names of the optional image layers baked into this disk."""
    try:
        return sorted(path.name for path in LAYERS_PATH.iterdir() if path.is_file())
    except FileNotFoundError:
        return []


def kubectl(*args):
    return subprocess.check_output(
        ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", *args],
        text=True, timeout=15, stderr=subprocess.PIPE,
    ).strip()


def status():
    if not registry_mounted():
        return {"ready": False, "message": "Waiting for persistent registry storage"}
    if not Path("/etc/kubernetes/admin.conf").exists():
        return {"ready": False, "message": "Starting Kubernetes"}
    try:
        if subprocess.run(["systemctl", "is-active", "--quiet", "demo.service"], timeout=5).returncode:
            return {"ready": False, "message": "Starting Kubernetes"}
        kubectl("get", "--raw=/readyz")
        installed = layers()
        observability = "observability" in installed
        for namespace, kind, name in BASE_WORKLOADS + (OBSERVABILITY_WORKLOADS if observability else []):
            obj = json.loads(kubectl("-n", namespace, "get", kind, name, "-o", "json"))
            state = obj.get("status", {})
            desired = state.get("desiredNumberScheduled", 0) if kind == "daemonset" else obj["spec"].get("replicas", 1)
            ready = state.get("numberReady", 0) if kind == "daemonset" else state.get("readyReplicas", 0)
            if not desired or ready < desired:
                return {"ready": False, "message": f"Starting {name}"}
        # Workload status can still describe the previous boot's endpoints.
        probes = [(30500, "/v2/")]
        if observability:
            probes += [(30080, "/"), (30090, "/api/health")]
        for port, path in probes:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                if response.status != 200:
                    return {"ready": False, "message": "Waiting for cluster services"}
        return {"ready": True, "message": "Ready", "registry_persistent": True,
                "registry_migrated": (REGISTRY_PATH / ".ig-legacy-migrated").exists(),
                "layers": installed}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return {"ready": False, "message": "Waiting for cluster services"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        expected = launcher_token()
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {expected}"):
            self.send_error(403)
            return
        try:
            if self.path == "/status":
                result = status()
            elif self.path == "/credentials":
                result = {
                    "kubeconfig": json.loads(kubectl("config", "view", "--raw", "--flatten", "--minify", "-o", "json")),
                    "token": kubectl("-n", "demo", "create", "token", "demo-admin", "--duration=24h"),
                }
            elif self.path == "/shutdown":
                self.respond({"message": "Shutting down"})
                subprocess.Popen(["systemctl", "poweroff"])
                return
            else:
                self.send_error(404)
                return
            self.respond(result)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            self.respond({"error": str(error)}, 503)

    def respond(self, result, code=200):
        body = json.dumps(result).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    launcher_token()
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
