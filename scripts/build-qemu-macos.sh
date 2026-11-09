#!/usr/bin/env bash
# Build the minimal QEMU runtime the macOS app bundles: one HVF system emulator,
# qemu-img, the dylibs they need, and the option ROMs the demo VM loads.
#
#   scripts/build-qemu-macos.sh [--if-missing] [OUTPUT_DIR]
#
# OUTPUT_DIR defaults to src-tauri/qemu, which tauri.macos.conf.json bundles.
# Needs Xcode command line tools and Homebrew:
#   brew install ninja pkg-config glib pixman libslirp dtc
# Binaries are ad-hoc signed (no Apple account); the emulator carries the
# hypervisor entitlement that HVF requires.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/qemu.env
source "$root/scripts/qemu.env"
QEMU_URL="https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz"
if_missing=0
if [[ ${1:-} == --if-missing ]]; then
  if_missing=1
  shift
fi
out=${1:-$root/src-tauri/qemu}

die() { echo "build-qemu-macos: $*" >&2; exit 1; }

[[ $(uname -s) == Darwin ]] || die "run this on macOS"
case $(uname -m) in
  arm64) target=aarch64; tcg=--disable-tcg ;;
  # Intel keeps TCG so QEMU's x86 HVF path has its usual fallback helpers.
  x86_64) target=x86_64; tcg=--enable-tcg ;;
  *) die "unsupported Mac architecture $(uname -m)" ;;
esac
emulator=qemu-system-$target
# Tauri sets this for cross builds; a native QEMU would be the wrong architecture.
if [[ -n ${TAURI_ENV_ARCH:-} && $TAURI_ENV_ARCH != "$target" ]]; then
  die "building for $TAURI_ENV_ARCH on a $target Mac; build the app on a matching Mac"
fi

if (( if_missing )) && [[ -f $out/VERSION ]] && [[ $(cat "$out/VERSION") == "$QEMU_VERSION-$target" ]]; then
  echo "build-qemu-macos: $out already has QEMU $QEMU_VERSION ($target)"
  exit 0
fi

command -v brew >/dev/null || die "Homebrew is required: https://brew.sh"
for tool in ninja pkg-config codesign install_name_tool otool shasum curl python3; do
  command -v "$tool" >/dev/null || die "missing $tool (brew install ninja pkg-config glib pixman libslirp dtc)"
done
for pc in glib-2.0 pixman-1 slirp libfdt; do
  pkg-config --exists "$pc" || die "missing $pc (brew install ninja pkg-config glib pixman libslirp dtc)"
done
python=
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
  command -v "$candidate" >/dev/null || continue
  if "$candidate" -c 'import importlib.util; assert importlib.util.find_spec("tomllib") or importlib.util.find_spec("tomli")' 2>/dev/null; then
    python=$(command -v "$candidate")
    break
  fi
done
[[ -n $python ]] || die "Python with TOML support is required (brew install python)"

work=$(mktemp -d "${TMPDIR:-/tmp}/ig-qemu.XXXXXX")
trap 'rm -rf "$work"' EXIT
cache=${QEMU_CACHE_DIR:-$root/build/qemu-cache}
mkdir -p "$cache"
archive=$cache/qemu-$QEMU_VERSION.tar.xz
if [[ ! -f $archive ]] || ! echo "$QEMU_SHA256  $archive" | shasum -a 256 -c - >/dev/null 2>&1; then
  echo "build-qemu-macos: downloading $QEMU_URL"
  curl --fail --location --retry 5 --output "$archive.part" "$QEMU_URL"
  mv "$archive.part" "$archive"
fi
echo "$QEMU_SHA256  $archive" | shasum -a 256 -c -
tar -xJf "$archive" -C "$work"
src=$work/qemu-$QEMU_VERSION

mkdir "$src/build"
(
  cd "$src/build"
  ../configure \
    --python="$python" \
    --target-list="$target-softmmu" \
    --without-default-features \
    --enable-system \
    --enable-tools \
    --enable-hvf \
    "$tcg" \
    --enable-slirp \
    --enable-fdt=system \
    --enable-pixman \
    --disable-debug-info \
    --disable-werror \
    --disable-download \
    --disable-docs
)
ninja -C "$src/build" "$emulator" qemu-img

stage=$work/runtime
mkdir -p "$stage/bin" "$stage/lib" "$stage/share/qemu" "$stage/licenses"
cp "$src/build/$emulator" "$src/build/qemu-img" "$stage/bin/"
# Option ROMs: virtio-net always; the x86 q35 machine also needs its BIOS images.
roms=(efi-virtio.rom)
if [[ $target == x86_64 ]]; then
  roms+=(bios-256k.bin kvmvapic.bin linuxboot_dma.bin multiboot_dma.bin vgabios-stdvga.bin)
