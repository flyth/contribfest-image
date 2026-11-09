#!/usr/bin/env bash
set -euo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
export HOME=/root KUBECONFIG=/etc/kubernetes/admin.conf
python3 - <<'PY'
import runpy
import time

status = runpy.run_path('/opt/demo/api.py')['status']
deadline = time.monotonic() + 600
while time.monotonic() < deadline:
    state = status()
    if state['ready'] and 'observability' in state['layers']:
        break
    print(state['message'], flush=True)
    time.sleep(2)
else:
    raise TimeoutError('Observability services did not become ready')
PY
curl -fsS http://127.0.0.1:30080/ > /dev/null
curl -fsS http://127.0.0.1:30090/api/health
# The base smoke test left this unsigned copy in the registry.
kubectl-gadget run -n demo registry.demo.svc.cluster.local:5000/demo/trace_open:demo \
  --timeout=30 > /dev/null &
gadget=$!
for _ in {1..8}; do
  sleep 2
  kubectl -n demo exec deployment/headlamp -- cat /etc/hostname > /dev/null
done
wait "$gadget"
# IG only reports its internal counters after use, so check them after the gadget ran.
query='/api/v1/namespaces/demo/services/prometheus:80/proxy/api/v1/query?query=ig_grpc_run_gadget_total'
for _ in {1..30}; do
  kubectl get --raw "$query" | grep -q '"namespace":"gadget"' && exit 0
  sleep 5
done
echo 'Prometheus never scraped the IG internal metrics' >&2
exit 1
