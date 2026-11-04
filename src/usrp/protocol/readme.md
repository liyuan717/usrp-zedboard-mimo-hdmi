# MRF1 v1：USRP → AD9361 双流空口协议

协议版本：1.0.0，日期：2026-10-24。固定PHY profile：`MRF1-QPSK-2X2-4M-v1`。

这是USRP与FPGA接收机共同遵守的接口冻结包，不是已经通过实板联调的报告。USRP工程由另一侧实现；本分支不修改USRP驱动、接收机RTL、Linux镜像或HDMI。既有RF开发分支保持独立。

请另一侧agent依次读取：

1. 本文：规范性帧/调制/采样定义。
2. [USRP_IMPLEMENTATION_REQUIREMENTS.md](USRP_IMPLEMENTATION_REQUIREMENTS.md)：完整发送端工程、流水线和性能验收。
3. [VIDEO_PAYLOAD.md](VIDEO_PAYLOAD.md)：后续视频目标使用的RVF2/ZVID2业务封装，不能与PHY帧号混同。
4. [INTEGRATION.md](INTEGRATION.md)：设备连接、分阶段联调、现有能力缺口和交接记录。
5. [air_interface.json](air_interface.json)：机器可读参数；[vectors/manifest.json](vectors/manifest.json)：确定性向量和哈希。
6. [RF_RECEIVER_IMPLEMENTATION_REQUIREMENTS.md](RF_RECEIVER_IMPLEMENTATION_REQUIREMENTS.md)：板端完整工程、DMA批处理、持续流水线与同等视频性能验收约束。

## 1. 规范优先级与边界

本目录规定的字节格式、已冻结参考向量和PHY参数是v1接口。文本与向量冲突时必须停下来报告并升级本协议，不能任选一个实现。允许不同语言独立实现；不得依赖另一个工作目录的未提交文件。

当前目标是USRP双通道同步发射，FMCOMMS3/AD9361双通道接收，Zynq接收端做同步、信道估计、MHGD检测、MRF1解析与CRC。HDMI后置。HDMI未完成不阻碍空口包验收，但RF包成功不能被称为视频帧率达标。

本PHY是单载波、2×2空间复用QPSK，不是OFDM、Wi-Fi、LTE或UHD自带包格式。无FEC、交织、重传、白化或加密。USRP不得自行加入这些处理。MHGD位于接收检测端，不是发射端编码器，也不是神经网络。RXTH头仅是FPGA→PS的本地DMA格式，绝不能发到空口。

## 2. 固定参数

```text
TX streams / RX antennas   2 / 2
sample rate                4,000,000 complex samples/s/channel
symbol rate                1,000,000 symbols/s/stream
samples per symbol         4
modulation                 unit-average-power QPSK
RRC rolloff / span          0.35 / 10 symbols
RRC taps                   41, symmetric, sum(tap²)=1
payload                    128 bytes per stream per RF frame
pilot interval             64 payload symbols (last block 16)
frame                      950 symbols, 3840 samples/channel including RRC tail
default RF center          2,450,000,000 Hz, configurable only by shared run manifest
initial analog bandwidth   3,000,000 Hz, receiver hardware profile must match
reference IQ peak          max(abs(Re),abs(Im)) across both lanes = 0.125
```

两路TX使用同一个帧号、同一个设备时钟和同一个起始采样时刻。通道0/1不允许各自循环、分别起流或相互错一帧。两路payload可以不同，数据阶段必须同时发射，不能把“两天线”实现为轮流发送整帧。

射频中心频率不是字节帧字段。更改频率需双方共同修改运行清单，不得默默偏移。采样率、符号率、RRC、导频布局的变更需要新profile ID与新向量，不能仍宣称本profile兼容。硬件可支持的更高采样率不等于当前bit/约束/驱动已支持。

## 3. MRF1字节包：每个stream各152字节

所有多字节整数均为little-endian；每个字节转比特时仍是MSB-first，这两条不是同一个概念。

```text
offset size field
 0      4   ASCII "MRF1" = 4d 52 46 31
 4      1   version = 1
 5      1   stream_id = 0 or 1
 6      1   modulation_id = 1 (QPSK only in this profile)
 7      1   flags = 0
 8      4   RF frame_id uint32
12      4   payload_bytes uint32 = 128
16      4   header_crc32, over bytes [0,16)
20    128   payload, arbitrary immutable bytes
148     4   payload_crc32, over bytes [20,148) only
```

