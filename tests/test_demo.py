import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


api = module("demo_api", "guest/api.py")
builder = module("builder", "scripts/build-image.py")


class GuestTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_readiness_waits_for_minikube_start_to_finish(self):
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)), \
             patch.object(api, "kubectl") as kubectl:
            self.assertEqual(api.status(), {"ready": False, "message": "Starting minikube"})
            kubectl.assert_not_called()

    def test_readiness_requires_all_workloads(self):
        def kubectl(*args):
            if args[0] == "get":
                return "ok"
            name = args[4]
            return json.dumps({
                "spec": {"replicas": 1},
                "status": {"desiredNumberScheduled": 1, "numberReady": 1,
                           "readyReplicas": 0 if name == "headlamp" else 1},
            })
        with patch.object(api.Path, "exists", return_value=True), patch.object(api, "kubectl", side_effect=kubectl):
            self.assertEqual(api.status(), {"ready": False, "message": "Starting headlamp"})
        with patch.object(api.Path, "exists", return_value=True), patch.object(api, "kubectl", side_effect=subprocess.TimeoutExpired("kubectl", 15)):
            self.assertFalse(api.status()["ready"])

    def test_bridge_denies_missing_token_and_serves_authorized_status(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_bytes(b"\x0b\x05\x00\x0e\x02other=value\0"
                              b"io.inspektor-gadget.demo.token=test-token\0\0")
            server = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/status"
                with patch.object(api, "TOKEN_PATH", token), patch.object(api, "status", return_value={"ready": True}):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(url)
                    self.assertEqual(error.exception.code, 403)
                    self.assertTrue(builder.request(server.server_port, "test-token", "status")["ready"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_missing_or_malformed_firmware_token_fails_closed(self):
        for record in [b"", b"\x0b\xff\x00\x0e\x01x\0\0",
                       b"\x0b\x05\x00\x0e\x01unrelated=value\0\0",
                       b"\x0b\x05\x00\x0e\x01io.inspektor-gadget.demo.token=\0\0"]:
            with self.subTest(record=record), patch.object(api.Path, "read_bytes", return_value=record):
                with self.assertRaises(ValueError):
                    api.launcher_token()

    def test_stale_ready_workloads_do_not_hide_unreachable_nodeports(self):
        def kubectl(*args):
            if args[0] == "get":
                return "ok"
            return json.dumps({"spec": {"replicas": 1},
                               "status": {"desiredNumberScheduled": 1,
                                          "numberReady": 1, "readyReplicas": 1}})
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api, "kubectl", side_effect=kubectl), \
             patch.object(api.urllib.request, "urlopen", side_effect=ConnectionResetError):
            self.assertFalse(api.status()["ready"])
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api, "kubectl", side_effect=kubectl), \
             patch.object(api.urllib.request, "urlopen") as http:
            http.return_value.__enter__.return_value.status = 200
            self.assertTrue(api.status()["ready"])

    def test_no_ready_daemon_on_zero_nodes(self):
        result = json.dumps({"spec": {}, "status": {"desiredNumberScheduled": 0}})
        with patch.object(api.Path, "exists", return_value=True), patch.object(api, "kubectl", return_value=result):
            self.assertFalse(api.status()["ready"])


class BuilderTests(unittest.TestCase):
    def test_architecture_boot_and_loopback_network(self):
        with patch.object(builder.os, "access", return_value=False):
            arm = builder.qemu_args("arm64", "/disk", "/firmware", 18081, "token")
            amd = builder.qemu_args("amd64", "/disk", "/firmware", 18081, "token")
        self.assertIn("qemu-system-aarch64", arm)
        self.assertIn("/firmware", arm)
        self.assertIn("q35", amd)
        self.assertNotIn("-bios", amd)
        network = arm[arm.index("-netdev") + 1]
        self.assertIn("hostfwd=tcp:127.0.0.1:18081-:8081", network)
        self.assertIn("hostfwd=tcp:127.0.0.1:15000-:30500", network)
        self.assertIn("tcg,thread=multi", arm)
        with patch.object(builder.os, "access", return_value=True):
            offline = builder.qemu_args("arm64", "/disk", "/firmware", 18081, "token", offline=True)
        self.assertIn("restrict=on", offline[offline.index("-netdev") + 1])
        with patch.object(builder.platform, "machine", return_value="aarch64"), \
             patch.object(builder.os, "access", return_value=True):
            native = builder.qemu_args("arm64", "/disk", "/firmware", 18081, "token")
            cross = builder.qemu_args("amd64", "/disk", "/firmware", 18081, "token")
        self.assertIn("kvm", native)
        self.assertIn("tcg,thread=multi", cross)


if __name__ == "__main__":
    unittest.main()
