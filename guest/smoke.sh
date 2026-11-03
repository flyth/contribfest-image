#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=versions.env
source /opt/demo/versions.env
export HOME=/root KUBECONFIG=/etc/kubernetes/admin.conf
test -s /sys/kernel/btf/vmlinux
kubectl -n gadget rollout status daemonset/gadget --timeout=180s
kubectl -n demo rollout status deployment/headlamp --timeout=180s
kubectl -n demo rollout status deployment/registry --timeout=180s
curl -fsS http://127.0.0.1:30500/v2/
curl -fsS http://127.0.0.1:30080/ > /dev/null
# Copying through the external-facing NodePort exercises the registry write path.
oras cp "ghcr.io/inspektor-gadget/gadget/trace_open:$IG_VERSION" \
  127.0.0.1:30500/demo/trace_open:demo --to-plain-http
# The daemon must pull the unsigned local copy, not the upstream signed image.
events=$(mktemp)
trap 'rm -f "$events"' EXIT
kubectl-gadget run -n demo registry.demo.svc.cluster.local:5000/demo/trace_open:demo \
  --timeout=30 > "$events" &
gadget=$!
for _ in {1..8}; do
  sleep 2
  kubectl -n demo exec deployment/headlamp -- cat /etc/hostname > /dev/null
done
wait "$gadget"
cat "$events"
grep -q 'cat.*hostname' "$events"
