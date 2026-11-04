---
name: clean-runtime
description: Prepare repeatable embedded-board RAM tests with pinned assets, exclusive ownership and confirmed cleanup.
---

先核目标板、串口固定 ID、当前 boot_id、启动资产 SHA 和 SD 状态。取得硬件独占权。每轮新目录，显式限时发射；停机先停 TX，再停止板端任务并确认 LO/ADC。异常内核不作为下一轮基线。历史脚本固定 boot_id/一次性目录不能直接再用。

使用下面的提示模板并填入实际路径和本轮限制：

> 请为当前已授权有限硬件测试检查运行条件，并先完成离线验证。写出具体资产身份、执行上限、停止顺序和证据目录。保持现有 SD，出现不可确认清理则阻止下一轮。不要将某一用户的自动重试授权推广给其他人。

来源与失效条件见 ../../references/pitfalls.md 和 ../../references/evidence_sources.md。
