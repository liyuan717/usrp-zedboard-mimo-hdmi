# R22 验收与资源

| 项目 | 结项记录 |
| --- | --- |
| 模式 | 单向 2×2 MIMO，1024×600，固定 8-bank，目标真实新源帧 20 fps、约 3 秒帧龄 |
| 短距 60 秒 | RAW 运行版交付 1,200/1,200 个物理新帧，真实采集 20.0651 fps，物理显示 20.0969 fps，零像素 CRC 差异，零超过 1 秒冻结 |
| 短距帧龄 | 无线上界 807.947 ms，独立有线上界 796.597 ms，均依该轮时钟模型与观测条件 |
| 恢复 | 原 R22 SD 脱机冷启动和停发后自动恢复经用户观察通过。3.4 秒人工源积压轮 200 帧已解码、124 帧按期限或积压策略抑制、76 帧物理呈现 |
| 持续容量 | 2 MHz 短距试验的持续窗口测得余量 34.10%，1 秒窗口余量 7.83%。试验保留启动块 20 条缺失，完整可靠容量与统一配置验收未完成 |
| 待验证 | 约 10 米，最终 RAW 接收程序脱机复核，完整 G0/G6，整板 I/O/CDC 与完整 CPU、内存和 DDR 预算。RF 反馈和 TDD 未实现验收 |

[RAW 短距、帧龄与积压](evidence/g0-R22-live-RAW-timebase-20261107.json) · [G7 脱机功能](evidence/g0-R22-G7-detached-operator-acceptance-20261106.json) · [G7 主机日志收据](evidence/g0-R22-G7-detached-TX-20261106.json) · [容量子试验](evidence/g0-R22-RX-bandwidth-live-and-capacity-20261107.json)

## 布线后资源与时序

| 项目 | LUT | FF | BRAM36 等效 | DSP |
| --- | ---: | ---: | ---: | ---: |
| R22 实际 routed | 47,118 | 44,183 | 74 | 150 |
| 阶段二估算预留 | 2,500 | 2,000 | 2 | 8 |
| 实际值加预留 | 49,618 | 46,183 | 76 | 158 |

R22 布线时序报告记录 WNS +0.115 ns、WHS +0.023 ns、WPWS +0.264 ns。最终 RAW 运行软件相对上次配对构建增加 PC TX ELF text 112 B、ARM ELF text 72 B，显式 1,200 帧 ACK 观测缓冲增加 9,600 B，ACK 回调静态栈增加 8 B，PL/DMA/像素池未增加。更早的 SOURCE 区间传递独立增加 ARM text 240 B、上下文 256 B。 [实现证据](evidence/g0-R22-integrated-implementation-20261106.json) · [区间传递证据](evidence/g0-R22-capture-interval-receiver-20261107.json) · [I/O 与 CDC 范围](evidence/g0-R22-IO-CDC-scope-20261107.json) · [资源账本](../spec/g0_resource_ledger.json)

收据中的原始路径仅用于识别历史工件。发布时归一化工作站路径，清单保留原始及发布散列。 [收据清单](evidence/provenance.json)
