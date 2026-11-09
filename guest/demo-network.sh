#!/usr/bin/env bash
# The cluster is initialized for QEMU user networking: node IP 10.0.2.15, DNS 10.0.2.3.
# Other VM managers (Lima) hand out other addresses, so the node keeps its baked
# address on the loopback link and DNS comes from DHCP.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin
node=10.0.2.15
dropin=/run/systemd/resolved.conf.d/demo.conf
if ip -4 -o addr show scope global | grep -v ' lo ' | grep -q " inet $node/"; then
  mkdir -p "${dropin%/*}"
  printf '[Resolve]\nDNS=10.0.2.3\n' > "$dropin"
else
  echo "No $node on a network link; adding it to lo for the cluster"
  ip addr replace "$node/32" dev lo
  rm -f "$dropin"
fi
systemctl try-reload-or-restart systemd-resolved.service
