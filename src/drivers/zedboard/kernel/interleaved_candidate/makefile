# Dedicated external-module entrypoint for the IMBC candidate only.
# It prevents a legacy detector module build/package from accidentally carrying
# this experimental driver.  The canonical implementation remains one level
# above so source review is shared with the kernel UAPI.
obj-m += zedboard_interleaved_candidate.o
ccflags-y += -I$(src)/..
