# 来源与验证范围

仓库 `board/results/` 存放首次网页验收，`sim/` 存放 50 轮检查点、失败原生/控制日志与离线验证，`build/reports/` 存放 routed 资源和时序报告。

- 首次可复用验收：5a72fa0d585f9311571ddf4479765e25（Sintel）与 d99541c284d220bc50971b0a192395a7（Bunny）。原 MP4 逐字节一致；各 25 物理帧；视觉反馈单独保存。
- 失败证据：d2bd6afcac4ccfe01c8a6821b76cc811、b535b748bbd1c0d38f0d5af6c489150d。前者 CRC 拒绝，后者漏帧。
- 显示隔离：聊天可见用户反馈与包内 photo/video comparison 记录；不构成唯一根因证明。
- 新增技能脚本由 `tests/test_skill_scripts.py` 在实收回执及构造负例上验证。`sim/verification/` 保存本次实际输出。
- 这些技能不依赖私有模型思考过程；协作摘录仅来自可见用户要求、操作和测试记录。
