#!/usr/bin/env bash
set -Eeuxo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
failed() {
  journalctl -u cloud-final.service -u demo-registry.service -u demo.service -u kubelet.service -u containerd.service -n 150 --no-pager || true
  kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s get pods -A -o wide || true
  kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s -n gadget describe pods || true
  kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s -n gadget logs \
    -l k8s-app=gadget --all-containers --tail=80 || true
  kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s -n demo describe pods || true
  echo DEMO_IMAGE_FAILED
  systemctl poweroff
}
trap failed ERR
# shellcheck source=versions.env
source /opt/demo/versions.env
export HOME=/root KUBECONFIG=/etc/kubernetes/admin.conf
case $(uname -m) in
  x86_64) arch=amd64 ;;
  aarch64) arch=arm64 ;;
  *) echo 'Unsupported guest architecture' >&2; exit 1 ;;
esac
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates containerd conntrack curl \
  e2fsprogs ethtool iproute2 iptables kmod mount python3 socat util-linux
# Legacy Ubuntu disks can share filesystem UUIDs and labels with the new OS.
python3 - <<'PY'
import os
from pathlib import Path
import re
import subprocess

fstab = Path('/etc/fstab')
lines = fstab.read_text().splitlines(keepends=True)
root = None
for index, line in enumerate(lines):
    fields = line.split()
    if not fields or line.lstrip().startswith('#') or fields[1] not in ('/', '/boot', '/boot/efi'):
        continue
    device = os.path.realpath(subprocess.check_output(['findfs', fields[0]], text=True).strip())
    partition = re.fullmatch(r'/dev/vda([0-9]+)', device)
    if not partition:
        raise RuntimeError(f'Unexpected OS partition: {device}')
    fields[0] = f'/dev/disk/by-id/virtio-ig-os-part{partition[1]}'
    if not Path(fields[0]).exists():
        raise RuntimeError(f'Missing OS disk identity: {fields[0]}')
    if fields[1] != '/':
        # Other VM managers (Lima) attach the disk without the ig-os serial; boot without these.
        fields[3] += ',nofail,x-systemd.device-timeout=10s'
    lines[index] = '\t'.join(fields) + '\n'
    if fields[1] == '/':
        # QEMU attaches the OS first; the kernel cannot resolve udev links yet.
        root = device
if root is None:
    raise RuntimeError('No root filesystem in fstab')
fstab.write_text(''.join(lines))
Path('/etc/default/grub.d/99-demo-root.cfg').write_text(
    f'GRUB_CMDLINE_LINUX="${{GRUB_CMDLINE_LINUX}} root={root}"\n')
PY
update-grub
mkdir -p /opt/bin /opt/cni/bin /etc/cni/net.d /etc/containerd \
  /etc/kubernetes /var/lib/kubelet \
  /opt/libexec/kubernetes/kubelet-plugins/volume/exec
# Provisioning runs under QEMU user networking; demo-network.service decides on each boot.
/opt/demo/demo-network.sh
fetch() {
  curl --fail --location --retry 5 --output "$2" "$1"
}
for tool in kubeadm kubelet kubectl; do
  fetch "https://dl.k8s.io/release/$KUBERNETES_VERSION/bin/linux/$arch/$tool" "/opt/bin/$tool"
  fetch "https://dl.k8s.io/release/$KUBERNETES_VERSION/bin/linux/$arch/$tool.sha256" "/tmp/$tool.sha256"
  echo "$(cat "/tmp/$tool.sha256")  /opt/bin/$tool" | sha256sum -c -
  chmod +x "/opt/bin/$tool"
done
/opt/demo/build-tools.sh install
cni_asset="cni-plugins-linux-$arch-$CNI_PLUGINS_VERSION.tgz"
fetch "https://github.com/containernetworking/plugins/releases/download/$CNI_PLUGINS_VERSION/$cni_asset" /tmp/cni.tgz
fetch "https://github.com/containernetworking/plugins/releases/download/$CNI_PLUGINS_VERSION/$cni_asset.sha256" /tmp/cni.sha256
echo "$(awk '{print $1}' /tmp/cni.sha256)  /tmp/cni.tgz" | sha256sum -c -
# Only the plugins 10-demo.conflist and the runtime use; the rest is about 80 MB.
tar -xzf /tmp/cni.tgz -C /opt/cni/bin ./bridge ./host-local ./loopback

