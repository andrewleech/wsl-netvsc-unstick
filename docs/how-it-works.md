# How it works

All source references are to the WSL2 kernel tag
[`linux-msft-wsl-6.18.58.1`](https://github.com/microsoft/WSL2-Linux-Kernel/tree/linux-msft-wsl-6.18.58.1).
Line numbers drift between versions; search for the function names.

## The failure chain

1. **A sub-channel open fails.** When a netvsc device gets several queues,
   each sub-channel is opened by `netvsc_sc_open()`
   (`drivers/net/hyperv/rndis_filter.c`), which calls `vmbus_open()`, which
   calls `vmbus_alloc_ring()` (`drivers/hv/channel.c`):

   ```c
   order = get_order(send_size + recv_size);
   page = alloc_pages_node(..., GFP_KERNEL|__GFP_ZERO, order);
   if (!page) page = alloc_pages(GFP_KERNEL|__GFP_ZERO, order);
   if (!page) return -ENOMEM;
   ```

   With the default ring size that is one physically contiguous **1MB (order 8)**
   block. After days of uptime with heavy page cache the allocator has none
   (`/proc/buddyinfo` shows zeros from order 8 up) and it fails:
   `page allocation failure: order:8`, then `sub channel open failed: -12`.
   Nothing retries with smaller pieces; `vmap()` in the caller would not care if
   the pages were non-contiguous.

2. **The failure path leaves the channel half-alive.** Before calling
   `vmbus_open()`, `netvsc_sc_open()` has already set `nvchan->channel = new_sc`
   and called `napi_enable()` (deliberately, "to avoid races"). On failure it
   calls `napi_disable()` and nothing else: no `netif_napi_del()`, `channel`
   still set. `napi_disable()` leaves `NAPI_STATE_SCHED | NAPI_STATE_NPSVC` set
   (that is how it keeps NAPI from running). And because `vmbus_open()` failed
   before `hv_ringbuffer_init()`, `chn->inbound.ring_buffer` and
   `chn->outbound.ring_buffer` are **NULL**.

3. **Something later walks every channel under `rtnl_lock`.**
   `netvsc_wait_until_empty()` (`netvsc_drv.c`), called from `netvsc_detach()` /
   `netvsc_close()`:

   ```c
   struct vmbus_channel *chn = nvdev->chan_table[i].channel;   /* cached local */
   if (!chn) continue;
   napi_synchronize(&nvdev->chan_table[i].napi);               /* spins here */
   aread = hv_get_bytes_to_read(&chn->inbound);                /* would crash here */
   ```

   and `napi_synchronize()` (`include/linux/netdevice.h`) is:

   ```c
   while (test_bit(NAPI_STATE_SCHED, &n->state))
       msleep(1);
   ```

   Unbounded, no timeout, not interruptible. On a dead channel `SCHED` never
   clears, so it spins forever.

4. **WSL's `GnsEngine` triggers it.** It pushes the Windows-side network config
   into the guest over netlink, including MTU changes. That is
   `rtnl_newlink -> do_setlink -> netvsc_change_mtu -> netvsc_detach ->
   netvsc_wait_until_empty`, all under `rtnl_lock`, so the lock is held by a
   thread that will never finish. Anything else that needs `rtnl_lock` or a
   netlink dump queues behind it: `ip`, `ss`, `sshd`, `ping`, and any program
   calling `getaddrinfo()` (glibc's default `AI_ADDRCONFIG` does an
   `RTM_GETADDR` dump first). They sit in `D` state and ignore `SIGKILL`.

`GnsEngine` runs in the utility VM's root PID namespace, so it is invisible from
inside the distro: `ps` shows only the victims, which is why this looks like an
"orphaned lock" with no owner. `scripts/find_rtnl_holder.py` finds it by reading
`rtnl_mutex.owner` from `/proc/kcore`.

## What the module does, and why each part is there

For each sub-channel (index >= 1) of the chosen `hv_netvsc` device whose
`napi.state` has both `SCHED` and `NPSVC` set:

1. **If `ring_buffer` is NULL, point it at a zeroed page** for both
   `chn->inbound` and `chn->outbound`.
2. **Then `clear_bit(NAPI_STATE_SCHED)`.**

Order matters: step 2 releases the spinning thread within ~1ms, and the very
next thing it does is `hv_get_bytes_to_read(&chn->inbound)`, which does:

```c
read_loc  = rbi->ring_buffer->read_index;     /* offset 4 in struct hv_ring_buffer */
write_loc = READ_ONCE(rbi->ring_buffer->write_index);
```

With `ring_buffer == NULL` that is a NULL dereference at address `0x4`; the first
attempt at this module (bit-clear only) panicked the VM exactly there:

```
CR2: 0000000000000004
netvsc_close+0x109/0x160 ... dev_ifsioc
```

With a zeroed page, `read_index == write_index == 0`, so it computes "0 bytes
pending" for both rings, `netvsc_wait_until_empty()` returns 0, `netvsc_detach()`
carries on, and `rtnl_lock` is released.

Things that look like they should work but do not:

- **Setting `nvchan->channel = NULL`** would make the loop `continue` past the
  channel, but the stuck thread already copied `chn` into a local before it
  started spinning. Changing the field now does nothing for that thread.
- **Killing `GnsEngine`**: it is in uninterruptible sleep; signals are not
  delivered until it leaves the kernel.
- **Taking `rtnl_lock`** from the module to "do it properly": that is the lock
  that is stuck.

### Why not just clear `SCHED` on anything that has it set?

`NAPI_STATE_SCHED` is also set, briefly, by every *healthy* channel while it
polls. Clearing it under a live poll lets `napi_schedule_prep()` succeed again
on an already-queued `napi_struct` and corrupts the per-CPU poll list. That is a
crash, not a no-op. `NAPI_STATE_NPSVC` is set only by `napi_disable()` and
cleared only by `napi_enable()`, so `SCHED && NPSVC` identifies exactly "disabled,
never re-enabled" channels. In the one real run, healthy channels read
`napi.state=0x10` (`LISTED`) and dead ones `0x19` (`SCHED|NPSVC|LISTED`).

### Why it is reasonably safe to touch these fields from outside the driver

The only reason this is needed is that `rtnl_lock` is held forever. While it is,
every other thread that would modify netdev state (device add/remove, queue
reconfiguration, MTU change, unbind) is parked waiting for it, so nothing else
mutates this netdev concurrently. The module walks netdevs under
`rcu_read_lock()` only, scopes to one interface, clamps to `VRSS_CHANNEL_MAX`,
only replaces a ring buffer that is NULL, and only touches devices bound to
`hv_netvsc`. What remains unsynchronised is NAPI scheduling itself, hence the
`SCHED && NPSVC` gate above.

This reasoning has had one independent review (of the bit-clear gating) and one
real-world run. It is not formally proven.

## After recovery

The released thread finishes `netvsc_detach()` (tearing down the channels,
including the dead ones, which is where the per-channel `WARN_ON` comes from:
`__netif_napi_del_locked` asserts `SCHED` is set and we cleared it), then
`netvsc_attach()` rebuilds the device and opens fresh sub-channels. If memory is
still fragmented, some of those fail the same way. Check with
`echo "<ifname> dry"` and recover again.

## What a real fix looks like

The problem is in the driver, and the proper fix removes the need for this
module:

1. **`vmbus_alloc_ring()`**: on order-N failure, fall back to allocating
   `1 << order` single pages and let the existing `vmap()` in
   `hv_ringbuffer_init()` make them contiguous. (Check that nothing downstream,
   such as GPADL setup, assumes physical contiguity.) This removes the trigger.
2. **`netvsc_sc_open()` failure path**: clear `nvchan->channel`, call
   `netif_napi_del()` (and make sure `SCHED` cannot be observed set) at the point
   of failure, so `netvsc_wait_until_empty()` never sees a half-dead channel.
   Microsoft's 6.18.58.1 tree already adds a `netvsc_napi_disable()` wrapper and
   rescind handling here but the failure path still leaves the channel in the
   state described above.
3. **`napi_synchronize()` callers** could bail out for channels that never
   opened.

## Findings that may help a maintainer

- The lock holder is `GnsEngine` (root PID namespace); stack above.
- Healthy vs dead channel signature: `napi.state` `0x10` vs `0x19`, rings NULL.
- Dead-channel count equals the number of `sub channel open failed: -12` lines.
- `order:7` failures can appear a day earlier with no wedge; only an `order:8`
  failure coincided with the hang.
- `hv_netvsc.ring_size` has an enforced minimum of 64 pages (default 128), so the
  command-line workaround only lowers the order by one.
