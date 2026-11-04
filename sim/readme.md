# 仿真、验证与证据导航

- [首次验收总表](../board/results/acceptance.json) 和 [两段原文件实板回执](../board/results/runs/)：新 ADC、RF/CRC、原文件、像素、物理呈现和用户视觉分别保存。
- [失败证据](failures/)：CRC 拒绝和漏帧两种代表性失败；[全轮次索引](attempt_index.json) 保留 50 个历史检查点。
- [资源/时序](../build/reports/)：布线、综合、时钟、CDC/DRC 原报告。
- [离线验证](verification/)：ARM/DCP/BD 构建、原始源码身份、技能正负例及新目录迁移检查。

两段已验收的原始 ADC/RFD、参考原文件与照片基线数据在运行包中；按恢复清单还原后可独立字节审计。技术门禁、用户视觉反馈和离线检查不相互替代。迁移使用离线工具，没有启动新的硬件轮次。
