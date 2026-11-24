#!/usr/bin/env bash
# Prepare a kernel source tree matching the RUNNING WSL2 kernel so the module
# can be built against it.
#
# Do this while WSL networking is healthy (it downloads ~250MB). If you are
# already wedged, download the tarball from Windows and pass --tarball.
#
#   scripts/setup-kernel-tree.sh                    # download + prepare ./kernel-src
#   scripts/setup-kernel-tree.sh --tarball FILE     # use an already-downloaded tarball
#   scripts/setup-kernel-tree.sh --dest DIR         # prepare somewhere else
#
# Needs ~2.5GB disk and: gcc make flex bison pahole (dwarves) bc,
# libelf-dev libssl-dev. Install those first (apt: build-essential flex bison
# dwarves bc libelf-dev libssl-dev). If apt hangs because the network is
# wedged, fetch the .debs from Windows and `sudo dpkg -i` them: dpkg has no
# network dependency, apt hooks do.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
dest="$here/kernel-src"
tarball=""

while [ $# -gt 0 ]; do
  case "$1" in
    --tarball) tarball="$2"; shift 2 ;;
    --dest)    dest="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

rel="$(uname -r)"
case "$rel" in
  *-microsoft-standard-WSL2) ;;
  *) echo "warning: '$rel' does not look like a WSL2 kernel; the tag mapping below may be wrong" >&2 ;;
esac
ver="${rel%%-*}"                     # 6.18.58.1-microsoft-standard-WSL2 -> 6.18.58.1
tag="linux-msft-wsl-$ver"
url="https://github.com/microsoft/WSL2-Linux-Kernel/archive/refs/tags/$tag.tar.gz"
echo "running kernel : $rel"
echo "source tag     : $tag"
echo "destination    : $dest"

missing=()
for t in gcc make flex bison bc tar; do command -v "$t" >/dev/null || missing+=("$t"); done
[ -f /usr/include/libelf.h ] || missing+=("libelf-dev")
[ -f /usr/include/openssl/ssl.h ] || missing+=("libssl-dev")
if ! command -v pahole >/dev/null; then
  missing+=("pahole (package: dwarves)")
fi
if [ ${#missing[@]} -gt 0 ]; then
  echo "error: missing build prerequisites: ${missing[*]}" >&2
  echo "       pahole is REQUIRED: without it Kconfig silently drops" >&2
  echo "       CONFIG_DEBUG_INFO_BTF_MODULES, struct module comes out 24 bytes" >&2
  echo "       too small, and insmod fails with 'Invalid module format'." >&2
  exit 1
fi
[ -r /proc/config.gz ] || { echo "error: /proc/config.gz not readable" >&2; exit 1; }

if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
  echo "error: $dest already exists and is not empty. Remove it or use --dest." >&2
  exit 1
fi
mkdir -p "$dest"

if [ -z "$tarball" ]; then
  tarball="$(mktemp --suffix=.tar.gz)"
  trap 'rm -f "$tarball"' EXIT
  echo "downloading $url"
  curl -fL --retry 3 -o "$tarball" "$url"
fi

echo "extracting (this takes a minute)..."
tar -xzf "$tarball" -C "$dest" --strip-components=1

cd "$dest"
# ALWAYS start from the pristine running config. olddefconfig only fills in
# symbols that are ABSENT from .config; if a previous run dropped a symbol
# (e.g. no pahole) it will stay dropped.
zcat /proc/config.gz > .config
orig_btf_mod=0
grep -q '^CONFIG_DEBUG_INFO_BTF_MODULES=y' .config && orig_btf_mod=1

make olddefconfig >/dev/null
if [ "$orig_btf_mod" = 1 ] && ! grep -q '^CONFIG_DEBUG_INFO_BTF_MODULES=y' .config; then
  echo "error: CONFIG_DEBUG_INFO_BTF_MODULES was enabled in the running kernel" >&2
  echo "       but olddefconfig dropped it. Check 'pahole --version' (>= 1.16)." >&2
  exit 1
fi

echo "running modules_prepare..."
make -j"$(nproc)" modules_prepare >/dev/null

echo
echo "done. Next:  make   (then: make check)"
