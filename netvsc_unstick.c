// SPDX-License-Identifier: GPL-2.0
/*
 * netvsc_unstick - live recovery for the hv_netvsc sub-channel-open-failure
 * deadlock on WSL2 (https://github.com/microsoft/WSL/issues/41622).
 *
 * THE BUG (see docs/how-it-works.md for the full chain with source lines)
 *
 *   1. vmbus_alloc_ring() fails to get a contiguous order-8 block while a
 *      netvsc sub-channel is being opened ("sub channel open failed: -12").
 *   2. netvsc_sc_open() has already done napi_enable() and set
 *      nvchan->channel, and its failure path calls napi_disable(), which
 *      leaves NAPI_STATE_SCHED | NAPI_STATE_NPSVC set forever. The channel's
 *      ring buffers were never initialised (ring_buffer == NULL).
 *   3. The next MTU change / interface-down (WSL's GnsEngine sends one) runs
 *      netvsc_wait_until_empty() under rtnl_lock, which calls
 *      napi_synchronize() on every channel. napi_synchronize() is an
 *      unbounded `while (test_bit(NAPI_STATE_SCHED, ...)) msleep(1);`, so it
 *      spins on the dead channel forever, holding rtnl_lock. Every netlink
 *      user on the box (ip, getaddrinfo(), sshd, ...) then hangs.
 *
 * WHAT THIS MODULE DOES
 *
 * It only acts on channels in the exact "disabled forever" state
 * (SCHED && NPSVC set), and for each one:
 *
 *   a. If the channel's inbound/outbound ring_buffer is NULL, point it at a
 *      zeroed page. This is REQUIRED: the spinning thread, once released,
 *      immediately calls hv_get_bytes_to_read(&chn->inbound), which reads
 *      rbi->ring_buffer->read_index. With a NULL ring_buffer that is a NULL
 *      dereference at offset 4 and the kernel panics (observed). A zeroed
 *      page makes read_index == write_index == 0, i.e. "0 bytes pending", so
 *      netvsc_wait_until_empty() returns 0 and the detach completes.
 *      (Setting nvchan->channel = NULL does NOT help: the stuck thread
 *      already copied the pointer into a local before it started spinning.)
 *   b. Then clear NAPI_STATE_SCHED, releasing the spinning thread.
 *
 * It never takes rtnl_lock (that is the lock that is stuck). It walks
 * netdevs under rcu_read_lock() only. Because rtnl_lock is wedged, no other
 * thread can be mutating netdev state concurrently, which is what makes
 * touching these fields from outside the driver reasonably safe.
 *
 * SAFETY GATES
 *
 *   - SCHED alone is NOT a safe match: a healthy channel has SCHED set for a
 *     moment during every poll, and clearing it under a live poll can
 *     corrupt the per-CPU poll list. SCHED && NPSVC is only ever true on a
 *     channel that napi_disable() was called on and napi_enable() was not.
 *   - Only sub-channels (index >= 1) are considered.
 *   - Loop bound is clamped to VRSS_CHANNEL_MAX.
 *   - ring_buffer is only replaced if it is currently NULL.
 *   - Only devices bound to the "hv_netvsc" driver are touched.
 *   - Scope to one interface by name.
 *
 * KNOWN SIDE EFFECTS
 *
 *   - Taints the kernel (out-of-tree module).
 *   - One WARN_ON in __netif_napi_del_locked() per rescued channel when the
 *     detach tears the device down (it expects SCHED set; we cleared it).
 *   - Two zeroed pages leaked per rescued channel (deliberate: proving it is
 *     safe to free them is harder than leaking 8KB).
 *   - This RECOVERS from the wedge. It does not fix the allocation failure;
 *     the re-attach that follows can fail the same way and leave new dead
 *     channels. Run `echo <ifname> dry` to check.
 *
 * USAGE (as root; debugfs must be mounted)
 *
 *   echo "eth7 dry" > /sys/kernel/debug/netvsc_unstick/fix   # report only
 *   echo eth7       > /sys/kernel/debug/netvsc_unstick/fix   # recover
 *   echo 1          > /sys/kernel/debug/netvsc_unstick/fix   # all hv_netvsc
 *
 * This file depends on private kernel structures (hyperv_net.h) and MUST be
 * built against the source tree matching the running kernel. See
 * docs/maintenance.md.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/debugfs.h>
#include <linux/device.h>
#include <linux/netdevice.h>
#include <linux/rcupdate.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/minmax.h>
#include <linux/if.h>
#include <linux/bits.h>
#include <linux/gfp.h>
#include <linux/mm.h>
#include <linux/string.h>

/* Private driver header, found via -I$(KSRC)/drivers/net/hyperv */
#include "hyperv_net.h"

#define NETVSC_DRIVER_NAME	"hv_netvsc"

static struct dentry *unstick_dir;

/*
 * Give an uninitialised ring a fake, permanently-empty state so that
 * hv_get_bytes_to_read()/hv_get_bytes_to_write() do not dereference NULL.
 * Never touches a ring that already has a buffer.
 */
