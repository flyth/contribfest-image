#!/usr/bin/env python3
"""Private launcher bridge, reachable only through QEMU's loopback forwards."""
import hmac
import json
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_PATH = Path("/sys/firmware/dmi/entries/11-0/raw")


def launcher_token():
    record = TOKEN_PATH.read_bytes()
    if len(record) >= 5 and record[0] == 11 and 5 <= record[1] < len(record):
        for value in record[record[1]:].split(b"\0"):
            prefix = b"io.inspektor-gadget.demo.token="
            if value.startswith(prefix) and len(value) > len(prefix):
                return value[len(prefix):].decode("ascii")
    raise ValueError("Missing launcher token")


def kubectl(*args):
    return subprocess.check_output(
        ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", *args],
        text=True, timeout=15, stderr=subprocess.PIPE,
    ).strip()


def status():
    if not Path("/etc/kubernetes/admin.conf").exists():
        return {"ready": False, "message": "Starting Kubernetes"}
    try:
        if subprocess.run(["systemctl", "is-active", "--quiet", "demo.service"], timeout=5).returncode:
            return {"ready": False, "message": "Starting minikube"}
        kubectl("get", "--raw=/readyz")
        for namespace, kind, name in [
            ("demo", "deployment", "registry"),
            ("demo", "deployment", "headlamp"),
            ("gadget", "daemonset", "gadget"),
            ("kube-system", "deployment", "coredns"),
            ("kube-system", "daemonset", "kube-proxy"),
        ]:
            obj = json.loads(kubectl("-n", namespace, "get", kind, name, "-o", "json"))
            state = obj.get("status", {})
            desired = state.get("desiredNumberScheduled", 0) if kind == "daemonset" else obj["spec"].get("replicas", 1)
            ready = state.get("numberReady", 0) if kind == "daemonset" else state.get("readyReplicas", 0)
            if not desired or ready < desired:
                return {"ready": False, "message": f"Starting {name}"}
        # Workload status can still describe the previous boot's endpoints.
        for port, path in [(30080, "/"), (30500, "/v2/")]:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                if response.status != 200:
                    return {"ready": False, "message": "Waiting for cluster services"}
        return {"ready": True, "message": "Ready"}
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
