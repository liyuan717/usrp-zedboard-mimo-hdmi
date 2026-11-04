# RVF2 / ZVID2 视频业务封装

本文件仅规定MRF1的128字节payload如何装视频。功能阶段可以发送任意128字节测试数据；最终30/60新视频帧目标必须实现此封装或双方另行冻结的新版本，不能把测试图案当完整视频工程。

PHY帧号、视频帧号、会话ID是三个独立域。一个视频AU可能占很多RF帧；USRP的发送API块又可能跨RF帧，不得把三者混作一个序号。

## ZVID2：一个完整压缩AU

全部整数little-endian，结构`<4sBBHIHHIIIQ`，恰36字节：

```text
offset size field
 0      4   magic "ZV2D"
 4      1   version = 2
 5      1   codec = 2 (H264 Annex-B constrained-baseline contract)
 6      2   flags: bit0 IDR, bit1 SPS present, bit2 PPS present; others 0
 8      4   video sequence uint32
12      2   width = 1024
14      2   height = 600
16      4   decoded RGB565 byte size = 1228800 (not compressed length)
20      4   Annex-B AU byte length (1..4194304, bounded)
24      4   CRC32 of Annex-B AU bytes only
28      8   source timestamp milliseconds uint64, from the source capture timeline
36      N   Annex-B AU bytes
```

一AU对应一幅图像。完整发送端须配置/验证progressive、YUV420P、无B帧、单slice、低延迟的baseline码流，AUD放在AU首部；可恢复IDR须同AU携带SPS/PPS。不能只检查NAL type就宣称真实解码成功。至少定期提供独立IDR；接收丢失参考后丢弃P帧直至下一完整独立IDR。GOP/VBV/码率是运行清单参数，须实测平衡恢复时间与关键帧突发。

## RVF2：装入一个MRF1 stream payload的分片

结构`<4sBBHIIIII`，恰28字节，后接100字节保留数据区：

```text
offset size field
 0      4   magic "RVF2"
 4      1   version = 2
 5      1   flags = 0
 6      2   actual fragment data bytes, 1..100
 8      4   session_id uint32
12      4   video sequence uint32, equals ZVID2 sequence
16      4   total ZVID2 envelope bytes, including 36-byte header
20      4   byte offset in the complete ZVID2 envelope (0,100,200,...)
24      4   CRC32 of the entire ZVID2 header + Annex-B AU
28    100   fragment bytes, unused tail zero-padded
```

同一AU的所有分片必须保持同一session/sequence/total/crc。每次取最多100字节；分片数`ceil((36+AU_bytes)/100)`。末片actual bytes小于等于100，剩余部分补零且不计入包CRC。拼接、长度、总CRC以及内部ZVID2 CRC全部通过才允许交付AU。冲突的重复分片是错误，不是“以最新为准”。会话切换须显式重置，不能混拼不同源。

默认最多16384个分片，因此本profile下有效AU上限还受`16384×100−36=1,638,364`字节约束，不能仅看ZVID2的4MiB字段上限。source timestamp虽然占uint64，本工程只接受0..2^63−1以匹配后续codec有符号PTS。

每个RF帧把接下来的两个分片分别放到stream0、stream1。分片数为奇数时，最后一片在stream1重复同样不可变字节；接收器按身份+offset幂等处理，不算第二个视频AU。RF frame_id跨AU继续递增。视频session/sequence必须出现在业务头，而不是挤占MRF1 flags。

上行没有ACK/ARQ/FEC。因此协议不会凭空修复RF丢包；必须记录丢失、超时、WAIT_IDR恢复及最终成功新帧数。测试可以先做可靠线缆互通，但不能把缺失恢复机制描述成“零丢包实时视频”。

现有RF分支的实现来源为`host/src/mimo_host/rf_encoded_video.py`；本分支未包含其整个实现或codec。USRP侧需要自行实现完整编码、AU界定、封装、连续双流供数和运行控制，不可只输出一份静态IQ文件即交付。

`reference_rvf2.py`提供标准库封装/重组与CRC参考，其格式自检不证明输入可被解码。`video_example/`另提供一幅实际编码的1024×600测试图AU、对应ZVID2与拼好的128B分片列表，供逐字节对照；单幅参考图不是30fps性能证明。`fragments-128b.bin`按片序连续存放，每128字节一片，不含外层MRF1 header。
