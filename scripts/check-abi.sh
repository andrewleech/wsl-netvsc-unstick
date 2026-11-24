#!/usr/bin/env bash
# Verify the built module agrees with the RUNNING kernel on sizeof(struct
# module). A mismatch is the classic cause of
#   insmod: ERROR: could not insert module: Invalid module format
#   dmesg: ".gnu.linkonce.this_module section size must match the kernel's
#           built struct module size at run time"
# and means the kernel tree's .config differs from the running kernel's
# (usually a symbol Kconfig dropped, e.g. CONFIG_DEBUG_INFO_BTF_MODULES
# without pahole installed).
#
# Needs pahole and /sys/kernel/btf/vmlinux (CONFIG_DEBUG_INFO_BTF=y).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
ko="$here/netvsc_unstick.ko"

[ -f "$ko" ] || { echo "error: build first (make)" >&2; exit 1; }
command -v pahole >/dev/null || { echo "error: pahole not installed" >&2; exit 1; }
[ -r /sys/kernel/btf/vmlinux ] || { echo "error: /sys/kernel/btf/vmlinux missing" >&2; exit 1; }

size_of() { pahole -C module "$1" 2>/dev/null | sed -n 's|.*size: \([0-9]*\),.*|\1|p' | head -1; }

running="$(size_of /sys/kernel/btf/vmlinux)"
built="$(size_of "$ko")"
echo "running kernel sizeof(struct module): ${running:-unknown}"
echo "built module   sizeof(struct module): ${built:-unknown}"

if [ -z "$running" ] || [ -z "$built" ]; then
  echo "could not determine one of the sizes (does the .ko have DWARF? CONFIG_DEBUG_INFO=y)" >&2
  exit 2
fi
if [ "$running" = "$built" ]; then
  echo "OK: sizes match."
else
  echo "MISMATCH: insmod will fail. Diff the tree's .config against 'zcat /proc/config.gz'." >&2
  exit 1
fi
