import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


api = module("demo_api", "guest/api.py")
builder = module("builder", "scripts/build-image.py")
updater = module("updater", "scripts/update-versions.py")


class GuestTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))
        patcher.start()
        self.addCleanup(patcher.stop)
        storage = patch.object(api, "registry_mounted", return_value=True)
        storage.start()
        self.addCleanup(storage.stop)
        # Most readiness tests describe the full image; base-only tests override this.
        layers = patch.object(api, "layers", return_value=["observability"])
        layers.start()
        self.addCleanup(layers.stop)

    def test_missing_registry_mount_blocks_readiness_before_cluster_access(self):
        with patch.object(api, "registry_mounted", return_value=False), \
             patch.object(api, "kubectl") as kubectl:
            self.assertFalse(api.status()["ready"])
            kubectl.assert_not_called()

    def test_readiness_waits_for_cluster_start_to_finish(self):
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)), \
             patch.object(api, "kubectl") as kubectl:
            self.assertFalse(api.status()["ready"])
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
            self.assertFalse(api.status()["ready"])
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
            self.assertTrue(api.status()["registry_persistent"])

    def test_readiness_requires_monitoring_workloads(self):
        for missing in ["prometheus", "grafana", "node-exporter"]:
            def kubectl(*args, missing=missing):
                if args[0] == "get":
                    return "ok"
                ready = 0 if args[4] == missing else 1
                return json.dumps({"spec": {"replicas": 1},
                                   "status": {"desiredNumberScheduled": 1,
                                              "numberReady": ready, "readyReplicas": ready}})
            with self.subTest(missing=missing), patch.object(api.Path, "exists", return_value=True), \
                    patch.object(api, "kubectl", side_effect=kubectl), \
                    patch.object(api.urllib.request, "urlopen") as http:
                http.return_value.__enter__.return_value.status = 200
                state = api.status()
                self.assertFalse(state["ready"])
                self.assertEqual(state["message"], f"Starting {missing}")

    def test_readiness_probes_grafana_through_its_nodeport(self):
        def kubectl(*args):
            if args[0] == "get":
                return "ok"
            return json.dumps({"spec": {"replicas": 1},
                               "status": {"desiredNumberScheduled": 1,
                                          "numberReady": 1, "readyReplicas": 1}})
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api, "kubectl", side_effect=kubectl), \
             patch.object(api.urllib.request, "urlopen") as http:
            http.return_value.__enter__.return_value.status = 200
            self.assertTrue(api.status()["ready"])
            urls = [call.args[0] for call in http.call_args_list]
        self.assertIn("http://127.0.0.1:30090/api/health", urls)

    def test_base_readiness_ignores_observability_workloads(self):
        base = {"registry", "gadget", "coredns", "kube-proxy"}

        def kubectl(*args):
            if args[0] == "get":
                return "ok"
            if args[4] not in base:
                raise subprocess.CalledProcessError(1, "kubectl")
            return json.dumps({"spec": {"replicas": 1},
                               "status": {"desiredNumberScheduled": 1,
                                          "numberReady": 1, "readyReplicas": 1}})
        with patch.object(api, "layers", return_value=[]), \
             patch.object(api.Path, "exists", return_value=True), \
             patch.object(api, "kubectl", side_effect=kubectl), \
             patch.object(api.urllib.request, "urlopen") as http:
            http.return_value.__enter__.return_value.status = 200
            state = api.status()
            urls = [call.args[0] for call in http.call_args_list]
        self.assertTrue(state["ready"])
        self.assertEqual(state["layers"], [])
        self.assertEqual(urls, ["http://127.0.0.1:30500/v2/"])

    def test_observability_layer_requires_its_workloads_and_ports(self):
        def kubectl(*args, missing=None):
            if args[0] == "get":
                return "ok"
            ready = 0 if args[4] == missing else 1
            return json.dumps({"spec": {"replicas": 1},
                               "status": {"desiredNumberScheduled": 1,
                                          "numberReady": ready, "readyReplicas": ready}})
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api, "kubectl", side_effect=lambda *a: kubectl(*a, missing="headlamp")), \
             patch.object(api.urllib.request, "urlopen") as http:
            http.return_value.__enter__.return_value.status = 200
            self.assertFalse(api.status()["ready"])
        with patch.object(api.Path, "exists", return_value=True), \
             patch.object(api, "kubectl", side_effect=kubectl), \
             patch.object(api.urllib.request, "urlopen") as http:
            http.return_value.__enter__.return_value.status = 200
            state = api.status()
            urls = [call.args[0] for call in http.call_args_list]
        self.assertTrue(state["ready"])
        self.assertEqual(state["layers"], ["observability"])
        self.assertIn("http://127.0.0.1:30080/", urls)
        self.assertIn("http://127.0.0.1:30090/api/health", urls)

    def test_smbios_value_reads_any_key(self):
        record = (b"\x0b\x05\x00\x0e\x02io.inspektor-gadget.demo.token=abc\0"
                  b"io.inspektor-gadget.demo.provision=observability\0\0")
        with patch.object(api.Path, "read_bytes", return_value=record):
            self.assertEqual(api.smbios_value("io.inspektor-gadget.demo.token"), "abc")
            self.assertEqual(api.smbios_value("io.inspektor-gadget.demo.provision"), "observability")
            self.assertIsNone(api.smbios_value("io.inspektor-gadget.demo.other"))

    def test_no_ready_daemon_on_zero_nodes(self):
        result = json.dumps({"spec": {}, "status": {"desiredNumberScheduled": 0}})
        with patch.object(api.Path, "exists", return_value=True), patch.object(api, "kubectl", return_value=result):
            self.assertFalse(api.status()["ready"])


