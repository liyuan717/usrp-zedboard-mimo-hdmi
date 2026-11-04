# 固定运行与参考数据包

manifest.json 保存单一运行包的大小和 SHA256。runtime_assets.tar.zst 使用 Git LFS，包含匹配 bit/DCP/XSA、内核/DTB、固定运行程序与依赖、两轮新空口 ADC/RFD/参考文件和照片回归证据。无需复制整套工具链、SD 镜像或历史候选。

在仓库根执行 python3 build/scripts/download_assets.py 后，再执行 python3 build/scripts/verify.py --assets；SHA 通过后才能恢复。
