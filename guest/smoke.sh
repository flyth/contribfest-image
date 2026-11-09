#!/usr/bin/env bash
set -euo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
# shellcheck source=versions.env
source /opt/demo/versions.env
export HOME=/root KUBECONFIG=/etc/kubernetes/admin.conf
test -s /sys/kernel/btf/vmlinux
python3 - <<'PY'
import runpy
import time

status = runpy.run_path('/opt/demo/api.py')['status']
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    state = status()
    if state['ready']:
        break
    print(state['message'], flush=True)
    time.sleep(2)
else:
    raise TimeoutError('Guest services did not become ready before the smoke check')
PY
curl -fsS http://127.0.0.1:30500/v2/
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
  kubectl -n demo exec deployment/registry -- cat /etc/hostname > /dev/null
done
wait "$gadget"
cat "$events"
grep -q 'cat.*hostname' "$events"