fi
for rom in "${roms[@]}"; do
  cp "$src/pc-bios/$rom" "$stage/share/qemu/"
done
cp "$src/COPYING" "$stage/licenses/QEMU-COPYING"
cp "$src/COPYING.LIB" "$stage/licenses/QEMU-COPYING.LIB"

# Copy every non-system dylib the binaries reach, then point them at lib/.
deps() { otool -L "$1" | tail -n +2 | awk '{print $1}'; }
external() { [[ $1 != /usr/lib/* && $1 != /System/* && $1 != @* ]]; }
queue=("$stage/bin/$emulator" "$stage/bin/qemu-img")
while ((${#queue[@]})); do
  file=${queue[0]}
  queue=("${queue[@]:1}")
  while IFS= read -r dep; do
    [[ $dep == @rpath/* ]] && die "$(basename "$file") links $dep; only absolute Homebrew paths are supported"
    external "$dep" || continue
    name=$(basename "$dep")
    [[ -f $stage/lib/$name ]] && continue
    cp "$dep" "$stage/lib/$name"
    chmod u+w "$stage/lib/$name"
    queue+=("$stage/lib/$name")
  done < <(deps "$file")
done

relink() {
  local file=$1 prefix=$2 dep
  while IFS= read -r dep; do
    external "$dep" || continue
    install_name_tool -change "$dep" "$prefix/$(basename "$dep")" "$file"
  done < <(deps "$file")
}
for lib in "$stage"/lib/*.dylib; do
  install_name_tool -id "@loader_path/$(basename "$lib")" "$lib"
  relink "$lib" @loader_path
done
for bin in "$stage"/bin/*; do
  relink "$bin" @executable_path/../lib
done

# Record where the dylibs came from, with their licenses, for redistribution.
{
  echo "QEMU $QEMU_VERSION, built from $QEMU_URL (sha256 $QEMU_SHA256)"
  echo "configure: --target-list=$target-softmmu --without-default-features --enable-system"
  echo "  --enable-tools --enable-hvf $tcg --enable-slirp --enable-fdt=system --enable-pixman"
  echo
  echo "Bundled libraries (Homebrew formulae; sources at https://formulae.brew.sh):"
} > "$stage/licenses/SOURCES.txt"
for lib in "$stage"/lib/*.dylib; do
  name=$(basename "$lib")
  origin=$(find -L "$(brew --prefix)/opt" -maxdepth 3 -name "$name" -print -quit 2>/dev/null || true)
  [[ -n $origin ]] || continue
  keg=$(cd "$(dirname "$origin")/.." && pwd -P)
  echo "  $name: $(basename "$(dirname "$keg")") $(basename "$keg")" >> "$stage/licenses/SOURCES.txt"
  for license in "$keg"/{COPYING,COPYING.LIB,LICENSE,LICENSE.txt,LICENSES}; do
    [[ -e $license ]] && cp -R "$license" "$stage/licenses/$(basename "$(dirname "$keg")")-$(basename "$license")"
  done
done

# install_name_tool invalidates signatures: re-sign libraries, then binaries.
cat > "$work/hvf.entitlements" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.hypervisor</key>
    <true/>
</dict>
</plist>
EOF
for lib in "$stage"/lib/*.dylib; do
  codesign --force --sign - "$lib"
done
codesign --force --sign - "$stage/bin/qemu-img"
codesign --force --sign - --entitlements "$work/hvf.entitlements" "$stage/bin/$emulator"

# Prove the runtime is self-contained and HVF-capable before publishing it.
for file in "$stage"/bin/* "$stage"/lib/*.dylib; do
  codesign --verify --strict "$file"
  if deps "$file" | grep -Eq '^/(opt|usr/local)/'; then
    die "$(basename "$file") still links outside the bundle"
  fi
done
codesign -d --entitlements - "$stage/bin/$emulator" 2>/dev/null | grep -q com.apple.security.hypervisor ||
  die "$emulator lacks the hypervisor entitlement"
env -i "$stage/bin/qemu-img" --version
env -i "$stage/bin/$emulator" -accel help | grep -qx hvf || die "$emulator was built without HVF"
env -i "$stage/bin/$emulator" -L "$stage/share/qemu" -machine help >/dev/null

echo "$QEMU_VERSION-$target" > "$stage/VERSION"
rm -rf "$out"
mkdir -p "$(dirname "$out")"
mv "$stage" "$out"
du -sh "$out"
echo "build-qemu-macos: QEMU $QEMU_VERSION runtime ready in $out"
