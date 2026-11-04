# 工具与外部依赖

| 项目 | 参考版本/来源 | 范围 |
|---|---|---|
| Vivado | 2025.2，器件 xc7z020clg484-1 | 用户自行安装及取得许可；47 条 Xilinx 工具 IP 输入由工具生成 |
| ARM 工具链 | Bootlin armv7-eabihf--glibc--stable-2024.02-1；GCC 12.3.0/binutils 2.41 | 原字节回建参考；不重复收录 495 MB 工具链 |
| Python | 主机 3.12；板端 3.12，14 项运行 SHA 固定 | 板端离线 Python/库闭包与 QEMU 在运行包 |
| Linux | 原 ADI/Xilinx 6.12 项目环境，外置模块 ABI 需匹配 | 原 kernel/DTB/模块在运行包；完整 Linux 开发树依赖外部 SDK |
| FFmpeg / UHD | 已冻结运行库／USRP X310-CBX | 原解码器/库已保存；主机 ffmpeg/ffprobe、UHD 需安装 |
| ADI HDL | 4366bd63c1fe56b680b69145981a4493c6f8753a ＋项目原 patch 树 | 102 条 vendor 输入与原 SHA 一致；源码只保留已用 IP |
| zstd | 常规 zstd CLI | 校验后解包运行资产 |

Bootlin 原工具链 README 的复建步骤：

```bash
git clone https://github.com/bootlin/buildroot-toolchains.git buildroot
cd buildroot
git checkout toolchains.bootlin.com-2024.02.1
curl https://toolchains.bootlin.com/downloads/releases/toolchains/armv7-eabihf/build_fragments/armv7-eabihf--glibc--stable-2024.02-1.defconfig > .config
make olddefconfig
make
make sdk
```

也可从 https://toolchains.bootlin.com/ 选择同一发行版。环境变化不自动视为与原工具链字节等价，应核对 `build_receiver.py` 的输出 SHA。