class LayerMarkerTests(unittest.TestCase):
    def test_layers_lists_marker_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(api, "LAYERS_PATH", Path(directory) / "missing"):
                self.assertEqual(api.layers(), [])
            with patch.object(api, "LAYERS_PATH", Path(directory)):
                (Path(directory) / "observability").touch()
                self.assertEqual(api.layers(), ["observability"])


class BuilderTests(unittest.TestCase):
    def test_selects_sha256_for_exact_image(self):
        digest = "ab" * 32
        contents = f"{'c' * 64} *other-image.img\n{digest.upper()} *image.img\n"
        self.assertEqual(builder.sha256_digest(contents, "image.img"), digest)

    def test_rejects_missing_or_invalid_sha256(self):
        for contents in ["", f"{'a' * 64} *other.img", f"{'a' * 40} *image.img",
                         f"{'g' * 64} *image.img"]:
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                builder.sha256_digest(contents, "image.img")


class LayeredBuildTests(unittest.TestCase):
    def test_provision_flag_shares_the_token_record(self):
        args = builder.qemu_args("amd64", "os", "registry", None, 18081, "tok", provision="observability")
        records = [args[i + 1] for i, a in enumerate(args) if a == "-smbios"]
        self.assertEqual(records, ["type=11,value=io.inspektor-gadget.demo.token=tok,"
                                   "value=io.inspektor-gadget.demo.provision=observability"])
        args = builder.qemu_args("amd64", "os", "registry", None, 18081, "tok")
        self.assertNotIn("provision", " ".join(args))

    def test_manifest_lists_variants_in_chain_order(self):
        for arch, support in [("arm64", ["ig-registry-seed-arm64.qcow2", "QEMU_EFI-arm64.fd"]),
                              ("amd64", ["ig-registry-seed-amd64.qcow2"])]:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                files = []
                for name in [f"ig-minimal-{arch}.qcow2", f"ig-observability-{arch}.qcow2", *support]:
                    (output / name).write_bytes(name.encode())
                    files.append(output / name)
                path = builder.write_manifest(output, arch, "images-v1.0.0", files)
                self.assertEqual(path.name, f"manifest-{arch}.json")
                manifest = json.loads(path.read_text())
                self.assertEqual(manifest["guest_api"], builder.GUEST_API)
                self.assertEqual((manifest["arch"], manifest["release"]), (arch, "images-v1.0.0"))
                self.assertEqual(manifest["versions"]["IG_VERSION"], builder.versions()["IG_VERSION"])
                first = f"ig-minimal-{arch}.qcow2"
                self.assertEqual(manifest["files"][first]["size"], len(first))
                self.assertEqual(manifest["files"][first]["sha256"],
                                 __import__("hashlib").sha256(first.encode()).hexdigest())
                self.assertEqual(manifest["variants"]["minimal"], {"chain": [first], "support": support})
                self.assertEqual(manifest["variants"]["observability"],
                                 {"chain": [first, f"ig-observability-{arch}.qcow2"], "support": support})

    @unittest.skipUnless(__import__("shutil").which("qemu-img") and __import__("shutil").which("qemu-io"),
                         "needs qemu-img")
    def test_layer_backs_onto_base_by_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            work, output = Path(directory) / "work", Path(directory) / "out"
            work.mkdir()
            output.mkdir()
            base, overlay = work / "base.qcow2", work / "layer.qcow2"
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", base, "16M"], check=True)
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, overlay],
                           check=True)
            subprocess.run(["qemu-io", "-c", "write -P 7 0 1M", overlay], check=True, capture_output=True)
            subprocess.run(["qemu-img", "convert", "-O", "qcow2", base, output / "ig-minimal-arm64.qcow2"],
                           check=True)
            layer = builder.package_layer(overlay, output, "ig-minimal-arm64.qcow2")
            self.assertEqual(layer, output / "ig-observability-arm64.qcow2")
            info = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", layer]))
            self.assertEqual(info["backing-filename"], "ig-minimal-arm64.qcow2")
            moved = Path(directory) / "moved"
            output.rename(moved)
            subprocess.run(["qemu-img", "compare", "-q", overlay, moved / layer.name], check=True)


