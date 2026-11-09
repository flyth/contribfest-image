#!/usr/bin/env python3
"""Refresh guest tool pins; update the Ubuntu snapshot explicitly, then build both images."""
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ig-demo-version-updater"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode()


def latest(repo):
    return json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))["tag_name"]


def chart_version(index, name):
    """Return the newest stable release of a chart from a Helm repository index."""
    entry = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", index, re.MULTILINE | re.DOTALL)
    stable = entry and re.search(r"^    version: ([0-9.]+)$", entry[1], re.MULTILINE)
    if not stable:
        raise ValueError(f"No stable release of chart {name}")
    return stable[1]


def main():
    kubernetes = get("https://dl.k8s.io/release/stable.txt").strip()
    # Keep CRI tools aligned with the Kubernetes minor, not an unrelated latest.
    cri_releases = json.loads(get("https://api.github.com/repos/kubernetes-sigs/cri-tools/releases?per_page=100"))
    minor = ".".join(kubernetes.split(".")[:2]) + "."
    cri = next(r["tag_name"] for r in cri_releases
               if r["tag_name"].startswith(minor) and not r["prerelease"] and not r["draft"])
    chart_index = get("https://kubernetes-sigs.github.io/headlamp/index.yaml")
    headlamp = chart_version(chart_index, "headlamp")
    prometheus = chart_version(get("https://prometheus-community.github.io/helm-charts/index.yaml"), "prometheus")
    grafana = chart_version(get("https://grafana-community.github.io/helm-charts/index.yaml"), "grafana")
    values = {
        "CNI_PLUGINS_VERSION": latest("containernetworking/plugins"),
        "KUBERNETES_VERSION": kubernetes,
        "CRICTL_VERSION": cri,
        "HELM_VERSION": latest("helm/helm"),
        "IG_VERSION": latest("inspektor-gadget/inspektor-gadget"),
        "HEADLAMP_CHART_VERSION": headlamp,
        "PROMETHEUS_CHART_VERSION": prometheus,
        "GRAFANA_CHART_VERSION": grafana,
        "REGISTRY_VERSION": latest("distribution/distribution").removeprefix("v"),
        "ORAS_VERSION": latest("oras-project/oras"),
    }
    path = ROOT / "versions.env"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(
        f"{line.split('=', 1)[0]}={values[line.split('=', 1)[0]]}"
        if line.split("=", 1)[0] in values else line for line in lines
    ) + "\n")


if __name__ == "__main__":
    main()
