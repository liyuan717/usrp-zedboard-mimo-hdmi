# 构建与固定源码身份

source_manifest.json 绑定当前源码；scripts/ 提供下载、校验、新目录恢复、ARM 回建、Vivado validate/synth/implement 和 routed DCP 回建入口。reports/ 保存原始资源、时序、CDC/DRC。全部步骤和依赖见 ../report/reproduction.md。构建输出在恢复目录中，工具缓存不会写入比赛仓库。
