"""Tests for the decision logic in ./netvsc-unstick. No root, no kernel needed.

    python3 -m unittest discover -s tests -v

The real wedge cannot be produced on demand, so the lock owner, module and
dmesg are faked. The dmesg samples are real lines from a recovery on
6.18.58.1-microsoft-standard-WSL2.
"""
import argparse
import importlib.machinery
import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("nu", os.path.join(ROOT, "netvsc-unstick"))
nu = importlib.util.module_from_spec(importlib.util.spec_from_loader("nu", _loader))
_loader.exec_module(nu)

REAL_DRY = [
    "[995978.478560] netvsc_unstick: DRY eth7 chan[1] napi.state=0x10 chan=ffff8ea52f2be800 in.rb=ffffd3facf62d000 out.rb=ffffd3facf52d000",
    "[995978.478790] netvsc_unstick: DRY eth7 chan[5] napi.state=0x19 chan=ffff8ea52f2b8400 in.rb=0000000000000000 out.rb=0000000000000000",
    "[995978.478853] netvsc_unstick: DRY eth7 chan[6] napi.state=0x19 chan=ffff8ea52f2bc400 in.rb=0000000000000000 out.rb=0000000000000000",
    "[995978.479484] netvsc_unstick: dry run: 2 dead channel(s) found, 0 recovered",
]


def holder(pid=245, state=2, frames=None, comm="GnsEngine"):
    return {"task": 1, "waiters": True, "pid": pid, "state": state, "comm": comm,
            "frames": frames if frames is not None else
            ["schedule", "msleep", "netvsc_wait_until_empty", "netvsc_detach", "netvsc_change_mtu"]}


class FakeKernel:
    def __init__(self, seq):
        self.seq = list(seq)

    def holder(self):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


class Diagnose(unittest.TestCase):
    def d(self, seq):
        with mock.patch.object(nu.time, "sleep"):
            return nu.diagnose(FakeKernel(seq), samples=3)[0]

    def test_unlocked(self):
        self.assertEqual(self.d([None]), "unlocked")

    def test_matches_known_deadlock(self):
        self.assertEqual(self.d([holder()]), "match")

    def test_released_during_sampling_is_transient(self):
        self.assertEqual(self.d([holder(), None, None]), "transient")

    def test_changing_owner_is_transient(self):
        self.assertEqual(self.d([holder(pid=1), holder(pid=2), holder(pid=3)]), "transient")

    def test_other_holder_is_not_ours(self):
        self.assertEqual(self.d([holder(frames=["schedule", "do_something_else"])]), "other")

    def test_running_not_sleeping_is_not_ours(self):
        self.assertEqual(self.d([holder(state=0)]), "other")


class ParseDry(unittest.TestCase):
    def test_real_lines(self):
        with mock.patch.object(nu, "module_cmd", return_value=REAL_DRY):
            dead, healthy = nu.dry_run("eth7")
        self.assertEqual([d[0] for d in dead], [5, 6])
        self.assertTrue(all(d[2] for d in dead))      # rings NULL
        self.assertEqual(healthy, 1)

    def test_sched_alone_is_not_dead(self):
        line = "[1.0] netvsc_unstick: DRY eth0 chan[2] napi.state=0x11 chan=ffff in.rb=ffff1 out.rb=ffff2"
        with mock.patch.object(nu, "module_cmd", return_value=[line]):
            dead, _ = nu.dry_run("eth0")
        self.assertEqual(dead, [])                    # SCHED without NPSVC: healthy poll


class Recover(unittest.TestCase):
    def run_recover(self, kern_seq, dead, yes=True, force=False, tty=False, recovered_dead=()):
        calls = []
        args = argparse.Namespace(iface=None, yes=yes, force=force, load=True)

        def fake_dry(n):
            return (list(dead) if not calls else list(recovered_dead)), 3

        def fake_cmd(c):
            calls.append(c)
            return ["[1.0] netvsc_unstick: eth7 chan[5] is SCHED+NPSVC (disabled forever), recovering",
                    "[1.1] netvsc_unstick: done: 1 dead channel(s) found, 1 recovered"]

        out = io.StringIO()
        with mock.patch.object(nu, "Kernel", lambda: FakeKernel(kern_seq)), \
             mock.patch.object(nu, "netvsc_interfaces", return_value=["eth7"]), \
             mock.patch.object(nu, "ensure_module", return_value=(True, "ok")), \
             mock.patch.object(nu, "dry_run", fake_dry), \
             mock.patch.object(nu, "module_cmd", fake_cmd), \
             mock.patch.object(nu, "failure_summary", return_value=(11, "x sub channel open failed")), \
             mock.patch.object(nu.time, "sleep"), \
             mock.patch.object(nu.sys.stdin, "isatty", return_value=tty), \
             redirect_stdout(out):
            rc = nu.cmd_recover(args)
        return rc, calls, out.getvalue()

    DEAD = [(5, 0x19, True), (6, 0x19, True)]

    def test_wedged_then_released(self):
        rc, calls, out = self.run_recover([holder(), holder(), holder(), None], self.DEAD)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["eth7"])
        self.assertIn("rtnl_lock released", out)

    def test_leftover_dead_channels_are_reported(self):
        rc, _, out = self.run_recover([holder(), holder(), holder(), None], self.DEAD,
                                      recovered_dead=[(6, 0x19, True)])
        self.assertEqual(rc, 0)
        self.assertIn("1 dead channel(s) remain", out)

    def test_not_released_returns_3(self):
        with mock.patch.object(nu, "RELEASE_WAIT", 0):
            rc, _, out = self.run_recover([holder()], self.DEAD)   # lock never released
        self.assertEqual(rc, 3)
        self.assertIn("STILL HELD", out)

    def test_refuses_other_holder(self):
        rc, calls, _ = self.run_recover([holder(frames=["x"])], self.DEAD)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_healthy_does_nothing_without_force(self):
        rc, calls, out = self.run_recover([None], self.DEAD)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertIn("Nothing to recover", out)

    def test_refuses_non_null_rings_when_wedged(self):
        rc, calls, out = self.run_recover([holder()], [(5, 0x19, False)])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_needs_yes_without_a_terminal(self):
        rc, calls, _ = self.run_recover([holder()], self.DEAD, yes=False, tty=False)
        self.assertEqual(rc, 4)
        self.assertEqual(calls, [])

    def test_prompt_decline(self):
        with mock.patch("builtins.input", return_value="n"):
            rc, calls, _ = self.run_recover([holder()], self.DEAD, yes=False, tty=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    def test_no_dead_channels_while_wedged_refuses(self):
        rc, calls, _ = self.run_recover([holder()], [])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
