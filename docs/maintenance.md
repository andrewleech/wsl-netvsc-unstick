# Maintenance guide

This module reaches into private driver structures, so it is tied to kernel
source. This page covers moving to a new WSL kernel, what in the code can break,
and how to diagnose build problems.

## Tested versions

| Kernel (`uname -r`) | Arch | Result |
|---|---|---|
| `6.18.58.1-microsoft-standard-WSL2` | x86_64 | Built, loaded, recovered a live wedge (11 dead channels). |
| `6.18.54.1-microsoft-standard-WSL2` | x86_64 | An earlier bit-clear-only version panicked the VM; the ring-buffer fix was developed against this tree and is the same logic. Not re-run on 54.1. |

Add rows as people try it. arm64 is untested (the fields used are
architecture-independent, but nobody has run it).

## Moving to a new kernel version

### 1. Rebuild the tree and the module (usually all that is needed)

```sh
rm -rf kernel-src                    # or use --dest for a new directory
scripts/setup-kernel-tree.sh         # maps uname -r -> tag linux-msft-wsl-<ver>
make clean; make; make check
```

The tag mapping is `6.18.58.1-microsoft-standard-WSL2` ->
`linux-msft-wsl-6.18.58.1`. If a kernel has no such tag (check
<https://github.com/microsoft/WSL2-Linux-Kernel/tags>), you cannot build
reliably for it; do not substitute the nearest tag, the structs may differ.

Because the module is compiled against the exact tree, **struct layout drift
between versions is handled by the compiler**. If a field is renamed or removed
you get a compile error, not silent corruption. That is the main reason the
setup script exists instead of vendoring headers. (An earlier version of this
project copied `hyperv_net.h` into the repo, and it went stale the first time the
kernel updated: 6.18.58.1 added `napi_enabled` to `struct netvsc_channel`, which
shifts every later offset.)

### 2. Re-verify the assumptions the code relies on

A clean compile does not prove the *behaviour* the module depends on still holds.
Re-check each of these in the new tree before trusting it. Paths are relative to
the kernel source tree; the commands assume you are inside `kernel-src`.

| # | Assumption | Where it is used | Check |
|---|---|---|---|
| 1 | A failed `netvsc_sc_open()` leaves the channel with `SCHED` and `NPSVC` set. | the match gate | `sed -n '/^static void netvsc_sc_open/,/^}/p' drivers/net/hyperv/rndis_filter.c` : failure branch still only disables NAPI and does not delete it or clear `nvchan->channel`? |
| 2 | `napi_disable()` sets exactly `SCHED \| NPSVC`, and `napi_enable()` clears them. | the gate | `sed -n '/^void napi_disable_locked/,/^}/p' net/core/dev.c` |
| 3 | `napi_synchronize()` is still an unbounded spin on `SCHED`. | the whole premise | `grep -n -A8 'static inline void napi_synchronize' include/linux/netdevice.h` : if it gained a timeout, the bug may no longer occur. |
| 4 | `netvsc_wait_until_empty()` calls `hv_get_bytes_to_read(&chn->inbound/outbound)` right after `napi_synchronize()`. | why fake rings are needed | `sed -n '/^static int netvsc_wait_until_empty/,/^}/p' drivers/net/hyperv/netvsc_drv.c` |
| 5 | `hv_get_bytes_to_read()` / `_write()` only read `ring_datasize` and `ring_buffer->read_index/write_index`. | the fake ring layout | `grep -n -A12 'hv_get_bytes_to_read' include/linux/hyperv.h`. If it reads more fields, the zeroed page may not be enough. |
| 6 | `struct hv_ring_buffer` starts with `write_index`, `read_index`. | "zeroed page == empty" | `sed -n '/^struct hv_ring_buffer {/,/^};/p' include/linux/hyperv.h` |
| 7 | The driver is still named `hv_netvsc`. | device filter | `grep -n 'KBUILD_MODNAME' drivers/net/hyperv/netvsc_drv.c` and `ls /sys/bus/vmbus/drivers/` |
| 8 | Sub-channel 0 is the primary and is never on the failing path. | loop starts at 1 | read `rndis_filter_device_add` / `netvsc_sc_open` index handling |
| 9 | NAPI bit numbering. | gate and docs (`0x19`, `0x10`) | `grep -n 'NAPI_STATE_' include/linux/netdevice.h` |

If any of 1-5 changed, **do not run the module** until you have re-derived the
failure chain in [how-it-works.md](how-it-works.md). Items 5-6 are the ones that
cause a panic if wrong (the first version crashed exactly here).

### 3. Look for an upstream fix

If Microsoft/upstream has fixed the failure path, this module becomes
unnecessary for that kernel. Signs: `netvsc_sc_open()` now calls
`netif_napi_del()` / clears `nvchan->channel` on failure;
`vmbus_alloc_ring()` falls back to smaller pages; or `napi_synchronize()` /
`netvsc_wait_until_empty()` bound their wait. Say so in the tested-versions table
and stop shipping builds for that version.

### 4. Test it properly

You cannot test on demand, the trigger needs weeks of fragmentation. In order of
preference:

1. Wait for a natural occurrence. Run `scripts/find_rtnl_holder.py`, then
   `echo "<if> dry"`, and only recover if the dry run shows the `0x19` + NULL
   rings signature and the dead count matches the dmesg failures.
2. Review the diff of the five functions in the table above between the old and
   new tag: `git diff linux-msft-wsl-<old>..linux-msft-wsl-<new> -- <file>`.

Do not "test" by faking the state on a healthy channel. Nulling a live ring
buffer is a different, unrelated way to crash the machine.

## What in the code is version-sensitive

All in `netvsc_unstick.c`:

| Code | Depends on |
|---|---|
| `netdev_priv(dev)` cast to `struct net_device_context` | `hv_netvsc` private data layout |
| `ndc->nvdev` (`__rcu`), `nvdev->num_chn`, `nvdev->chan_table[]`, `VRSS_CHANNEL_MAX` | `struct netvsc_device` |
| `nvchan->napi`, `nvchan->channel` | `struct netvsc_channel` |
| `chn->inbound` / `chn->outbound` (`struct hv_ring_buffer_info`), `.ring_buffer`, `.ring_datasize` | `struct vmbus_channel`, `include/linux/hyperv.h` |
| `NAPI_STATE_SCHED`, `NAPI_STATE_NPSVC` | `include/linux/netdevice.h` |
| `get_zeroed_page(GFP_ATOMIC)` | must stay atomic: we hold `rcu_read_lock()` |

Everything else (debugfs plumbing, parsing the write buffer) is generic kernel API
that changes rarely. If a newer kernel moves the lock (for example Microsoft's
6.18.58.1 comments mention a "netdev instance lock"), re-check the "nothing else
can mutate this netdev while `rtnl_lock` is stuck" argument in
[how-it-works.md](how-it-works.md#why-it-is-reasonably-safe-to-touch-these-fields-from-outside-the-driver).

## Build problems

| Symptom | Cause | Fix |
|---|---|---|
| `error: ... is not a prepared kernel tree` | `kernel-src` missing or not prepared. | `scripts/setup-kernel-tree.sh` |
| `error: kernel tree is X but running kernel is Y` | WSL kernel updated since you built the tree. | Re-run setup against the new kernel. |
| `No rule to make target 'modules'` | You pointed `KSRC` at a "headers" package (`/lib/modules/*/build` on WSL is `include/` only, no Kbuild). | Use the full source tree from the setup script. |
| `/bin/sh: flex: not found` during setup | Missing build dependency. | Install `flex bison`. If apt hangs (see below) use `dpkg -i`. |
| `insmod: Invalid module format` and dmesg `.gnu.linkonce.this_module section size must match` | `struct module` size differs: the tree's `.config` is not the running kernel's. Almost always `pahole` was missing when `olddefconfig` ran, so Kconfig **silently dropped** `CONFIG_DEBUG_INFO_BTF_MODULES`, removing 24 bytes from `struct module`. | Install `pahole` (dwarves), delete `kernel-src`, re-run setup (it always restarts from the pristine `/proc/config.gz`; re-running `olddefconfig` on an already-processed `.config` will *not* bring the option back). `make check` shows the two sizes. |
| `modpost: ... undefined!` errors | No `Module.symvers` (no full kernel build). | The Makefile exports `KBUILD_MODPOST_WARN=1`, making these warnings. Safe: the running kernel resolves real symbols at `insmod`. |
| `Skipping BTF generation ... due to unavailability of vmlinux` | Same, no vmlinux. | Ignore. |
| `insmod: ... Unknown symbol` | Rare; a symbol exists in the source but is not exported or is not built in. | Check `grep <sym> /proc/kallsyms`. |
| `apt-get install` hangs while the network is wedged | Ubuntu apt hooks do network calls even for local `.deb` files. | Download the `.deb`s from Windows and `sudo dpkg -i` them. `dpkg` has no network dependency. Kill the stuck apt tree first (it is `S` state, killable). |

## Quick reference for diagnosing a new kernel's wedge by hand

```sh
sudo grep -E ' (rtnl_mutex|init_task)$' /proc/kallsyms     # real addresses need root
pahole -C task_struct /sys/kernel/btf/vmlinux | grep -E '__state|stack|pid|comm|thread'
pahole -C thread_struct /sys/kernel/btf/vmlinux | grep ' sp;'
```

`scripts/find_rtnl_holder.py` does this automatically using pahole, so it adapts
to a new kernel without edits as long as BTF and `kallsyms` are available
(`CONFIG_DEBUG_INFO_BTF=y`, `CONFIG_KALLSYMS=y`, `kptr_restrict` must allow root).
