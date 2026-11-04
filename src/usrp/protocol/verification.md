# 协议发布前离线验证

日期：2026-10-24。以下均为离线软件/文件验证，不是USRP发送、RF接收或视频性能验收。

## 两份独立实现交叉比较

标准库 `reference_mrf1.py` 不依赖NumPy或项目源码。与接收开发工作树当前的NumPy `waveform.generate_waveform(WaveformProfile(iq_peak_component=.125), frame_id, payloads=...)` 比较三组输入：

- deterministic_0：两个152B packet逐字节一致、两个通道合并的sc16逐字节一致、fc32分量最大误差3.7245652950712582e-9。
- ascending_1：packet/sc16逐字节一致，fc32最大误差3.7252187862213404e-9。
- deterministic_65535：packet/sc16逐字节一致，fc32最大误差3.7245652950712582e-9。

三组符号段布局完全一致；容差要求1e-6，此次最大误差约3.73e-9。对照的接收开发源码 `host/src/mimo_host/waveform.py` SHA256为 `03347d8570e6c34bb591f70e126e912cc6c7c399219b0a5fc1f2d9cd9db7988f`。这是源码快照哈希，不把未提交工程状态冒充某个Git commit。

发布前将参考工具的段名称统一为零起始通道：`preamble_tx0`、`training_tx0`、`training_tx1`。接收开发源码沿用从1开始的标签；比较时按段起点、长度和实际lane匹配，名称更正不改变IQ或packet字节。

另以所附真实IDR作RVF2交叉验证：独立标准库参考与接收分支的 `encode_access_unit()`、`fragment_access_unit()` 的36B ZVID2头、完整AU封装以及209个128B片全部逐字节一致，对应105个RF帧。此比较不运行射频。

协议branch只包含本目录可独立运行的reference与vector；接收算法、MHGD、RTL、Linux构建和HDMI不在本次验证范围内。

## 可重复验证

```sh
python protocols/mrf1-v1/reference_mrf1.py self-test
python protocols/mrf1-v1/reference_mrf1.py verify --output protocols/mrf1-v1/vectors
python protocols/mrf1-v1/reference_rvf2.py --self-test
python protocols/mrf1-v1/reference_rvf2.py --verify-example protocols/mrf1-v1/video_example
```

PHY自检覆盖CRC标准check、MSB-first/QPSK映射、包长度/CRC拒绝、双路时分训练/导频与固定IQ长度。vector verifier检查三组必需向量、recipe SHA、artifact集合、文件哈希、packet解析及IQ重生成，不能删掉向量/文件后得到虚假PASS。

RVF2自检覆盖乱序重组、幂等重复、缺片、冲突身份与CRC损坏拒绝；只验证封装，不验证任意输入为合法H264。`video_example`的AU来自此前实际编码的1024×600测试图，曾用真实codec解析/解码；这不意味着本协议工具提供编码器或已经验收USRP工程。

任何协议变更都需重新运行上述命令，并同步检查机器JSON、正文、向量与两侧实现。最终成功必须另外提供INTEGRATION.md所列硬件与业务证据。
