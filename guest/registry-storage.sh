#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/opt/bin:/usr/sbin:/usr/bin
registry=/var/lib/demo-registry
device=/dev/disk/by-id/virtio-ig-registry
legacy=/dev/disk/by-id/virtio-ig-legacy
marker=$registry/.ig-legacy-migrated
temporary=
legacy_mounts=()

fail() {
  echo "Registry storage: $*" >&2
  exit 1
}

cleanup() {
  local result=$? directory
  trap - EXIT
  for directory in "${legacy_mounts[@]}"; do
    if mountpoint -q "$directory"; then
      umount "$directory" || result=1
    fi
    rmdir "$directory" || result=1
  done
  if [[ -n "$temporary" ]]; then
    rmdir "$temporary" || result=1
  fi
  exit "$result"
}
trap cleanup EXIT

udevadm settle --timeout=30
if [[ ! -e "$device" ]] && ! grep -qs 'io.inspektor-gadget.demo.token=' /sys/firmware/dmi/entries/11-*/raw; then
  # Not started by the launcher (e.g. Lima): the OS disk persists, so the registry lives on it.
  echo 'Registry storage: no registry disk; keeping the registry on the OS disk'
  mkdir -p "$registry"
  exit 0
fi
[[ -b "$device" ]] || fail 'Missing registry disk'
[[ $(blkid -p -s TYPE -o value "$device") == ext4 ]] || fail 'Registry disk must be ext4'
[[ $(blkid -p -s LABEL -o value "$device") == ig-registry ]] || fail 'Wrong registry disk label'
[[ ! -L "$registry" ]] || fail 'Registry mountpoint must not be a symlink'
mkdir -p "$registry"
if mountpoint -q "$registry"; then
  [[ $(findmnt -rn -M "$registry" -o SOURCE) -ef "$device" ]] || fail 'Wrong mounted registry disk'
  options=$(findmnt -rn -M "$registry" -o VFS-OPTIONS)
  [[ ",$options," == *,rw,* ]] || fail 'Registry disk is read-only'
else
  mount -t ext4 -o rw,nosuid,nodev "$device" "$registry"
fi

# A completed migration must never overwrite pushes made on later launches.
if [[ -f "$marker" && ! -L "$marker" ]]; then
  exit 0
fi
[[ ! -e "$marker" && ! -L "$marker" ]] || fail 'Invalid migration marker'
if [[ ! -e "$legacy" && ! -L "$legacy" ]]; then
  exit 0
fi
[[ -b "$legacy" ]] || fail 'Invalid legacy disk'
temporary=$(mktemp -d /run/demo-registry-migration.XXXXXX)
sources=()
shopt -s nullglob
for partition in "$legacy"-part*; do
  [[ -b "$partition" ]] || fail "Invalid legacy partition: $partition"
  if filesystem=$(blkid -p -s TYPE -o value "$partition"); then
    [[ "$filesystem" == ext4 ]] || continue
  else
    result=$?
    [[ "$result" == 2 ]] || exit "$result"
    continue
  fi
  directory=$temporary/${#legacy_mounts[@]}
  mkdir "$directory"
  legacy_mounts+=("$directory")
  # ext4 may replay its journal; the host supplies a writable, disposable overlay.
  mount -t ext4 -o ro,nosuid,nodev,noexec "$partition" "$directory"
  source=$directory/var/lib/demo-registry
  if [[ -d "$source" && ! -L "$directory/var" && ! -L "$directory/var/lib" && ! -L "$source" ]]; then
    sources+=("$source")
  fi
done
[[ ${#sources[@]} == 1 ]] || fail "Expected one legacy registry root, found ${#sources[@]}"
cp -a -- "${sources[0]}/." "$registry/"
sync -f "$registry"
pending=$(mktemp "$registry/.ig-legacy-migrated.XXXXXX")
printf 'migrated\n' > "$pending"
sync -f "$pending"
mv -fT -- "$pending" "$marker"
sync -f "$registry"
