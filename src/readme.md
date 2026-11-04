# 当前功能源码

`src/` 是便于审阅的小写目录视图；`build/source_manifest.json` 绑定 797 个当前源码/配置文件的原路径、SHA256 和大小。所有功能源字节保留，只有浏览目录重新组织；构建前运行仓库根 `build/scripts/restore_workspace.py` 恢复标准原路径。

| 目录 | 内容 |
|---|---|
| src/web | 本机网页、原文件发送控制、串口/TFTP/停止、SHA/结果核验 |
| src/usrp | 双路 UHD 有限发送器、波形、MRF/RVF 协议 |
| src/receiver | 已使用的 5120 块 ARM 捕获、同步、估计、前端/传输 |
| src/display / src/codec | 当前 HDMI presenter、PTS 调度、视频解码/CRC/NEON 辅助 |
| src/drivers | 当前 MIMO/RXTH、HDMI、DMA provider 与 UAPI |
| src/fpga | 已验收 RTL/约束/配方及使用中的 ADI IP |
| src/control / src/assembly | 当前编排及仍被装配函数读取的已验证兼容代码 |

`assembly` 中的兼容代码是当前构建调用链的依赖，不代表额外的新成果。没有加入 gain21、10fps、白化等未验收分支。固定 ELF/KO、bit/DCP/XSA、运行库、参考数据存于 `data/runtime_assets.tar.zst`，并由 `board/asset_manifest.json` 逐文件绑定。
