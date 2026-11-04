# External Kbuild entry point; CONFIG_DRM_ADI_AXI_HDMI is intentionally n.
obj-m += adi_axi_hdmi_present.o
adi_axi_hdmi_present-y := axi_hdmi_encoder.o axi_hdmi_crtc.o axi_hdmi_drv.o
ccflags-y += -I$(src)/include/uapi -I$(srctree)/include/uapi/drm
