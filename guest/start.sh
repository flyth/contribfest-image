#!/usr/bin/env bash
set -euo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
export KUBECONFIG=/etc/kubernetes/admin.conf
swapoff -a
modprobe overlay
modprobe br_netfilter
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf
sysctl -w net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1 \
  net.ipv6.conf.default.forwarding=1 net.bridge.bridge-nf-call-iptables=1
mkdir -p /var/lib/demo-data
ln -sfn /var/lib/demo-data /data
systemctl start kubelet.service
if [[ ! -f /etc/kubernetes/admin.conf ]]; then
  kubeadm init --config=/etc/kubernetes/kubeadm.yaml --skip-token-print
  kubectl taint nodes demo node-role.kubernetes.io/control-plane-
fi
until kubectl --request-timeout=5s get --raw=/readyz > /dev/null 2>&1; do
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=300s
