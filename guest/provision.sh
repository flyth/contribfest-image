#!/usr/bin/env bash
set -euxo pipefail
failed() {
  journalctl -u demo.service -u kubelet.service -u containerd.service -n 150 --no-pager || true
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
export DEBIAN_FRONTEND=noninteractive HOME=/root KUBECONFIG=/etc/kubernetes/admin.conf
arch=$(dpkg --print-architecture)
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl containerd conntrack iptables iproute2 socat \
  containernetworking-plugins python3
update-alternatives --set iptables /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNS=10.0.2.3\n' > /etc/systemd/resolved.conf.d/demo.conf
systemctl restart systemd-resolved
mkdir -p /opt/cni/bin /etc/containerd
cp -a /usr/lib/cni/. /opt/cni/bin/
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd

fetch() {
  curl --fail --location --retry 5 --output "$2" "$1"
}
fetch "https://github.com/kubernetes/minikube/releases/download/$MINIKUBE_VERSION/minikube-linux-$arch" /usr/local/bin/minikube
fetch "https://github.com/kubernetes/minikube/releases/download/$MINIKUBE_VERSION/minikube-linux-$arch.sha256" /tmp/minikube.sha256
echo "$(cat /tmp/minikube.sha256)  /usr/local/bin/minikube" | sha256sum -c -
fetch "https://dl.k8s.io/release/$KUBERNETES_VERSION/bin/linux/$arch/kubectl" /usr/local/bin/kubectl
fetch "https://dl.k8s.io/release/$KUBERNETES_VERSION/bin/linux/$arch/kubectl.sha256" /tmp/kubectl.sha256
echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c -
fetch "https://get.helm.sh/helm-$HELM_VERSION-linux-$arch.tar.gz" /tmp/helm.tar.gz
fetch "https://get.helm.sh/helm-$HELM_VERSION-linux-$arch.tar.gz.sha256sum" /tmp/helm.sha256
echo "$(awk '{print $1}' /tmp/helm.sha256)  /tmp/helm.tar.gz" | sha256sum -c -
tar -xzf /tmp/helm.tar.gz -C /usr/local/bin --strip-components=1 "linux-$arch/helm"
fetch "https://github.com/kubernetes-sigs/cri-tools/releases/download/$CRICTL_VERSION/crictl-$CRICTL_VERSION-linux-$arch.tar.gz" /tmp/crictl.tar.gz
fetch "https://github.com/kubernetes-sigs/cri-tools/releases/download/$CRICTL_VERSION/crictl-$CRICTL_VERSION-linux-$arch.tar.gz.sha256" /tmp/crictl.sha256
echo "$(cat /tmp/crictl.sha256)  /tmp/crictl.tar.gz" | sha256sum -c -
tar -xzf /tmp/crictl.tar.gz -C /usr/local/bin crictl
fetch "https://github.com/inspektor-gadget/inspektor-gadget/releases/download/$IG_VERSION/kubectl-gadget-linux-$arch-$IG_VERSION.tar.gz" /tmp/gadget.tar.gz
fetch "https://github.com/inspektor-gadget/inspektor-gadget/releases/download/$IG_VERSION/SHA256SUMS" /tmp/ig.sha256
expected=$(grep " kubectl-gadget-linux-$arch-$IG_VERSION.tar.gz$" /tmp/ig.sha256 | awk '{print $1}')
test -n "$expected"
echo "$expected  /tmp/gadget.tar.gz" | sha256sum -c -
tar -xzf /tmp/gadget.tar.gz -C /usr/local/bin kubectl-gadget
oras_asset="oras_${ORAS_VERSION#v}_linux_$arch.tar.gz"
fetch "https://github.com/oras-project/oras/releases/download/$ORAS_VERSION/$oras_asset" /tmp/oras.tar.gz
fetch "https://github.com/oras-project/oras/releases/download/$ORAS_VERSION/oras_${ORAS_VERSION#v}_checksums.txt" /tmp/oras.sha256
expected=$(grep " $oras_asset$" /tmp/oras.sha256 | awk '{print $1}')
test -n "$expected"
echo "$expected  /tmp/oras.tar.gz" | sha256sum -c -
tar -xzf /tmp/oras.tar.gz -C /usr/local/bin oras
chmod +x /usr/local/bin/{minikube,kubectl,kubectl-gadget}
systemctl daemon-reload
systemctl enable demo.service demo-api.service
systemctl start demo.service
helm repo add gadget https://inspektor-gadget.github.io/charts
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/
helm repo update
sed "s/\${REGISTRY_VERSION}/$REGISTRY_VERSION/" /opt/demo/registry.yaml | kubectl apply -f -
helm upgrade --install gadget gadget/gadget --namespace gadget --create-namespace \
  --version "${IG_VERSION#v}" -f /opt/demo/gadget-values.yaml
# The chart's one-second exec startup probe is too tight on emulated CPUs.
kubectl -n gadget patch daemonset gadget --type=strategic -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"gadget",
    "startupProbe":{"timeoutSeconds":10,"periodSeconds":10,"failureThreshold":30},
    "livenessProbe":{"timeoutSeconds":10,"periodSeconds":10},
    "readinessProbe":{"timeoutSeconds":10,"periodSeconds":10}}]}}}}'
kubectl -n gadget rollout status daemonset/gadget --timeout=600s
helm upgrade --install headlamp headlamp/headlamp --namespace demo \
  --version "$HEADLAMP_CHART_VERSION" -f /opt/demo/headlamp-values.yaml --wait --timeout 5m
kubectl -n demo rollout status deployment/registry --timeout=180s
/opt/demo/smoke.sh

apt-get purge -y snapd lxd-installer lxd-agent-loader apport apport-core-dump-handler \
  openssh-server openssh-sftp-server pollinate unattended-upgrades
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer || true
apt-get clean
rm -rf /var/lib/apt/lists /root/.cache/helm
rm -f /tmp/minikube.sha256 /tmp/kubectl.sha256 /tmp/helm.tar.gz /tmp/helm.sha256 \
  /tmp/crictl.tar.gz /tmp/crictl.sha256 /tmp/gadget.tar.gz /tmp/ig.sha256 \
  /tmp/oras.tar.gz /tmp/oras.sha256
touch /etc/cloud/cloud-init.disabled
fstrim -av
sync
echo DEMO_IMAGE_READY
systemctl poweroff
