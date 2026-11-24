#!/usr/bin/env python3
"""Identify who holds rtnl_mutex on a wedged WSL2 kernel, read-only.

Run as root inside the distro:  sudo python3 find_rtnl_holder.py

Why this exists: the holder (GnsEngine) lives in the WSL utility VM's root PID
namespace, so ps and /proc/<pid>/stack inside the distro cannot see it. This
reads struct mutex.owner from /proc/kcore, decodes the task_struct, and
heuristically unwinds its kernel stack. No netlink, no module, no locks, so it
works while rtnl_lock is wedged. Needs: /proc/kcore, root-readable
/proc/kallsyms, `pahole` (dwarves) and BTF at /sys/kernel/btf/vmlinux.

Found 2026-11-25 (kernel 6.18.58.1): owner GnsEngine pid 245, state 0x2,
stack msleep <- netvsc_wait_until_empty <- netvsc_detach <- netvsc_change_mtu
<- do_setlink <- rtnl_newlink. See ../README.md and the investigation log.
"""
import bisect
import re
import struct
import subprocess
import sys


def pahole_offsets(struct_name, fields):
    out = subprocess.run(
        ["pahole", "-C", struct_name, "/sys/kernel/btf/vmlinux"],
        capture_output=True, text=True, check=True).stdout
    res = {}
    for f in fields:
        m = re.search(r"\b%s(?:\[\d+\])?;\s*/\*\s*(\d+)" % re.escape(f), out)
        if not m:
            sys.exit("could not find %s.%s in pahole output" % (struct_name, f))
        res[f] = int(m.group(1))
    return res


def main():
    syms = []
    rtnl = None
    for line in open("/proc/kallsyms"):
        p = line.split()
        a = int(p[0], 16)
        if a == 0:
            sys.exit("kallsyms addresses are zero: run as root")
        if p[2] == "rtnl_mutex":
            rtnl = a
        if p[1] in "tTwW":
            syms.append((a, p[2]))
    if rtnl is None:
        sys.exit("rtnl_mutex not found in kallsyms")
    syms.sort()
    addrs = [s[0] for s in syms]

    ts = pahole_offsets("task_struct", ["__state", "stack", "pid", "comm", "thread"])
    sp_off = ts["thread"] + pahole_offsets("thread_struct", ["sp"])["sp"]

    f = open("/proc/kcore", "rb")
    hdr = f.read(64)
    phoff = struct.unpack_from("<Q", hdr, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", hdr, 54)
    segs = []
    for i in range(phnum):
        f.seek(phoff + i * phentsize)
        t, _, off, va, _, fs, _, _ = struct.unpack("<IIQQQQQQ", f.read(phentsize)[:56])
        if t == 1:
            segs.append((va, fs, off))

    def rd(addr, n):
        for va, fs, off in segs:
            if va <= addr < va + fs:
                f.seek(off + addr - va)
                return f.read(n)
        raise SystemExit("unmapped %#x" % addr)

    def u64(a):
        return struct.unpack("<Q", rd(a, 8))[0]

    def sym(a):
        i = bisect.bisect_right(addrs, a) - 1
        if i < 0 or a - syms[i][0] >= 0x2000:
            return None
        return syms[i][1], a - syms[i][0]

    owner = u64(rtnl)
    print("rtnl_mutex @ %#x  owner raw = %#x" % (rtnl, owner))
    print("waiters present: %s" % bool(owner & 1))
    task = owner & ~7
    if not task:
        print("mutex is UNLOCKED (no owner). The wedge is not rtnl_mutex.")
        return
    pid = struct.unpack("<I", rd(task + ts["pid"], 4))[0]
    state = struct.unpack("<I", rd(task + ts["__state"], 4))[0]
    comm = rd(task + ts["comm"], 16).split(b"\0")[0].decode(errors="replace")
    print("owner: pid=%d comm=%s __state=%#x (0x2 = uninterruptible)" % (pid, comm, state))
    sp = u64(task + sp_off)
    print("--- kernel stack words that look like return addresses (most recent first;")
    print("    stale words further down are noise) ---")
    for i in range(0, 512):
        w = u64(sp + i * 8)
        if 0xFFFFFFFF81000000 <= w < 0xFFFFFFFF84000000:
            s = sym(w)
            if s and s[0] != "srso_return_thunk":
                print("%4d  %s+%#x" % (i * 8, s[0], s[1]))


if __name__ == "__main__":
    main()
