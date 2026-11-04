# 可复用技能包

适用对象：与大模型协作进行 FPGA＋ARM＋无线接收＋显示联调的团队。本项目采用 RTL、ARM C++/Python；未使用 PYNQ，不申报“通用 PYNQ Skill”验证或加分。

所有交付路径使用英文小写；`skill.md` 是交付入口。若安装到要求 `SKILL.md` 的模型工具，在提交目录外复制并改名为 `SKILL.md`，内容不变。不要为满足模型工具格式改动本交付包。

| 项目 | 适用范围与使用方式 | 已验证效果及证据 | 失效条件 |
|---|---|---|---|
| [证据工作流](workflows/evidence-first/skill.md) | RF/文件/显示多阶段验收；按提示模板收集新运行身份及逐层回执 | 两个原 MP4 网页实收轮文件字节、1674/1546 CRC 包、25 帧呈现及用户视觉反馈分开记录 | 缺 ADC/原始包/源文件、套用旧结果、无视觉反馈时不能宣称相应通过 |
| [干净运行环境](workflows/clean-runtime/skill.md) | 多模型共享串口、RAM 启动、重复测试；先核身份、锁和资产哈希 | 串口独占/停止顺序测试、干净 RAM 启动；31 项回归记录 | 板型/内核/位流/权限改变，或异常后内核健康无法确认 |
| [显示隔离](workflows/display-isolation/skill.md) | 已恢复正确数据但闪动；逐轮切换 LO、ADC、CPU、PL 条件 | 历史 LO-only/ADC-only/CPU-only/PL-only 视觉正常，实收轮仍可能闪动；不据此声称唯一根因 | 同时改变多个条件、沿用异常内核、用技术回执代替肉眼观察 |
| [资源与回建](workflows/reproducible-fpga/skill.md) | 导出 routed 报告、核查源闭包与工具版本；使用报告/包校验脚本 | DCP 与 bitstream SHA 绑定；LUT 82.30%，WNS +0.160 ns；找回两份历史 HDMI 输入源码，最终 102 条 vendor 与 18 项配方匹配 | 仅有 synthesis 数字、工具版本变化、输入哈希不匹配；不能推定 Fmax/RF 吞吐 |
| [脚本与模板](scripts/) | `compare_bytes.py ref actual`；`check_evidence.py result.json`；`check_reports.py --utilization ... --timing ...` | 随包测试覆盖实收成功、真实失败、错视觉身份、篡改字节和负时序 | 当前证据检查器固定双流、5120 块、25 HDMI 帧；换协议前需改配置及负例 |

这些经验来自真实失败和对照；不是“让模型自行宣称成功”的提示。详见 `references/pitfalls.md` 与 `references/evidence_sources.md`。模板中的硬件操作仍需执行者具备该轮授权，技能本身不授予发射、改卡或改位流权限。
