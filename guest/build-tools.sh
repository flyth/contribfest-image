#!/usr/bin/env bash
# Tools only the image build uses (Helm charts, smoke tests, debugging while
# provisioning). provision.sh and provision-layer.sh install them into /opt/bin and
# remove them before the disk is packaged, which keeps about 180 MB out of the images.
# Users run kubectl and kubectl-gadget on their own computer.
#
#   build-tools.sh install | remove
set -Eeuo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
tools=(helm crictl kubectl-gadget oras)
case ${1:-} in
  remove) for tool in "${tools[@]}"; do rm -f "/opt/bin/$tool"; done; exit 0 ;;
  install) ;;
  *) echo "usage: $0 install|remove" >&2; exit 64 ;;
esac
# shellcheck source=versions.env
source /opt/demo/versions.env
case $(uname -m) in
  x86_64) arch=amd64 ;;
  aarch64) arch=arm64 ;;
  *) echo 'Unsupported guest architecture' >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fetch() {
  # --retry alone skips TLS and connection errors, which do happen mid-build.
  curl --fail --location --retry 5 --retry-all-errors --output "$2" "$1"
}
mkdir -p /opt/bin
fetch "https://get.helm.sh/helm-$HELM_VERSION-linux-$arch.tar.gz" "$tmp/helm.tar.gz"
fetch "https://get.helm.sh/helm-$HELM_VERSION-linux-$arch.tar.gz.sha256sum" "$tmp/helm.sha256"
echo "$(awk '{print $1}' "$tmp/helm.sha256")  $tmp/helm.tar.gz" | sha256sum -c -
tar -xzf "$tmp/helm.tar.gz" -C /opt/bin --strip-components=1 "linux-$arch/helm"
fetch "https://github.com/kubernetes-sigs/cri-tools/releases/download/$CRICTL_VERSION/crictl-$CRICTL_VERSION-linux-$arch.tar.gz" "$tmp/crictl.tar.gz"
fetch "https://github.com/kubernetes-sigs/cri-tools/releases/download/$CRICTL_VERSION/crictl-$CRICTL_VERSION-linux-$arch.tar.gz.sha256" "$tmp/crictl.sha256"
echo "$(cat "$tmp/crictl.sha256")  $tmp/crictl.tar.gz" | sha256sum -c -
tar -xzf "$tmp/crictl.tar.gz" -C /opt/bin crictl
fetch "https://github.com/inspektor-gadget/inspektor-gadget/releases/download/$IG_VERSION/kubectl-gadget-linux-$arch-$IG_VERSION.tar.gz" "$tmp/gadget.tar.gz"
fetch "https://github.com/inspektor-gadget/inspektor-gadget/releases/download/$IG_VERSION/SHA256SUMS" "$tmp/ig.sha256"
expected=$(grep " kubectl-gadget-linux-$arch-$IG_VERSION.tar.gz$" "$tmp/ig.sha256" | awk '{print $1}')
test -n "$expected"
echo "$expected  $tmp/gadget.tar.gz" | sha256sum -c -
tar -xzf "$tmp/gadget.tar.gz" -C /opt/bin kubectl-gadget
oras_asset="oras_${ORAS_VERSION#v}_linux_$arch.tar.gz"
fetch "https://github.com/oras-project/oras/releases/download/$ORAS_VERSION/$oras_asset" "$tmp/oras.tar.gz"
fetch "https://github.com/oras-project/oras/releases/download/$ORAS_VERSION/oras_${ORAS_VERSION#v}_checksums.txt" "$tmp/oras.sha256"
expected=$(grep " $oras_asset$" "$tmp/oras.sha256" | awk '{print $1}')
test -n "$expected"
echo "$expected  $tmp/oras.tar.gz" | sha256sum -c -
tar -xzf "$tmp/oras.tar.gz" -C /opt/bin oras
for tool in "${tools[@]}"; do chmod +x "/opt/bin/$tool"; done
