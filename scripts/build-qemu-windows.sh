#!/usr/bin/env bash
# Build the minimal QEMU runtime the Windows app bundles: the x86_64 WHPX system
# emulator, qemu-img, the DLLs they need, and the option ROMs the demo VM loads.
#
#   scripts/build-qemu-windows.sh [--if-missing] [OUTPUT_DIR]
#
# Run in an MSYS2 UCRT64 shell with these packages installed:
#   pacman -S --needed diffutils mingw-w64-ucrt-x86_64-{gcc,glib2,pixman,libslirp,dtc,ninja,pkgconf,python}
# OUTPUT_DIR defaults to src-tauri/qemu, which tauri.windows.conf.json bundles.
# The DLLs sit beside the executables, where Windows looks first; nothing is signed.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/qemu.env
source "$root/scripts/qemu.env"
QEMU_URL="https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz"
packages="diffutils mingw-w64-ucrt-x86_64-{gcc,glib2,pixman,libslirp,dtc,ninja,pkgconf,python}"
if_missing=0
if [[ ${1:-} == --if-missing ]]; then
  if_missing=1
  shift
fi
out=${1:-$root/src-tauri/qemu}
target=x86_64
emulator=qemu-system-$target

die() { echo "build-qemu-windows: $*" >&2; exit 1; }

[[ ${MSYSTEM:-} == UCRT64 ]] || die "run this in an MSYS2 UCRT64 shell"
if (( if_missing )) && [[ -f $out/VERSION ]] && [[ $(cat "$out/VERSION") == "$QEMU_VERSION-$target" ]]; then
  echo "build-qemu-windows: $out already has QEMU $QEMU_VERSION ($target)"
  exit 0
fi
for tool in gcc ninja pkg-config python3 curl sha256sum ldd cygpath pacman; do
  command -v "$tool" >/dev/null || die "missing $tool (pacman -S --needed $packages)"
done
for pc in glib-2.0 pixman-1 slirp libfdt; do
  pkg-config --exists "$pc" || die "missing $pc (pacman -S --needed $packages)"
done

work=$(mktemp -d "${TMPDIR:-/tmp}/ig-qemu.XXXXXX")
trap 'rm -rf "$work"' EXIT
cache=${QEMU_CACHE_DIR:-$root/build/qemu-cache}
mkdir -p "$cache"
archive=$cache/qemu-$QEMU_VERSION.tar.xz
if [[ ! -f $archive ]] || ! echo "$QEMU_SHA256  $archive" | sha256sum -c - >/dev/null 2>&1; then
  echo "build-qemu-windows: downloading $QEMU_URL"
  curl --fail --location --retry 5 --output "$archive.part" "$QEMU_URL"
  mv "$archive.part" "$archive"
fi
echo "$QEMU_SHA256  $archive" | sha256sum -c -
# The tarball has symlinks for other hosts' test data; MSYS2 tar cannot create them.
tar -xJf "$archive" -C "$work" 2>"$work/tar.log" || grep -v "Cannot create symlink" "$work/tar.log" >&2 || true
src=$work/qemu-$QEMU_VERSION
[[ -f $src/configure ]] || die "could not extract $archive"

mkdir "$src/build"
(
  cd "$src/build"
  ../configure \
    --target-list="$target-softmmu" \
    --without-default-features \
    --enable-system \
    --enable-tools \
    --enable-whpx \
    --enable-tcg \
    --enable-slirp \
    --enable-fdt=system \
    --enable-pixman \
    --disable-debug-info \
    --disable-werror \
    --disable-download \
    --disable-docs
)
ninja -C "$src/build" "$emulator.exe" qemu-img.exe

stage=$work/runtime
mkdir -p "$stage/bin" "$stage/share/qemu" "$stage/licenses"
cp "$src/build/$emulator.exe" "$src/build/qemu-img.exe" "$stage/bin/"
# Option ROMs for virtio-net and the q35 machine's BIOS.
for rom in efi-virtio.rom bios-256k.bin kvmvapic.bin linuxboot_dma.bin multiboot_dma.bin vgabios-stdvga.bin; do
  cp "$src/pc-bios/$rom" "$stage/share/qemu/"
done
cp "$src/COPYING" "$stage/licenses/QEMU-COPYING"
cp "$src/COPYING.LIB" "$stage/licenses/QEMU-COPYING.LIB"

# Copy every UCRT64 DLL the executables reach (system DLLs stay out).
ucrt_bin=$(cygpath -m /ucrt64/bin)
declare -A copied=()
queue=("$stage/bin/$emulator.exe" "$stage/bin/qemu-img.exe")
while ((${#queue[@]})); do
  file=${queue[0]}
  queue=("${queue[@]:1}")
  while IFS= read -r path; do
    path=${path//\\//}
    if [[ $path == "$ucrt_bin"/* ]]; then
      path=/ucrt64/bin/${path##*/}
    fi
    [[ $path == /ucrt64/bin/* ]] || continue
    name=${path##*/}
    if [[ -n ${copied[$name]:-} ]]; then
      [[ ${copied[$name]} == "$path" ]] || die "two different $name DLLs"
      continue
    fi
    copied[$name]=$path
    cp "$path" "$stage/bin/$name"
    queue+=("$stage/bin/$name")
  done < <(ldd "$file" | sed -nE 's/.* => ([^ ]+) \(.*/\1/p')
done
((${#copied[@]})) || die "ldd found no UCRT64 DLLs; the copy step is broken"

# Record the exact MSYS2 packages behind the DLLs, with their licenses.
{
  echo "QEMU $QEMU_VERSION, built from $QEMU_URL (sha256 $QEMU_SHA256)"
  echo "configure: --target-list=$target-softmmu --without-default-features --enable-system"
  echo "  --enable-tools --enable-whpx --enable-tcg --enable-slirp --enable-fdt=system --enable-pixman"
  echo
  echo "Bundled DLLs (MSYS2 UCRT64 packages; sources at https://packages.msys2.org):"
} > "$stage/licenses/SOURCES.txt"
declare -A owners=()
for name in "${!copied[@]}"; do
  owner=$(pacman -Qqo "${copied[$name]}" 2>/dev/null || true)
  [[ -n $owner ]] || continue
  owners[$owner]=1
  echo "  $name: $owner" >> "$stage/licenses/SOURCES.txt"
done
for owner in "${!owners[@]}"; do
  echo "  package $(pacman -Q "$owner")" >> "$stage/licenses/SOURCES.txt"
  licenses=/ucrt64/share/licenses/${owner#mingw-w64-ucrt-x86_64-}
  [[ -d $licenses ]] && cp -R "$licenses" "$stage/licenses/${owner#mingw-w64-ucrt-x86_64-}"
done

# Prove the runtime works outside MSYS2: only its own folder and Windows on PATH.
system_path=$(cygpath -u "${SYSTEMROOT:-C:\\Windows}")/System32
PATH="$stage/bin:$system_path" "$stage/bin/qemu-img.exe" --version
PATH="$stage/bin:$system_path" "$stage/bin/$emulator.exe" -accel help | tr -d '\r' | grep -qx whpx ||
  die "$emulator was built without WHPX"
PATH="$stage/bin:$system_path" "$stage/bin/$emulator.exe" -L "$(cygpath -m "$stage/share/qemu")" -machine help >/dev/null

echo "$QEMU_VERSION-$target" > "$stage/VERSION"
rm -rf "$out"
mkdir -p "$(dirname "$out")"
mv "$stage" "$out"
du -sh "$out"
echo "build-qemu-windows: QEMU $QEMU_VERSION runtime ready in $out"
