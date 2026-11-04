# 自主选题赛道·初级组提交要求对照

依据用户提供的 AMD 2026 指南第 3.3 节（印刷页 19-22）及第六节（印刷页 25）；文件身份见 `guide_reference.json`。本表不代替后续官方评分细则，也不自行补写报名截止日。

## 组别、平台与工具

- 按用户声明组织为初级组。指南限定大一至大三在读本科生；混合年级/学历按最高者归高级组。队员学籍由报名审核确认，仓库不存放学生证或个人证件，也不假定已通过资格审核。
- ZedBoard / XC7Z020CLG484-1 属于 Zynq-7000，器件类别符合指南。外接 FMCOMMS3（AD9361）、USRP X310/CBX；这些为本作品实际硬件，未更换板型。
- 实现为 RTL＋ARM C++/Python＋Linux；Vivado 2025.2 是指南明确允许版本。未采用 HLS、AI Engine、PYNQ，不将其他赛道的 Ryzen AI PC、ROCm 或本地大模型部署要求搬入本赛道。
- PYNQ 是推荐而非强制；通用 PYNQ Skill 是单独加分，本作品不申报未经 PYNQ 验证的额外加分。
- 根 `license` 保留自有代码的可再分发许可；第三方声明见 `third_party/` 和 `third_party.md`。

## 3.3.5 提交物

| 要求 | 本仓库位置 | 已有验证/状态 |
|---|---|---|
| 完整工程源码（按实际实现方式） | `src/`、`build/source_manifest.json`、`board/asset_manifest.json` | 797 个当前源码/配置文件；45 条自有 RTL/约束、18 项配方、102 条 ADI 输入身份匹配 |
| 仿真或验证结果、正确性确认方法 | `sim/`、`board/results/`、运行包中的 ADC/RFD/参考文件 | 源→新 ADC→RF/CRC→原文件→像素→物理呈现→用户视觉分层核验 |
| 综合/实现报告与关键性能 | `build/reports/`、`performance.md`、`metrics.csv` | routed 资源/时序、时钟和两个物理帧率；不把理论值当实测速率 |
| 上板工程、脚本、实测/参考比对 | `board/`、`data/runtime_assets.tar.zst`、`reproduction.md` | 原 RAM 启动组合与实板证据保存；当前迁移未新做 RF 验收 |
| 可复现构建脚本 | `build/scripts/` | ARM 原目录及恢复目录字节回建一致；DCP payload 一致；Vivado 工程/BD 校验通过；完整综合/实现本轮未重跑 |
| 技能包与范围/用法/失效条件 | `skill/readme.md`、工作流/脚本/模板/排错 | 4 个工作流、7 项原正负例；迁移后的字节码依赖案例另有 ARM NULL 验证 |
| 设计报告：背景、已有工作、原理、划分、接口、优化 | `design_report.md`、`related_work.md`、`rtl_design.md`、`performance.md` | 工程集成贡献，不宣称算法首创或未测最优性能 |
| 大模型协作提示、回答、自纠与工作流 | `collaboration_record.md`、`collaboration/` | 只整理可见真实交互及工具失败/修复；非虚构完整聊天导出 |
| 技能提炼与验证边界 | `skill_derivation.md` | 每项关联失败/观察、复用范围、验证和边界 |
| 从零复现说明 | `reproduction.md`、`dependencies.md`、恢复/校验入口 | 下载→SHA→新目录恢复→离线构建/解码已执行；参考环境和部署前提逐项说明 |
| 全英文小写命名 | 所有提交路径 | 按小写规则统一；原源码大小写通过清单恢复，中文只在正文 |

## 本版交付与决赛说明

本版按“当前已验收功能的完整工程”交付：源码、构建/运行脚本、固定运行资产、实测与参考对比、报告和技能包均有对应入口。参见 release.md 与 deliverables.json。

报名资格与提交时间由队伍在官方系统办理。若进入决赛，指南第六节规定使用 AMD 官方 PPT 模板制作一页英文海报、不含队伍信息，A4 彩色打印；本包附 poster_content_en.md 供排版使用。这是决赛展示方式说明，不列为当前工程的开发待办。

协作材料以真实可见提示、回答和纠错案例组织，明确摘录来源及验证证据，不编造聊天记录。本版功能边界为兼容原文件完整接收后播放；真实性能与支持的输入格式见 performance.md 和 release.md。