等价header结构为 `<4sBBBBII`，长度16；追加header CRC后为20。两个CRC分别计算，payload CRC不包含header。CRC为CRC-32/ISO-HDLC：反射多项式0xEDB88320、init/final-xor均FFFFFFFF；与Python `zlib.crc32()`一致，`CRC("123456789")=CBF43926`，在字节流中存为`26 39 f4 cb`。不再追加以太网CRC或UHD包头。

frame_id在每个新RF帧递增，不能每个视频AU归零。跨uint32回绕必须显式新会话/重置，不能当作连续链。两路stream的frame_id、长度和调制必须一致；stream_id分别为0、1。首轮确定性payload仅用于测试，实际业务可以是任意128字节；接收算法不得读取测试payload来推断答案。

## 4. 比特与QPSK映射

每字节按bit7..bit0展开，每两个bit形成一个索引，先发bit7:6，再5:4、3:2、1:0。

```text
bits  index  complex symbol
00      0    ( +1 + j ) / sqrt(2)
01      1    ( -1 + j ) / sqrt(2)
10      2    ( -1 - j ) / sqrt(2)
11      3    ( +1 - j ) / sqrt(2)
```

例：`0x1B = 00 01 10 11`，对应索引`[0,1,2,3]`。禁止换成其他Gray映射、交换I/Q、取复共轭或额外旋转π/4后仍使用本向量。全局常相位由信道估计吸收，不是允许修改协议的理由。

header的20字节始终映射成80个QPSK符号；payload及其CRC的132字节映射成528个QPSK符号。这528个同时刻的双流符号对，对应接收端每RF帧528个2×2检测任务，不是528个原始ADC样点。

## 5. 已知序列：不使用语言/库随机数

所有已知符号按下列SHA-256计数扩展生成，禁止用NumPy/LabVIEW/MATLAB的默认随机数代替：

```text
prefix = ASCII(domain) || 00 || LE32(seed) || LE32(frame_id) || LE32(stream)
bytes  = SHA256(prefix || LE32(0)) || SHA256(prefix || LE32(1)) || ...
取所需长度，按第4节映射QPSK。
```

训练等已知序列的seed/frame_id/stream全部为0。domain如下：

- `MRF1-PREAMBLE`：取8字节，32符号，重复两次。
- `MRF1-SFD`：取8字节，32符号，接在两次重复块后。
- `MRF1-TRAINING`：取16字节，64符号，两路训练各复用同一序列。
- `MRF1-PILOT`：取1字节，只用前两个QPSK符号p0、p1。
- 测试payload可用`MRF1-PAYLOAD`，seed分别717/1717、实际frame_id及stream_id；它不是接收端先验。

## 6. 两路符号时序

每段起点是脉冲成形前的符号索引；完整列表见每个向量的`sections`。

```text
start count TX0                         TX1
  0    32   zero guard                  zero guard
 32    96   PREAMBLE,PREAMBLE,SFD        zero
128    12   zero                        zero
140    64   TRAINING                    zero
204    12   zero                        zero
216    64   zero                        TRAINING
280    12   zero                        zero
292    80   stream0 header QPSK         stream1 header QPSK
372    ...  9 repetitions of pilot pair + payload block (below)
918    32   zero guard                  zero guard
```

每个pilot pair恰为两个时间符号：第一个`TX0=p0, TX1=0`，第二个`TX0=0, TX1=p1`；然后两路同时发送最多64个payload符号。前8块各64，最后1块16，因此有18个pilot时间符号+528个payload时间符号。训练与导频通过时间正交估计2×2信道的两列；它们不是两路同时发送相同序列。

## 7. 脉冲成形、幅度与帧连接

在符号n插入到`u[4n]`，其他过采样位置为零；u长度为950×4=3800。与41抽头RRC做full convolution，输出3840复样点。RRC的时间位置是-5..+5 symbol、步长1/4，公式与特殊点处理由标准库参考程序和manifest的41个抽头冻结。不得改成`same`卷积、删前后20样点或自行trim 40样点。

两路成形后用同一个正实数缩放：取所有I/Q分量绝对值的共同最大值peak，乘`0.125/peak`。不分别归一化两路，也不对不同帧/不同通道额外做AGC。参考采用按帧独立full convolution，依次连接每帧的3840样点，包含它自己的guard和tail；禁止同时改为跨帧连续滤波器状态，否则样点布局已不是当前profile。

