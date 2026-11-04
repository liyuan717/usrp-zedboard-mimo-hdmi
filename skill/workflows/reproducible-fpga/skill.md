---
name: reproducible-fpga
description: Package FPGA/ARM builds with source and artifact hashes, routed resource/timing reports and explicit rebuild gaps.
---

区分 synthesis、route 和实板。先绑定 bit/DCP/XSA/源输入哈希，再读报告；时钟工作点不等于最大频率。将缺失源码、未执行的完整回建和已通过软件构建分开。脚本 ../../scripts/check_reports.py 只验证报告门禁。

使用下面的提示模板并填入实际路径和本轮限制：

> 请为该 FPGA 项目整理来源、工具版本、源码/依赖清单、构建命令、布线资源/时序和实板边界。对每个完整性缺口给出路径与预期 SHA；不要用近似 RTL 冒充已验收输入。

来源与失效条件见 ../../references/pitfalls.md 和 ../../references/evidence_sources.md。
