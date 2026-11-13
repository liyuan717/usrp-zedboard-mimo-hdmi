# 构建、运行与归档范围

## FPGA

R22 采用 Vivado 2025.2，目标器件为 XC7Z020-CLG484-1。实际综合输入位于 [fpga/r22](../fpga/r22/)，包括 R22 的 PL 源、约束、6 个 ROM 和 ADI IP 源码。该目录与通用开发树 `pl/`、仿真树 `sim/` 并存。用归档的 R22 入口可选择实测配置的输入和约束。

在仓库根目录执行以下命令。输出目录必须尚未创建。综合示例使用 1 线程，R22 实现记录使用 5 线程。需要 Vivado 及本地磁盘、内存余量。

```bash
repo="$PWD"
out="$repo/build/r22"
python3 tools/prepare_r22_archive.py --output "$out"
cd "$out"
vivado -mode batch -source build.tcl -tclargs 1
vivado -mode batch -source area-and-route.tcl -tclargs "$out" 5 0
```

检查时序、DRC、CDC 和 I/O 报告后，可单独生成位流。

```bash
vivado -mode batch -source "$repo/pl/g0/emit_g0_source_bit.tcl" -tclargs "$out" 2
```

归档脚本核验输入，并把路径占位符解析到选定输出目录。原始文件及发布文件的散列均在清单中。实测位流 CRC32 为 `f35c9d9a`，本次发布未重新综合或布线。 [FPGA 输入清单](../fpga/r22/provenance.json) · [实测位流身份](../artifacts/r22-identities.json)

## 主机与接收端软件

- PC 源码包括 V4L2 摄像头时钟采集、FFmpeg H.264 编码与有限队列、UHD 双路输出。关键入口为 [实时摄像头管线](../tools/g0_camera_live_stream.py)、[无线测试协调器](../tools/run_g0_video_air.py)、[UHD 主机程序](../pc/g0_uhd_probe.cpp)。运行需要匹配的 UHD/USRP 驱动、FFmpeg、V4L2 摄像头以及双路射频硬件。
- Cortex-A9 接收源位于 [ps/](../ps/)，包括 SOURCE DMA、分片重组、FFmpeg 解码及 HDMI 呈现。 [交叉构建脚本](../tools/build_g0_stream_decode.py) 需要已有的参考工具链和构建收据，以及 FFmpeg 提交 `b1a4534186ca51b0457579fc05a5739eb2cc45cd` 对应的 ARM 库。内核模块需要与板端的内核 ABI 匹配。
- SD 启动需要配套的 FSBL/U-Boot、内核、设备树、initramfs 与位流。 [启动包脚本](../tools/stage_g0_receive_boot.py) 依赖原 R9 基线和匹配的 R22 硬件交付文件。 [镜像脚本](../tools/stage_g0_receive_sd_image.py) 负责从配套启动包生成普通镜像文件。 [启动文件散列](../artifacts/r22-identities.json) 用于标识已经实测的原 R22 SD。本分支为源码、位流和证据归档，完整启动包的外部依赖需单独取得。

## 两套已测接收程序

仓库的 `ps/` 与 `vendor/` 主树提供最终 RAW 程序。另有原 SD 版解码器的 11 个差异源码。核对原 SD 版时，在副本上以相对路径叠加这 11 个文件，并按原 SD 身份清单验证解码器输入。其余输入与主树相同。两份清单也记录了实测二进制的 SHA-256。 [最终 RAW 解码器](../artifacts/raw-ram-decoder-identity.json) · [原 SD 解码器](../artifacts/sd-offline-decoder-identity.json) · [原 SD 源码差异](../artifacts/sd-offline-source-overlay/)

## 来源与许可

主项目源码沿用 MIT 许可。 [项目许可](../LICENSE)

R22 所用 ADI HDL 及 Vivado IP 描述保留各文件头与 ADI 许可。自有 HDL、板端代码复用关系记录在 `vendor/` 的来源清单中。内核相关源码与拓扑脚本依其文件声明适用 GPL-2.0、Apache-2.0 或其他随附许可。 [ADI 许可](../fpga/r22/sources/vendor/LICENSE) · [复用来源](../vendor/ORIGIN.json) · [内核来源](../vendor/hdmi_kernel/ORIGIN.json) · [拓扑来源](../vendor/r9_io/ORIGIN.json) · [许可文本](../licenses/)

发布快照源于本地提交 `8517a7b3006f3cba763e669401a59e2085375e2e`。归档整理了工作站路径、引入自包含的启动包解析辅助脚本，并将磁盘检查改为只读。保留的协议文件是施工规划的历史快照，当前归档状态见首页及验收记录。 [协议快照](protocol/) · [验收记录](validation.md)