UHD缓冲区边界与空口RF帧边界独立：一个发送块可包含多帧或一帧的尾部+下一帧头部，但不能为了对齐API块长在每帧中间插零、重复或丢样。有限会话结束时允许在最后完整帧之后补零到发送块边界，清单必须记录padding；padding不是新业务帧。

## 8. IQ文件/主机格式，不是空口包头

参考`.fc32le`按sample-major存`I0,Q0,I1,Q1`，每个分量IEEE754 float32 little-endian。每帧61440字节。读为complex64后是`[samples,2]`；传给具体USRP API所需`[channels,samples]`等形状时应显式变换，不得依赖错误stride。发送的是两个复通道，不是把四个分量当四根天线。

参考`.sc16le`具有同样顺序，每分量为`round_ties_to_even(normalized*32767)`的signed int16，整帧30720字节。它是主机IQ量化参考，不是AD9361数字引脚上的12位排列。接收机本地RXTH的signed12-MSB格式不得反向套到USRP文件。fc32与sc16两种主机格式不应被无依据地再乘16。

包字节和SHA/CRC必须精确相同。不同语言卷积/浮点求和可有末位差异：独立IQ实现对参考fc32最大绝对分量误差不得超过1e-6；直接读取所附二进制向量时文件SHA必须一致。不能只比较文件长度。

## 9. 同步与现有能力边界

现有重复块CFO估计的无歧义范围严格小于`symbol_rate/(2*32)=15,625 Hz`；边界不保证。首轮互通要求测得并控制残余CFO在±10kHz以内，或者接收侧完成独立宽频偏获取扩展后，再在运行清单中声明扩展已验收。不要把这当成USRP自身频准规格，也不要假设两个独立设备天然零频偏。共享参考/LO预补偿/宽范围搜索的选型须写进实际配置与证据，不能偷偷修改已知序列。

当前数字验证只覆盖静态平坦信道与有限CFO/SFO测试，并未证明频率选择性多径、任意自由运行时钟或OTA现场均可用。MHGD当前32次迭代、零随机提议在某些混合信道会陷入局部最优；它不是线缆/USRP格式错误的替代解释。接收端后续必须解决并给出CRC证据，不能用软件ML替代后冒充PL结果。

## 10. 带宽与性能验收

每RF帧0.00096s；理想连续上限1041.667 RF帧/s。两路共256字节MRF1 payload，因此裸业务净上限约2.133333 Mbit/s，不是QPSK的4Mbit/s原始数据符号率。视频采用RVF2时每128字节只含100字节分片数据，长流上限约1.666667 Mbit/s，还未扣AU头、奇数分片重复、间隙、丢帧或恢复。

1024×600 RGB565原始视频30fps为294.912Mbit/s，60fps为589.824Mbit/s；本profile不可能直接承载未压缩视频。后续USRP必须接完整有界压缩/组帧流水线。1.2Mbit/s“目标码率”不是峰值输出保证；真实一秒30帧参考曾输出173491B、888RF帧，占空口85.248%，且单个IDR需100.8ms串行化。若仅80%空口可用，此样本平均带宽已不足，不能靠扩大队列解决。

硬验收是1024×600至少30个新源帧/s，60为扩展目标；在达成前不能用降低分辨率、重复图案、循环旧IQ、重复扫描或只报USRP采样率替代。编码率/画质必须按动态图源评估并记录PSNR/SSIM或经确认的视觉基线，不能只按码率宣称画质已满足。更高吞吐profile需双方先修改硬件/接收代码/约束、重新生成向量并共同验收。

## 11. 离线使用与变更流程

```sh
python protocols/mrf1-v1/reference_mrf1.py self-test
python protocols/mrf1-v1/reference_mrf1.py verify --output protocols/mrf1-v1/vectors
# 如需在别处重新生成（目标目录必须不存在）：
python protocols/mrf1-v1/reference_mrf1.py generate --output /a/new/vector-directory
```

这些命令只需Python标准库，绝不打开USRP/串口/FPGA或发射RF。另一侧应先独立验证packet和IQ，再做硬件试验。

协议修改必须提交新revision、列出兼容性变化、更新JSON/参考程序/向量/manifest和验收结果，双方共同使用同一个commit。当前分支发布不代表RF receiver、USRP工程或HDMI已经完成。
