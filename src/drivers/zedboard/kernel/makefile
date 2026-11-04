# Out-of-tree ZedBoard Detector DMA owner.
# Invoke through scripts/zedboard/build_detector_module.py so KDIR and
# CROSS_COMPILE are explicit rather than inherited from a host shell.
obj-m += zedboard_detector.o
obj-m += zedboard_interleaved_candidate.o
obj-m += zedboard_rx_transport.o
ccflags-y += -I$(src)