sed "s/\${KUBERNETES_VERSION}/$KUBERNETES_VERSION/" /opt/demo/kubeadm.yaml > /etc/kubernetes/kubeadm.yaml
sandbox=$(kubeadm config images list --config=/etc/kubernetes/kubeadm.yaml | grep '/pause:')
test -n "$sandbox"
cat > /etc/containerd/config.toml <<EOF
version = 2
root = "/var/lib/containerd"
state = "/run/containerd"
[plugins."io.containerd.grpc.v1.cri"]
  sandbox_image = "$sandbox"
# Keep only the unpacked snapshots of pulled images, not their compressed layers.
[plugins."io.containerd.grpc.v1.cri".containerd]
  discard_unpacked_layers = true
[plugins."io.containerd.grpc.v1.cri".cni]
  bin_dir = "/opt/cni/bin"
  conf_dir = "/etc/cni/net.d"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
  SystemdCgroup = true
EOF
cat > /etc/crictl.yaml <<'EOF'
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
EOF
# Single-node bridge: no overlay daemon or external CNI manifest is needed.
cat > /etc/cni/net.d/10-demo.conflist <<'EOF'
{
  "cniVersion": "1.0.0",
  "name": "demo",
  "plugins": [{
    "type": "bridge",
    "bridge": "cni0",
    "isGateway": true,
    "ipMasq": true,
    "hairpinMode": true,
    "ipam": {
      "type": "host-local",
      "ranges": [[{"subnet": "10.244.0.0/24"}]],
      "routes": [{"dst": "0.0.0.0/0"}]
    }
  }]
}
EOF
mkdir -p /etc/modules-load.d /etc/sysctl.d
printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/demo.conf
cat > /etc/sysctl.d/90-demo.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
net.ipv6.conf.default.forwarding=1
net.bridge.bridge-nf-call-iptables=1
EOF
systemctl daemon-reload
systemctl restart containerd.service
systemctl enable demo-network.service demo-registry.service kubelet.service demo.service \
  demo-api.service demo-layer.service
systemctl start demo.service
helm repo add gadget https://inspektor-gadget.github.io/charts
helm repo update
sed "s/\${REGISTRY_VERSION}/$REGISTRY_VERSION/" /opt/demo/registry.yaml | kubectl apply -f -
helm upgrade --install gadget gadget/gadget --namespace gadget --create-namespace \
  --version "${IG_VERSION#v}" -f /opt/demo/gadget-values.yaml
# Allow 30 seconds for exec probes under CPU emulation, with fewer process launches.
kubectl -n gadget patch daemonset gadget --type=strategic -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"gadget",
    "startupProbe":{"timeoutSeconds":30,"periodSeconds":30,"failureThreshold":10},
    "livenessProbe":{"timeoutSeconds":30,"periodSeconds":30},
    "readinessProbe":{"timeoutSeconds":30,"periodSeconds":30}}]}}}}'
kubectl -n gadget rollout status daemonset/gadget --timeout=600s
kubectl -n demo rollout status deployment/registry --timeout=180s
/opt/demo/smoke.sh

# openssh-server stays: Lima reaches the guest over SSH. The launcher never forwards port 22.
apt-get purge -y snapd lxd-installer lxd-agent-loader apport apport-core-dump-handler \
  pollinate unattended-upgrades
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer || true
apt-get clean
/opt/demo/build-tools.sh remove
rm -rf /root/.cache/helm /var/lib/apt/lists/*
rm -f /tmp/{kubeadm,kubelet,kubectl}.sha256 /tmp/cni.tgz /tmp/cni.sha256
# A VM loads no hardware firmware; no package owns this copy.
rm -rf "/usr/lib/firmware/$(uname -r)"
journalctl --rotate
journalctl --vacuum-time=1s
fstrim -av
# Keep the baked network configuration. cloud-init runs again only when a NoCloud seed
# (Lima's cidata disk) is attached; ds-identify disables it on the launcher's boots.
printf 'datasource_list: [NoCloud]\n' > /etc/cloud/cloud.cfg.d/99-ig-demo.cfg
sync
echo DEMO_IMAGE_READY
systemctl poweroff
