#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=versions.env
source /opt/demo/versions.env
export KUBECONFIG=/etc/kubernetes/admin.conf
swapoff -a
modprobe overlay
modprobe br_netfilter
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf
sysctl -w net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1 \
  net.ipv6.conf.default.forwarding=1 net.bridge.bridge-nf-call-iptables=1
mkdir -p /var/lib/demo-data
ln -sfn /var/lib/demo-data /data
if [[ -f /etc/kubernetes/admin.conf ]]; then
  systemctl start kubelet.service
else
  KUBECONFIG=/root/.kube/config minikube start --driver=none --container-runtime=containerd --force \
    --kubernetes-version="$KUBERNETES_VERSION" --cni=bridge \
    --apiserver-ips=127.0.0.1,10.0.2.15 --apiserver-name=localhost \
    --extra-config=kubelet.cgroup-driver=systemd
fi
until kubectl --request-timeout=5s get --raw=/readyz > /dev/null 2>&1; do
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=300s
