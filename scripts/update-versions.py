#!/usr/bin/env python3
"""Refresh pinned guest releases. Review the diff, then run both image builds."""
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def get(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode()


def latest(repo):
    return json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))["tag_name"]


def main():
    minikube = latest("kubernetes/minikube")
    constants = get(f"https://raw.githubusercontent.com/kubernetes/minikube/{minikube}/pkg/minikube/constants/constants.go")
    kubernetes = re.search(r'DefaultKubernetesVersion\s*=\s*"([^"]+)"', constants)[1]
    # Keep CRI tools aligned with the Kubernetes minor, not an unrelated latest.
    cri_releases = json.loads(get("https://api.github.com/repos/kubernetes-sigs/cri-tools/releases?per_page=100"))
    minor = ".".join(kubernetes.split(".")[:2]) + "."
    cri = next(r["tag_name"] for r in cri_releases
               if r["tag_name"].startswith(minor) and not r["prerelease"] and not r["draft"])
    chart_index = get("https://kubernetes-sigs.github.io/headlamp/index.yaml")
    headlamp = re.search(r"^    version: ([0-9.]+)$", chart_index, re.MULTILINE)[1]
    values = {
        "MINIKUBE_VERSION": minikube,
        "KUBERNETES_VERSION": kubernetes,
        "CRICTL_VERSION": cri,
        "HELM_VERSION": latest("helm/helm"),
        "IG_VERSION": latest("inspektor-gadget/inspektor-gadget"),
        "HEADLAMP_CHART_VERSION": headlamp,
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
