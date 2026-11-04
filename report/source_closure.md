# 原始源码闭包审计

已验收 bitstream/DCP/XSA SHA 固定。本次先由 ADI 固定提交重建 vendor 树，发现四处内容哈希不一致。随后在后续工程留存的 `rfv2-r9-adi-source-20261029.zip` 找到完整历史 vendor 树，压缩包 SHA256 为 `c45fd0ae0bb00792a3e5ba10870a578a545bedec6a3835454d33e07e24e3329a`。

最终采用精确原始 vendor 归档：**102 条历史 library/ipshared 输入匹配、18 项自有构建配方匹配，ADI 内容缺口为 0**。47 条 Xilinx 工具 IP 输入需要已许可的 Vivado 2025.2 生成，不随本项目分发其工具源码。详细检查见 `../sim/verification/source_closure.json`。近似重建树没有收入交付。

包中 `vendor/adi_hdl_verified` 在恢复工作区后可用作 ADI_HDL_DIR，父目录有匹配的 source-manifest.json；无需重新运行存在历史锚点问题的旧 HDMI vendor 补丁脚本。

这些比较确认了收集到的原始源码身份，不代表本次重新完成全部综合和实现。ARM 接收程序在原路径和新恢复路径都重新编译为相同 SHA。已保存 routed DCP 可用于单独回建报告/位流；从 RTL 重跑完整 Vivado 构建的执行结果应另行记录，不能与历史已验收 bitstream 混用。