class ImageWorkflowTests(unittest.TestCase):
    def test_image_releases_are_public_and_separate(self):
        workflow = (ROOT / ".github/workflows/images.yml").read_text()
        self.assertIn('tags: ["images-v*"]', workflow)
        self.assertNotIn('tags: ["v*"]', workflow)
        self.assertIn('--release "$GITHUB_REF_NAME"', workflow)
        self.assertIn("--latest=false", workflow)
        self.assertNotIn("--draft", workflow)
        publish = workflow[workflow.index("Publish image assets"):]
        self.assertLess(publish.index("! -name 'manifest-*'"), publish.index("manifest-${{ matrix.arch }}.json"))


class FakeGrafana(BaseHTTPRequestHandler):
    """Answers datasource queries with whichever series a test configured."""
    series = {}
    org_status = 200

    def log_message(self, *_):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/api/org":
            self.send_response(self.org_status)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(url.query)["query"][0]
        body = json.dumps({"status": "success", "data": {"result": self.series.get(query, [])}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MonitoringCheckTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGrafana)
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        FakeGrafana.org_status = 200

    def test_accepts_scraped_ig_and_node_metrics_without_credentials(self):
        FakeGrafana.series = {query: [{"value": [0, "1"]}] for query in builder.MONITORING_QUERIES}
        builder.check_monitoring(self.base, timeout=1)

    def test_times_out_when_any_expected_series_is_missing(self):
        for missing in builder.MONITORING_QUERIES:
            FakeGrafana.series = {query: [{"value": [0, "1"]}] for query in builder.MONITORING_QUERIES
                                  if query != missing}
            with self.subTest(missing=missing), self.assertRaises(TimeoutError):
                builder.check_monitoring(self.base, timeout=0)

    def test_times_out_when_grafana_demands_login(self):
        FakeGrafana.org_status = 401
        FakeGrafana.series = {query: [{"value": [0, "1"]}] for query in builder.MONITORING_QUERIES}
        with self.assertRaises(TimeoutError):
            builder.check_monitoring(self.base, timeout=0)


class ChartVersionTests(unittest.TestCase):
    INDEX = """entries:
  other:
  - version: 9.9.9
  prometheus:
  - appVersion: v3
    version: 30.0.0-rc.1
  - appVersion: v3
    version: 29.40.3
  - version: 29.40.2
  prometheus-node-exporter:
  - version: 4.60.1
"""

    def test_selects_newest_stable_release_of_the_named_chart(self):
        self.assertEqual(updater.chart_version(self.INDEX, "prometheus"), "29.40.3")

    def test_missing_chart_is_an_error(self):
        with self.assertRaises(ValueError):
            updater.chart_version(self.INDEX, "grafana")


class MonitoringConfigTests(unittest.TestCase):
    def test_builder_forwards_grafana_like_the_launcher(self):
        args = builder.qemu_args("amd64", "os", "registry", None, 18081, "token")
        self.assertIn("hostfwd=tcp:127.0.0.1:18090-:30090", args[args.index("-netdev") + 1])

    def test_ig_exports_daemon_and_internal_metrics(self):
        values = (ROOT / "guest/gadget-values.yaml").read_text()
        self.assertRegex(values, r"otel-metrics-listen: true")
        self.assertRegex(values, r"otel-metrics-export-internals: true")

    def test_grafana_never_shows_a_login(self):
        values = (ROOT / "guest/grafana-values.yaml").read_text()
        self.assertRegex(values, r"auth\.anonymous:\s+enabled: true\s+org_role: Admin")
        self.assertRegex(values, r"disable_login_form: true")
        self.assertRegex(values, r"nodePort: 30090")

    def test_grafana_rollout_outlasts_a_slow_image_pull(self):
        # Pulling and unpacking its 1.4 GB image took 13 minutes under emulation.
        values = (ROOT / "guest/grafana-values.yaml").read_text()
        deadline = int(re.search(r"(?m)^progressDeadlineSeconds: (\d+)$", values)[1])
        install = re.search(r"helm upgrade --install grafana .*?--timeout (\d+)m",
                            (ROOT / "guest/provision-layer.sh").read_text(), re.DOTALL)
        self.assertGreaterEqual(deadline, 3600)
        self.assertGreaterEqual(int(install[1]) * 60, deadline)

    def test_prometheus_stack_excludes_unrequested_components(self):
        values = (ROOT / "guest/prometheus-values.yaml").read_text()
        for component in ["alertmanager", "kube-state-metrics", "prometheus-pushgateway"]:
            self.assertRegex(values, rf"(?m)^{component}:\s+enabled: false")
        self.assertRegex(values, r"(?m)^prometheus-node-exporter:\s+enabled: true")

    def test_starter_dashboard_is_valid_and_charts_ig_and_the_node(self):
        dashboard = json.loads((ROOT / "guest/grafana-dashboard.json").read_text())
        expressions = " ".join(target["expr"] for panel in dashboard["panels"]
                               for target in panel["targets"])
        self.assertIn('namespace="gadget"', expressions)
        self.assertIn("node_", expressions)

    def test_base_provision_installs_no_observability_charts(self):
        base = (ROOT / "guest/provision.sh").read_text()
        layer = (ROOT / "guest/provision-layer.sh").read_text()
        for chart in ["headlamp", "prometheus", "grafana"]:
            self.assertNotRegex(base, rf"helm upgrade --install {chart}\b")
            self.assertRegex(layer, rf"helm upgrade --install {chart}\b")
        self.assertIn("touch /opt/demo/layers/observability", layer)
        self.assertIn("demo-layer.service", base)
        self.assertIn("io.inspektor-gadget.demo.provision", layer)

    def test_build_only_tools_are_removed_before_packaging(self):
        for name, ready in [("provision.sh", "DEMO_IMAGE_READY"), ("provision-layer.sh", "DEMO_LAYER_READY")]:
            script = (ROOT / "guest" / name).read_text()
            install = script.index("/opt/demo/build-tools.sh install")
            remove = script.index("/opt/demo/build-tools.sh remove")
            self.assertLess(install, script.index("helm "), name)
            self.assertLess(remove, script.index(f"echo {ready}"), name)
            self.assertLess(script.rindex("smoke"), remove, name)
        self.assertIn("tools=(helm crictl kubectl-gadget oras)",
                      (ROOT / "guest/build-tools.sh").read_text())

    def test_chart_versions_are_pinned(self):
        versions = builder.versions()
        self.assertTrue(versions["PROMETHEUS_CHART_VERSION"])
        self.assertTrue(versions["GRAFANA_CHART_VERSION"])


class LimaTests(unittest.TestCase):
    def test_template_runs_the_minimal_image_with_only_the_api_forwarded(self):
        template = (ROOT / "lima/ig-demo.yaml").read_text()
        for placeholder in ["@IMAGE@", "@ARCH@", "@SHA256@"]:
            self.assertIn(placeholder, template)
        self.assertIn("mounts: []", template)
        self.assertRegex(template, r"containerd:\n  system: false\n  user: false")
        self.assertRegex(template, r"guestPort: 8443\n    hostIP: 127.0.0.1\n    hostPort: 18443")
        # Any guest address and both protocols: the default rule lets [::] binds through.
        self.assertIn(
            "  - guestIP: 0.0.0.0\n    guestIPMustBeZero: false\n"
            "    guestPortRange: [1, 65535]\n    proto: any\n    ignore: true\n", template)

    def test_script_verifies_every_download(self):
        script = (ROOT / "lima/ig-demo").read_text()
        self.assertIn("ig-minimal-$arch.qcow2", script)
        self.assertEqual(script.count("fetch \""), 3)  # image, kubectl, kubectl-gadget
        self.assertIn("checksum mismatch", script)
        self.assertIn("https://127.0.0.1:$API_PORT", script)

    def test_guest_boots_outside_the_launcher(self):
        provision = (ROOT / "guest/provision.sh").read_text()
        self.assertNotIn("cloud-init.disabled", provision)
        self.assertIn("datasource_list: [NoCloud]", provision)
        self.assertNotIn("openssh-server", provision.split("apt-get purge", 1)[1].split("\n\n", 1)[0])
        self.assertIn("nofail", provision)
        self.assertIn("demo-network.service", provision)
        network = (ROOT / "guest/demo-network.sh").read_text()
        self.assertIn('ip addr replace "$node/32" dev lo', network)
        storage = (ROOT / "guest/registry-storage.sh").read_text()
        self.assertIn("keeping the registry on the OS disk", storage)
        self.assertIn("ConditionPathExists=/sys/firmware/dmi/entries/11-0/raw",
                      (ROOT / "guest/demo-api.service").read_text())


class MacRuntimeTests(unittest.TestCase):
    def test_macos_bundle_ships_a_pinned_hvf_qemu(self):
        config = json.loads((ROOT / "src-tauri/tauri.macos.conf.json").read_text())
        self.assertEqual(config["bundle"]["resources"], {"qemu/": "qemu/"})
        script = (ROOT / "scripts/build-qemu-macos.sh").read_text()
        self.assertIn("scripts/build-qemu-macos.sh --if-missing", config["build"]["beforeBuildCommand"])
        self.assertTrue(config["build"]["beforeDevCommand"]["wait"])
        for flag in ["--enable-hvf", "--enable-slirp", "--enable-tools", "--without-default-features"]:
            self.assertIn(flag, script)
        self.assertIn("com.apple.security.hypervisor", script)
        self.assertIn("efi-virtio.rom", script)
        self.assertIn("/src-tauri/qemu/", (ROOT / ".gitignore").read_text())

    def test_windows_bundle_ships_a_whpx_qemu_from_the_same_pin(self):
        config = json.loads((ROOT / "src-tauri/tauri.windows.conf.json").read_text())
        self.assertEqual(config["bundle"]["resources"], {"qemu/": "qemu/"})
        pin = (ROOT / "scripts/qemu.env").read_text()
        self.assertRegex(pin, r"QEMU_SHA256=[0-9a-f]{64}\n")
        for name in ["build-qemu-macos.sh", "build-qemu-windows.sh"]:
            self.assertIn('source "$root/scripts/qemu.env"', (ROOT / "scripts" / name).read_text())
        script = (ROOT / "scripts/build-qemu-windows.sh").read_text()
        for flag in ["--enable-whpx", "--enable-slirp", "--enable-tools", "--without-default-features"]:
            self.assertIn(flag, script)
        workflow = (ROOT / ".github/workflows/desktop.yml").read_text()
        self.assertIn("scripts/build-qemu-windows.sh --if-missing", workflow)


if __name__ == "__main__":
    unittest.main()