static void fake_empty_ring(struct hv_ring_buffer_info *rbi)
{
	unsigned long page;

	if (rbi->ring_buffer)
		return;

	/* GFP_ATOMIC: we are inside rcu_read_lock(). */
	page = get_zeroed_page(GFP_ATOMIC);
	if (!page) {
		pr_err("netvsc_unstick: failed to allocate fake ring page\n");
		return;
	}

	/* struct hv_ring_buffer begins with write_index, read_index (u32s);
	 * a zeroed page is already read_loc == write_loc == 0 == "empty".
	 */
	rbi->ring_buffer = (struct hv_ring_buffer *)page;
	rbi->ring_datasize = PAGE_SIZE;
}

static void unstick_one_device(struct net_device *dev, unsigned int *found,
			       unsigned int *cleared, bool dry)
{
	const unsigned long stuck_mask =
		BIT(NAPI_STATE_SCHED) | BIT(NAPI_STATE_NPSVC);
	struct net_device_context *ndc;
	struct netvsc_device *nvdev;
	u32 nchan;
	u32 i;

	if (!dev->dev.parent)
		return;
	if (strcmp(dev_driver_string(dev->dev.parent), NETVSC_DRIVER_NAME) != 0)
		return;

	ndc = netdev_priv(dev);
	nvdev = rcu_dereference(ndc->nvdev);
	if (!nvdev)
		return;

	nchan = min_t(u32, nvdev->num_chn, VRSS_CHANNEL_MAX);

	/* Start at 1: only sub-channels go through the failing open path. */
	for (i = 1; i < nchan; i++) {
		struct netvsc_channel *nvchan = &nvdev->chan_table[i];
		struct vmbus_channel *chn = nvchan->channel;

		if (dry)
			pr_info("netvsc_unstick: DRY %s chan[%u] napi.state=%#lx chan=%px in.rb=%px out.rb=%px\n",
				dev->name, i, nvchan->napi.state, chn,
				chn ? chn->inbound.ring_buffer : NULL,
				chn ? chn->outbound.ring_buffer : NULL);

		if ((nvchan->napi.state & stuck_mask) != stuck_mask)
			continue;

		(*found)++;
		if (dry)
			continue;

		pr_info("netvsc_unstick: %s chan[%u] is SCHED+NPSVC (disabled forever), recovering\n",
			dev->name, i);

		if (chn) {
			fake_empty_ring(&chn->inbound);
			fake_empty_ring(&chn->outbound);
		}

		/* Must be last: this releases the thread spinning in
		 * napi_synchronize(), which immediately reads the rings
		 * prepared above.
		 */
		clear_bit(NAPI_STATE_SCHED, &nvchan->napi.state);
		(*cleared)++;
	}
}

static ssize_t fix_write(struct file *file, const char __user *buf,
			 size_t count, loff_t *ppos)
{
	char cmd[IFNAMSIZ + 8];
	size_t len = min(count, sizeof(cmd) - 1);
	unsigned int found = 0, cleared = 0;
	bool dry = false;
	char *sp;

	if (copy_from_user(cmd, buf, len))
		return -EFAULT;
	cmd[len] = '\0';
	if (len > 0 && cmd[len - 1] == '\n')
		cmd[len - 1] = '\0';

	/* "<ifname> dry": report only, modify nothing */
	sp = strchr(cmd, ' ');
	if (sp) {
		*sp++ = '\0';
		dry = !strcmp(sp, "dry");
	}
	if (strlen(cmd) >= IFNAMSIZ)
		return -EINVAL;

	rcu_read_lock();
	if (cmd[0] != '\0' && strcmp(cmd, "1") != 0) {
		struct net_device *dev = dev_get_by_name_rcu(&init_net, cmd);

		if (!dev) {
			rcu_read_unlock();
			pr_info("netvsc_unstick: interface '%s' not found\n", cmd);
			return -ENODEV;
		}
		unstick_one_device(dev, &found, &cleared, dry);
	} else {
		struct net_device *dev;

		for_each_netdev_rcu(&init_net, dev)
			unstick_one_device(dev, &found, &cleared, dry);
	}
	rcu_read_unlock();

	pr_info("netvsc_unstick: %s: %u dead channel(s) found, %u recovered\n",
		dry ? "dry run" : "done", found, cleared);

	return count;
}

static const struct file_operations fix_fops = {
	.write = fix_write,
	.open = simple_open,
	.llseek = default_llseek,
};

static int __init netvsc_unstick_init(void)
{
	unstick_dir = debugfs_create_dir("netvsc_unstick", NULL);
	if (IS_ERR(unstick_dir))
		return PTR_ERR(unstick_dir);

	debugfs_create_file("fix", 0200, unstick_dir, NULL, &fix_fops);

	pr_info("netvsc_unstick: loaded. Inspect: echo \"<ifname> dry\" > /sys/kernel/debug/netvsc_unstick/fix ; recover: echo <ifname> > ...\n");
	return 0;
}

static void __exit netvsc_unstick_exit(void)
{
	debugfs_remove_recursive(unstick_dir);
	pr_info("netvsc_unstick: unloaded\n");
}

module_init(netvsc_unstick_init);
module_exit(netvsc_unstick_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Live recovery for the hv_netvsc sub-channel open failure / rtnl_lock deadlock (microsoft/WSL#41622)");
MODULE_AUTHOR("Andrew Leech");
