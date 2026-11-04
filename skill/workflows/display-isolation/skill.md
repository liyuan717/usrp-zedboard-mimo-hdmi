---
name: display-isolation
description: Diagnose flicker after successful receive/decode using controlled display-only and subsystem isolation.
---

先证实原文件/像素正确，再从干净环境重显同一实收输入；按实际问题选择 LO、ADC、CPU 或 PL 单因素条件，保留基线。对用户未看清或收回反馈的轮次记为未确认。同 buffer 与静默 UART 是本工程已用策略，不承诺适用于所有显示驱动。

使用下面的提示模板并填入实际路径和本轮限制：

> 请针对已经校验正确的接收文件设计一个最小显示隔离轮次。保持输入、模式、驱动一致，只改变一个条件；分别报告物理回执和视觉反馈。不能因某次不闪就宣布根因已修复。

来源与失效条件见 ../../references/pitfalls.md 和 ../../references/evidence_sources.md。
