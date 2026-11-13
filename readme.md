# USRP–ZedBoard 2×2 MIMO 实时视频 · ML 最终版

本分支归档 R22 实时视频链路。X310 从摄像头实时采集并流水发送 H.264，ZedBoard＋FMCOMMS3 接收并持续输出 HDMI。FPGA 使用最大似然检测（Maximum Likelihood，ML），对两路 QPSK 的 16 个联合候选计算平方残差并取最小值。接收端采用固定 8-bank FEC，保留 CRC 擦除、有界重组和周期恢复点。项目以 MHGD 和 ML 两版结项。

| 最终版本 | 数据路径与算法 | 结项时的完成度 |
| --- | --- | --- |
| [MHGD · main](https://github.com/liyuan717/usrp-zedboard-mimo-hdmi/tree/main) | 网页发送照片或完整 MP4，接收端恢复原文件后通过 HDMI 播放，PL 使用 MHGD 检测 | 已验收的比赛交付。两份 MP4 逐字节恢复，分别记录 25 个物理 HDMI 呈现帧 |
| ML · ml-final | 单向 2×2 MIMO，实时摄像头编码、无线传输与 HDMI 呈现并行，PL 使用联合 ML 检测 | 工程最终归档。R22 的短距实时视频、条件帧龄和原 SD 脱机功能各有实测，统一配置的整体验收未完成 |

## R22 已测结果

- **短距实时，2026-11-07，RAM 运行版。** 1024×600，60 秒交付 1,200/1,200 个物理新帧。真实采集 20.0651 fps，HDMI 物理新帧 20.0969 fps。该轮缺帧、参考像素 CRC 差异和超过 1 秒的冻结均为零，最长新帧间隔 217.313 ms。按该轮校准模型，采集到物理呈现的无线和独立有线条件帧龄上界分别为 0.808 秒、0.797 秒。 [短距实测收据](docs/evidence/g0-R22-live-RAW-timebase-20261107.json)
- **G7 脱机，2026-11-06，原 R22 SD 运行版。** 实卡写入读回通过。断电冷启动并拔除接收端开发线后，用户确认迟发时能出图，停发至少约 11.81 秒后恢复发射，接收端无需重启即可自动出图。此项依据用户观察和主机发射日志。 [脱机功能收据](docs/evidence/g0-R22-G7-detached-operator-acceptance-20261106.json)
- **有界恢复。** 丢包和 CRC 坏包转入有界处理，过期内容可丢弃，缺帧、受损帧和冻结各自计数。早期 R22 短距轮交付 1,181/1,200 个新帧，缺 19 帧，19.78 fps，138 帧参考像素 CRC 差异。RAW 积压试验解码 200 帧，主动抑制 124 帧，实际显示 76 帧。 [早期长测](docs/evidence/g0-R22-short-camera1200-20261106.json) · [RAW 积压收据](docs/evidence/g0-R22-live-RAW-timebase-20261107.json)

两套软件使用同一 R22 位流。原 SD 运行版的脱机结果适用于已测的原卡镜像。最终 RAW 程序在 RAM 中验证。约 10 米、最终 RAW 脱机、统一配置的完整 G0/G6、整板 I/O 与 CDC 签核及 CPU/RAM/DDR 和完整资源预算尚未完成。阶段二的 RF 反馈与 TDD 未纳入本版。

## 获取与核验

```bash
git clone --branch ml-final --single-branch https://github.com/liyuan717/usrp-zedboard-mimo-hdmi.git
cd usrp-zedboard-mimo-hdmi
python3 tools/prepare_r22_archive.py
python3 -m unittest discover -s tests -p 'test_offline_boundaries.py'
```

- PL 检测与实际构建输入、PC 采集与发射、ARM 解码与 HDMI、板端启动脚本均随源码归档。 [构建与运行边界](docs/build.md) · [协议](docs/protocol/RF_PROTOCOL.md) · [架构](docs/protocol/ARCHITECTURE.md)
- 固定 R22 接收位流、原 SD 镜像与五个启动文件身份可核对。归档提供位流和清单。SD 镜像、根文件系统及原始采集数据留在原工程。 [R22 位流](artifacts/r22-receiver.bit) · [SHA-256 与镜像身份](artifacts/r22-identities.json)
- 实现实测 47,118 LUT、44,183 FF、74 BRAM36 等效、150 DSP，WNS +0.115 ns、WHS +0.023 ns。 [实现报告](fpga/r22/reports/implementation_utilization.rpt) · [预算与证据](docs/validation.md)
