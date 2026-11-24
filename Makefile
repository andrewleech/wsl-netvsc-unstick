# netvsc_unstick - out-of-tree module for WSL2 (hv_netvsc deadlock recovery)
#
# This module uses PRIVATE kernel structures from drivers/net/hyperv/, so it
# must be built against the full kernel SOURCE TREE that matches the running
# kernel (not just a headers package; WSL2 does not ship one that works).
# Prepare that tree once with:   scripts/setup-kernel-tree.sh
#
#   make                 build against ./kernel-src
#   make KSRC=/path      build against another prepared tree
#   make check           verify struct module ABI matches the running kernel
#   make clean

KSRC ?= $(CURDIR)/kernel-src

obj-m += netvsc_unstick.o
ccflags-y += -I$(KSRC)/drivers/net/hyperv

# Without a full vmlinux build there is no Module.symvers, so modpost reports
# every external symbol as undefined. That is harmless here (the running
# kernel resolves real symbols at insmod time) so downgrade to warnings.
export KBUILD_MODPOST_WARN := 1

.PHONY: all check clean versioncheck

all: versioncheck
	$(MAKE) -C $(KSRC) M=$(CURDIR) modules

versioncheck:
	@if [ ! -f "$(KSRC)/include/generated/utsrelease.h" ]; then \
	  echo "error: $(KSRC) is not a prepared kernel tree."; \
	  echo "       run scripts/setup-kernel-tree.sh first (see README)."; exit 1; fi
	@tree=$$(sed -n 's/.*UTS_RELEASE "\(.*\)"/\1/p' $(KSRC)/include/generated/utsrelease.h); \
	 run=$$(uname -r); \
	 if [ "$$tree" != "$$run" ]; then \
	  echo "error: kernel tree is $$tree but running kernel is $$run."; \
	  echo "       The module would not load, and private struct layouts differ"; \
	  echo "       between versions. Re-run scripts/setup-kernel-tree.sh."; exit 1; fi

check:
	scripts/check-abi.sh

clean:
	-$(MAKE) -C $(KSRC) M=$(CURDIR) clean
