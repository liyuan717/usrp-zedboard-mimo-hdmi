# 下载、恢复与复现

## 1. 获取完整成果

```bash
git clone https://github.com/liyuan717/usrp-zedboard-mimo-hdmi.git
cd usrp-zedboard-mimo-hdmi
python3 build/scripts/verify.py
python3 build/scripts/download_assets.py
python3 build/scripts/verify.py --assets
```

已安装 Git LFS 时，可用 `git lfs pull` 获取运行包。GitHub 的 ZIP 下载可能只包含 LFS 指针，不能将指针当压缩包使用。运行资产仅一个版本，SHA256 在 `data/manifest.json`。

## 2. 恢复完整工作区

```bash
python3 build/scripts/restore_workspace.py --output /tmp/mimo_competition
```

输出目录必须不存在，不覆盖用户工程、系统文件或 SD。脚本验证所有源码和运行资产，恢复原大小写、可执行位及原工程相对路径。浏览用源码在 `src/`，经映射恢复后 Python/CMake/include 才按原目录工作。

原验证环境使用 `/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3` 及 `/home/leeeo/Downloads`。完整网页硬件流程仍绑定此记录环境；新机器需在该路径恢复并配置匹配依赖，或先适配/核验路径。不能假定任意目录改名后都能完成原硬件验收。**记录路径已有工程时不要覆盖**。`external_media/` 中只保留源照片和已验收的两段测试视频，使用前放到对应 Downloads 路径。

## 3. 软件与 FPGA 构建

主机需要 Python 3.12、g++、CMake、make、ffmpeg/ffprobe、UHD、zstd。ARM 参考编译器为 Bootlin `armv7-eabihf--glibc--stable-2024.02-1`、GCC 12.3.0。参见 `dependencies.md`。

```bash
python3 build/scripts/build_receiver.py --workspace /tmp/mimo_competition --compiler /path/to/arm-buildroot-linux-gnueabihf-g++ --output /tmp/mimo_receiver_build
python3 build/scripts/build_fpga.py --workspace /tmp/mimo_competition --name competition_validate --action validate
```

参考记录中 ARM 重建产物 SHA256 为 `09fd7222ec31434d5643c9235171add705c747e5e9b853da2182f99228d83eec`；原路径及隔离恢复路径均实测一致。Vivado 2025.2 的工程/BD 生成已通过；本次迁移不重新宣称完整综合/实现通过。`rebuild_from_routed.tcl` 可从恢复的 routed DCP 回建位流，已实测配置 payload 与原 bit 一致。

## 4. 板卡与网页演示

保留已验证的 SD 或准备匹配 Linux 根环境；运行包的参考 rootfs 已移除继承 SSH 授权并锁定口令，14 个关键运行组件 SHA 与验收环境一致。该分发 rootfs 未单独做新的 SD/冷启动硬件验收，不提供自动写卡操作。已有工作 U-Boot/SD 是当前 RAM 演示的硬件前提。

保持 USRP、ZedBoard＋FMCOMMS3、天线、HDMI、网线及 USB 串口连接。USRP `192.168.10.2`，电脑 `192.168.10.1`；板卡 `169.254.209.84`，电脑 `169.254.209.83`。UART 按实际 Cypress 固定 by-id 配置，不猜 ttyACM 编号；新设备应显式更新部署配置。

恢复工作区后，在其 `photo-console/` 下按 `setup_once.py`（首次 sudo 权限配置）及 `install_user_service.py` 安装服务。打开 `http://127.0.0.1:8098/`，选择兼容原 MP4，点击发送。冷 SD 无命令入口时按页面要求使用 PS-RST。后台执行新采集、新有限发射、原文件恢复和物理 HDMI；不写 SD/saveenv。照片入口为 `/photo`。

参数边界：1024×600、25 帧/5 秒、源 5 fps、Constrained Baseline/YUV420P、无 B 帧、每帧单 slice、原文件 ≤180000 字节。固定 5.5 GHz、4 MS/s、双通道，已验收增益 18 dB。完整接收后再播放。源标称 5 fps，物理实测约 4.30/4.49 fps，不等同于均匀 5 fps 或持续实时。

## 5. 验收与停止

先看新 ADC 身份与连续性、唯一 RF 帧集合、双流 CRC、完整文件逐字节/SHA、解码像素和 25 帧物理回执；再独立观察 HDMI 并填视觉反馈。失败和没看清都不能记为视觉通过。停止要等待 TX、LO、ADC 清理完成。

`board/results/` 保留两段成功原始回执，`sim/` 保留两种典型失败和全运行索引；原始 ADC/RFD/参考原文件在运行资产中。迁移与离线校验不增加新的 RF/HDMI 验收。
