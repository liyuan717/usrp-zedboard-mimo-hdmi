# 分发精简阶段的失败与修正

本目录为打包/离线检查证据，不是新的硬件失败。Python 根树缺 encodings 字节码、Vivado 配方缺静态 wrapper、ADI IP 缺 gtcl/ttcl 等依赖已从冻结来源补齐，功能源字节未修改。最终通过记录见 ../../verification/migration_arm_decode.json 与 ../../verification/migration_fpga_validation.json。工具创建的 .Xil 目录改由构建辅助脚本的外置输出目录承载。
