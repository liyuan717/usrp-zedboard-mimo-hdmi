# Receiver-only candidate. BOOT.BIN must load the matching R17 PL image.
setenv kernel_addr_r 0x02000000
setenv ramdisk_addr_r 0x08000000
setenv fdt_addr_r 0x10000000
setenv bootm_low 0x00000000
setenv bootm_size 0x10000000
setenv bootargs 'console=ttyPS0,115200 rdinit=/init cma=64M'

if fatload mmc 0:1 ${kernel_addr_r} zImage && fatload mmc 0:1 ${fdt_addr_r} hdmi.dtb; then
    if fatload mmc 0:1 ${ramdisk_addr_r} v3-receive-initramfs.cpio.gz; then
        setenv ramdisk_size ${filesize}
        bootz ${kernel_addr_r} ${ramdisk_addr_r}:${ramdisk_size} ${fdt_addr_r}
    fi
fi
echo 'V3 receiver boot failed'
false
