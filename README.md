# netvsc-unstick

Live recovery for the WSL2 networking hang described in
[microsoft/WSL#41622](https://github.com/microsoft/WSL/issues/41622): when
`hv_netvsc` fails to open a sub-channel (`sub channel open failed: -12`),
`rtnl_lock` ends up held forever and *every* networking-adjacent call in the
distro hangs until you `wsl --shutdown`. This out-of-tree kernel module
unwedges it **without restarting WSL**.

> **Status: experimental.** Used successfully once, on
> `6.18.58.1-microsoft-standard-WSL2` (x86_64, mirrored networking). An earlier
> version of the idea panicked the VM. It taints your kernel, it pokes kernel
> memory from outside the driver, and it has had very little testing. Read
> [How it works](docs/how-it-works.md) before using it, and save your work
> first. If it goes wrong the failure mode is a VM crash/restart, which is
> what you would have done to recover anyway.

It is a **recovery tool, not a fix.** It does not stop the allocation failure
that causes the problem. See [Limitations](#limitations).

## Is this your bug?

You have it if, after the network goes away inside WSL while Windows is fine
(often after sleep/resume or a dock/lid cycle, and long WSL uptime):

1. `ip route`, `ping google.com`, `ssh` etc. hang and **do not die with Ctrl-C
   or `kill -9`** (they are in uninterruptible `D` state), and
2. the kernel log has a failed allocation in `vmbus_alloc_ring` followed by
   sub-channel failures:

   ```
   $ sudo dmesg | grep -E "page allocation failure|sub channel open failed"
   kworker/0:0: page allocation failure: order:8, mode:0xdc0(GFP_KERNEL|__GFP_ZERO)
   hv_netvsc cb071f1c-...: eth7: sub channel open failed: -12
   (repeated, once per failed channel)
   ```

3. Confirm *who* holds the lock. The holder is `GnsEngine`, WSL's in-guest
   network daemon, which lives in the VM's root PID namespace and so **does not
   appear in `ps` or `/proc/*/stack` inside your distro**.
   `scripts/find_rtnl_holder.py` reads it straight out of kernel memory
   without touching the (wedged) network stack:

   ```
   $ sudo python3 scripts/find_rtnl_holder.py
   rtnl_mutex @ 0xffffffff843fdf60  owner raw = 0xffff8ea28632de01
   waiters present: True
   owner: pid=245 comm=GnsEngine __state=0x2 (0x2 = uninterruptible)
      272  msleep+0x38
      304  netvsc_wait_until_empty+0x49
      360  netvsc_detach+0x135
      440  netvsc_change_mtu+0xb9
   ```

   `msleep <- netvsc_wait_until_empty <- netvsc_detach <- netvsc_change_mtu` is
   this bug. If the owner is something else, or the mutex is unlocked, this
   module will not help.

## Requirements

- WSL2, x86_64, a kernel you can build against: this has only been tried on
  `6.18.58.1`. Other 6.18.x should need no code changes; other major versions
  may. See [docs/maintenance.md](docs/maintenance.md).
- Root inside the distro; `debugfs` mounted (it is by default on WSL2).
- Build tools: `gcc make flex bison bc pahole libelf-dev libssl-dev`
  (apt: `build-essential flex bison dwarves bc libelf-dev libssl-dev`).
- ~2.5GB disk for the kernel source tree.

**Prepare this while the network works.** The module can only be built against
the full kernel source tree matching the running kernel (it uses private
`drivers/net/hyperv` structures; WSL2 ships no usable headers package), and
fetching that needs network. Do the setup and build ahead of time and just
leave the `.ko` on disk.

## Build

```sh
git clone https://github.com/<you>/netvsc-unstick && cd netvsc-unstick

# one-time per kernel version: downloads ~250MB, configures from the running
# kernel's /proc/config.gz, runs modules_prepare
scripts/setup-kernel-tree.sh

make            # builds netvsc_unstick.ko
make check      # confirms struct module ABI matches the running kernel
```

`make` refuses to build if the tree version differs from `uname -r`. After a
`wsl --update` that changes the kernel you must re-run the setup script (into a
fresh `kernel-src`) and rebuild: the module will not load on any other version.

If you are already wedged and cannot download from inside WSL, download
`https://github.com/microsoft/WSL2-Linux-Kernel/archive/refs/tags/linux-msft-wsl-<version>.tar.gz`
from Windows and pass it in: `scripts/setup-kernel-tree.sh --tarball /mnt/c/.../file.tar.gz`.

Troubleshooting the build is in [docs/maintenance.md](docs/maintenance.md#build-problems).

## Use

```sh
sudo insmod netvsc_unstick.ko        # loading does nothing by itself
```

When the network is wedged:

```sh
# 1. Look first. Reports every sub-channel's state, modifies nothing.
echo "eth7 dry" | sudo tee /sys/kernel/debug/netvsc_unstick/fix
sudo dmesg | grep netvsc_unstick | tail -20
```

Healthy and dead channels are easy to tell apart:

```
DRY eth7 chan[1] napi.state=0x10 chan=ffff... in.rb=ffffd3fa... out.rb=ffffd3fa...   <- healthy
DRY eth7 chan[5] napi.state=0x19 chan=ffff... in.rb=0000000000000000 out.rb=0000000000000000   <- dead
dry run: 11 dead channel(s) found, 0 recovered
```

`0x10` is just `LISTED`; `0x19` is `SCHED|NPSVC|LISTED` with NULL ring buffers:
the signature of a channel whose open failed. The number of dead channels
should match the number of `sub channel open failed` lines. If it does not,
stop and report it.

```sh
# 2. Recover. Name the wedged interface (the one in the dmesg lines).
echo eth7 | sudo tee /sys/kernel/debug/netvsc_unstick/fix
```

`ip route`, `ping` etc. should start returning within about a second, and
`find_rtnl_holder.py` should report the mutex unlocked.

`echo 1 | sudo tee .../fix` applies to every `hv_netvsc` device instead of one.
Prefer naming the interface.

```sh
sudo rmmod netvsc_unstick            # optional
```

### Expected side effects

- Kernel tainted (`O`), and `no symbol version for module_layout` in dmesg.
  Both are expected for this build method.
- **One kernel `WARNING` per recovered channel** from
  `__netif_napi_del_locked` (`net/core/dev.c`), logged by `GnsEngine`'s detach
  as it tears the device down. It asserts a flag we cleared; harmless.
- Two zeroed pages (8KB) leaked per recovered channel, on purpose.

## Limitations

- **It recovers; it does not prevent.** Recovery makes `GnsEngine` finish its
  detach and re-attach the NIC, and that re-attach can hit the same
  order-8 allocation failure and leave *new* dead channels, so it can wedge
  again at the next MTU change. Check with `echo "<if> dry"` after recovering
  and run it again if needed. In the one real run, 11 channels were rescued and
  4 new ones were dead straight after.
- Reducing the chance of the failure in the first place: add
  `kernelCommandLine=hv_netvsc.ring_size=64` under `[wsl2]` in `.wslconfig`
  (halves the contiguous allocation; the driver enforces 64 as the minimum, the
  default is 128), then `wsl --shutdown`. Partial mitigation, not a cure.
- Tested on one kernel version, x86_64, mirrored networking only.
- Not a replacement for the real fix, which belongs in the driver
  ([docs/how-it-works.md](docs/how-it-works.md#what-a-real-fix-looks-like)).

## Contents

| Path | What |
|---|---|
| `netvsc_unstick.c` | the module |
| `Makefile` | build against `./kernel-src` (or `KSRC=`), version-guarded |
| `scripts/setup-kernel-tree.sh` | fetch and prepare the matching kernel source tree |
| `scripts/check-abi.sh` | verify `struct module` size matches the running kernel |
| `scripts/find_rtnl_holder.py` | identify the `rtnl_mutex` holder on a wedged system |
| `docs/how-it-works.md` | root cause with source references, why each step is needed |
| `docs/maintenance.md` | adapting to new kernel versions, build problems |

## Reporting results

Useful for the upstream issue: kernel version, whether `find_rtnl_holder.py`
showed `GnsEngine` in `netvsc_wait_until_empty`, the dry-run output, whether
recovery worked, and any oops/panic text from `dmesg`.

## License

GPL-2.0, same as the kernel. See `LICENSE`. No warranty; this writes to kernel
memory.
