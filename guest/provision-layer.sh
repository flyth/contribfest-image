#!/usr/bin/env bash
# Installs the observability layer on top of a provisioned base disk. Only the
# image builder sets the SMBIOS flag; normal boots exit immediately.
set -Eeuo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
# A missing or unreadable SMBIOS record means "no layer requested".
layer=$(python3 -c 'import runpy; print(runpy.run_path("/opt/demo/api.py")["smbios_value"]("io.inspektor-gadget.demo.provision") or "")' 2>/dev/null || true)
[[ $layer == observability ]] || exit 0
set -x
failed() {
  kubectl --request-timeout=10s get pods -A -o wide || true
  kubectl --request-timeout=10s -n demo describe pods || true
  echo DEMO_LAYER_FAILED
  systemctl poweroff
}
trap failed ERR
# shellcheck source=versions.env
source /opt/demo/versions.env
export HOME=/root KUBECONFIG=/etc/kubernetes/admin.conf
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana-community https://grafana-community.github.io/helm-charts
helm repo update
helm upgrade --install headlamp headlamp/headlamp --namespace demo \
  --version "$HEADLAMP_CHART_VERSION" -f /opt/demo/headlamp-values.yaml --wait --timeout 15m
helm upgrade --install prometheus prometheus-community/prometheus --namespace demo \
  --version "$PROMETHEUS_CHART_VERSION" -f /opt/demo/prometheus-values.yaml --wait --timeout 15m
helm upgrade --install grafana grafana-community/grafana --namespace demo \
  --version "$GRAFANA_CHART_VERSION" -f /opt/demo/grafana-values.yaml \
  --set-file dashboards.default.ig-demo.json=/opt/demo/grafana-dashboard.json --wait --timeout 60m
mkdir -p /opt/demo/layers
touch /opt/demo/layers/observability
/opt/demo/smoke-observability.sh
rm -rf /root/.cache/helm /root/.config/helm
fstrim -av
sync
echo DEMO_LAYER_READY
systemctl poweroff
