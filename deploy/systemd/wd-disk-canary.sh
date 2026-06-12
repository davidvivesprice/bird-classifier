#!/bin/sh
# Hardware-watchdog disk test: O_DIRECT+sync write to the NVMe rootfs.
# Bypasses page cache — succeeds only if the disk truly accepts writes.
exec timeout 30 dd if=/dev/zero of=/home/vives/.wd-canary bs=512 count=1 oflag=direct,sync conv=notrunc 2>/dev/null
