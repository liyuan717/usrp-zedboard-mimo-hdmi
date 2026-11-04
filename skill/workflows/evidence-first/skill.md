---
name: evidence-first
description: Audit staged RF, file recovery and physical display evidence without confusing replay or visual acceptance.
---

先确定本轮身份和采集来源；按新 ADC→双流包→全文件→解码像素→物理呈现→视觉反馈顺序审计。对缺失层写“未验证”。成功和失败都保留。校验脚本见 ../../scripts/check_evidence.py；项目固定计数仅是样例，不可推广到不同帧协议。

使用下面的提示模板并填入实际路径和本轮限制：

> 请审查以下运行目录。给出每层可验证结论、支持文件与剩余缺口；不要只依据网页绿色状态。区分新空口、历史重显、完整文件、物理回执和用户视觉。遇到缺帧或 CRC 拒绝，保留失败；未经验证不放宽门禁。

来源与失效条件见 ../../references/pitfalls.md 和 ../../references/evidence_sources.md。
