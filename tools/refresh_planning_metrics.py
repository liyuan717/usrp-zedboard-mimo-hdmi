#!/usr/bin/env python3
"""Prepare only the three authorized planning documents from the verified733a3e0 snapshot."""
from pathlib import Path
import hashlib,json
ROOT=Path(__file__).resolve().parents[1]
src=ROOT/'docs/planning-refresh-20261103'
out=ROOT/'docs/planning-metrics-refresh-20261103'
out.mkdir(exist_ok=True)
texts={n:(src/n).read_text() for n in ('README.md','ARCHITECTURE.md','RF_PROTOCOL.md')}
def replace(n,old,new):
 assert texts[n].count(old)==1,(n,old[:80])
 texts[n]=texts[n].replace(old,new)
status='完整55RTL原始2097152样本回放现交付136条SOURCE/69632B（13312B恢复）、142记录491批，与R7完整接受流逐字节一致；此前52RTL的135/136失败保留为历史。R7有限实板已完成PL接收/CRC/固定8-bank/FEC→物理DMA→ARM的86SOURCE/44032B（9216B恢复）精确交付，68条直接源包含TX0的41条和TX1的27条；1024个32KiB批次CRC相等，STOP/重入无残留owner。该0.5秒合成FEC探针仍有6块过期和92个合并SOURCE/FEC事件丢弃，不能当可靠净容量、持续视频或全局误包率。'
pending='R8复用既有seal接口关闭窗口外LOADING块，交付已有完整源并有界终结；QUEUED/WORKING和持有SOURCE的owner保留到原终结握手。固定8-bank连续16个受损块从旧8/16交付改善为16/16，8个完整排队/活动块及第9任务拒收/释放后继续、CRC/回绕/背压和完整55RTL回放均通过；尚未部署新候选。R8整板综合48849 LUT/45595 FF/74 BRAM36等效/154 DSP，较同口径R7综合净增382 LUT、减少4 FF，RAM/DSP不变；整板布局布线正在运行，不能沿用R7时序。'
replace('README.md','- 压缩包/像素缓冲放DDR，不占大量BRAM；RGB565一帧1228800B，三帧约3.69MB。帧缓冲用DMABUF或等价明确所有权机制，避免多次RGB全帧复制；帧只在真正不再被scanout使用时回收。','- 压缩包/像素缓冲放DDR，不占大量BRAM；当前HDMI物理契约为XRGB8888/64-bit AXIS，一帧2457600B，三帧估算7372800B，另计页对齐、解码参考帧及转换池。RGB565一帧1228800B只适用于中间格式。帧缓冲用DMABUF或等价明确所有权机制，避免多次RGB全帧复制；帧只在真正不再被scanout使用时回收。')
a=texts['README.md'].index('2026-11-03本地已通过数据对照的PL检查点')
b=texts['README.md'].index('必要CRC、DMA记录边界',a)
texts['README.md']=texts['README.md'][:a]+'''2026-11-03最新整板G0候选为R7及待实板的R8；没有已验收的生产视频位流。以下区分同口径综合、实际布线和未实例化估算，BRAM为36Kb等效，不将子核成本重复相加：

|测量口径|LUT|FF|BRAM|DSP|
|---|---:|---:|---:|---:|
|R7整板综合|48,467|45,599|74|154|
|R7实际整板布线|47,125|45,498|74|154|
|R8窗口寿命修正整板综合，路由待完成|48,849|45,595|74|154|
|R7→R8同口径综合新增净成本|+382|−4|0|0|
|阶段二反馈/TDD另列估算，阶段一未实例化|2,500|2,000|2|8|
|R8综合加阶段二估算，仍非完整预算|51,349|47,595|76|162|

R7全部95575条网络布线完成且零路由错误，WNS/WHS/WPWS为+0.173/+0.015/+0.264ns，类型化RF/HDMI/counter65门禁通过。继承的12输入/16输出延迟缺项（24个去重外围端口）和两条CDC-10根与旧native ADC相同；完整生产外设I/O/CDC签署仍未通过，不能以有限G0候选复核代替。R8综合加阶段二估算的物理LUT余1851，相对85%软目标超6129；尚需剩余核心/并发核算，余量不是可布线或完整容量保证。

'''+pending+'\n\n'+status+'''

native SOURCE DMA采用8用户槽加1不可映射STOP排空槽，每槽32KiB，共288KiB coherent DDR，新增PL资源为0。有限填充5/32批、STOP/重入和上述有限RF载荷已实板通过；持续可靠吞吐、DDR/HDMI/视频并发仍未通过。旧32MiB临时诊断捕获不是常驻生产缓存，不能按其卸载申报节省。

优先精简项及实测净收益如下；局部与整板测量不相加，已包含的收益不从新总量再次扣减：

|项目与用途|处理及影响|实际资源变化与口径|
|---|---|---|
|旧外部1024×128 raw采集FIFO|候选已移除外层，保留128拍入口、真实WFIFO/CPACK/CDC及溢出/重捕获；过载可有界丢样|局部配对净省2320 LUT/824 FF/4 BRAM；已包含于整板，不再预扣|
|旧有限raw录制序列器|替换为SOURCE板桥，复用RF DMA/DDR互联，保留新设备身份/批次单位/owner|旧毛239 LUT/474 FF不能算净省；替换净收益未独立配对，不申报未知收益|
|HDMI详细读追踪|关闭详细寄存器，保留EOF/token/IRQ/事件FIFO和显示所有权|局部净省202 LUT/297 FF；R3→R5整板同口径实际净省212 LUT/285 FF，RAM/DSP不变|
|粗捕获多读口历史|改两份TDP映像，保留W33、II7和完整数值|R5→R6整板实际净省778 LUT/765 FF，增加9 BRAM|
|41-tap RRC历史移位|改三份TDP映像加recent旁路，保留全精度、41tap和II8|R6→R7整板实际净省2436 LUT/2734 FF，增加6 BRAM|
|宽观察/私有512历史/历史兼容/多方案电路|已裁剪或已复用的实例不重复申报；仅维护8-bank|本次新增节省0；既有历史证据保留|
|阶段二RF反馈/TDD|继续延期，TX1A无天线、板端TX关闭|未实例化预留是估算，不是可删现有资源，不阻塞阶段一G0|

R3→R7同口径综合累计净省3426 LUT/3784 FF、增加15 BRAM；计入必要R8窗口修正后，R3→R8净省3044 LUT/3788 FF、增加15 BRAM，DSP不变。旧外部FIFO替换发生在R3前，不再加到该累计值。旧50RTL分项和51372 LUT、旧52RTL丢1条SOURCE/容量预测、R3/R6布局失败均保留为历史；当前结论以本表实际整板口径为准，未测节省扣0。

'''+texts['README.md'][b:]
start='截至2026-11-03：'
a=texts['README.md'].index(start)
b=texts['README.md'].index('\n',a)
texts['README.md']=texts['README.md'][:a]+start+status+pending+'仍待实测/整机验证：8-bank接入真实PHY/DMA后的持续可靠吞吐和99分位突发/截止、实板独立支路相位/CFO跟踪、2×2实时1024×600真实新源20fps及画质、约3秒采集到HDMI帧龄、至少30%可靠净吞吐裕度、损坏/失锁后的持续运行与有界恢复及花屏冻结、完整资源/I/O/CDC/时序和DDR/HDMI并发、≥60秒RF素材/摄像头/约10m、无开发线冷启动。native本地ARM解码/物理HDMI的1200帧/约60秒子条件不能代替RF视频。完整G0及G1～G7尚未通过；阶段二反馈/TDD未实施/未通过，不阻塞阶段一。'+texts['README.md'][b:]
replace('ARCHITECTURE.md','- V05 pixels：YUV→RGB565和必要显示适配，复用经过验证的NEON/格式契约；不使用逐像素Python。尽量融合补全与转换，减少全帧内存往返。','- V05 pixels：YUV到必要显示格式适配，当前HDMI为XRGB8888/64-bit AXIS；RGB565仅为可能的中间格式。复用经过验证的NEON/格式契约，不使用逐像素Python；尽量融合补全与转换，减少全帧内存往返。')
replace('ARCHITECTURE.md','现有native owner只证明ADC诊断流的只读租约、持有不覆盖及取消重入；完整SOURCE到物理DMA/PS的联合边界和持续吞吐仍待验证，局部记录/批次仿真不能替代。','R7 native SOURCE owner已通过有限填充5/32批、STOP/重入和0.5秒双TX RF探针的1024批/86SOURCE精确交付；8用户槽加1不可映射STOP槽共288KiB DDR。持续吞吐及DDR/HDMI/视频并发仍待验证，有限探针和局部仿真不能替代。')
replace('ARCHITECTURE.md','已包含于50RTL主链','已包含于当前整板主链')
a=texts['ARCHITECTURE.md'].index('PL当前规划软目标LUT')
b=texts['ARCHITECTURE.md'].index('\n',a)
texts['ARCHITECTURE.md']=texts['ARCHITECTURE.md'][:a]+'PL当前规划软目标LUT≤85%（45,220），FF≤65%、BRAM≤70%、DSP≤75%保持；旧LUT70%为历史对照。R7同口径整板综合48467/45599/74/154，实际布线47125/45498/74/154；95575网零路由错误、WNS/WHS+0.173/+0.015ns。继承外设I/O缺项及两条CDC-10根仍待生产签署。'+pending+'阶段二另列未实例化估算2500/2000/2/8，R8综合加估算51349/47595/76/162，物理LUT余1851，完整预算未通过。诊断关闭/相关及RRC历史BRAM映射的实际整板净收益和剩余功能见README第6节；局部毛成本和已含收益不重复扣减。旧50RTL分项和51372 LUT与52RTL丢1的报告保留为历史，不能当当前状态。'+texts['ARCHITECTURE.md'][b:]
replace('ARCHITECTURE.md','目前只有有限队列/实采RTL回放/局部OOC/native显示子条件，完整G0～G7未通过，不能由局部结果提前勾选。',status+'R8新增窗口寿命修正的离线回归通过，整板路由及新候选实板尚待完成；完整G0～G7未通过，不能由局部结果提前勾选。')
a=texts['RF_PROTOCOL.md'].index('验收另须覆盖固定8-bank队列')
b=texts['RF_PROTOCOL.md'].index('\n',a)
texts['RF_PROTOCOL.md']=texts['RF_PROTOCOL.md'][:a]+'验收另须覆盖固定8-bank队列/突发/截止与PHY/DMA联合持续运行，丢包/CRC/失锁后继续接收、自动恢复并计花屏/冻结，必要安全边界仍有效。'+status+pending+'窗口寿命策略属于未冻结G0候选，不改变字段布局和每块RS(20,32)/S512能力，不增加逐坏包必修复或整帧完整门槛。2×2实时1024×600/20新源fps、约3秒帧龄、至少30%可靠吞吐裕度和README G4～G7仍待验证，完整预算未通过。资源按README第6节与ARCHITECTURE第5节区分实际综合/布线、未完成核心和阶段二估算，新增项单列，可选诊断/阶段二不作为核心视频共同阻塞项。'+texts['RF_PROTOCOL.md'][b:]
for n,t in texts.items():
 (out/n).write_text(t)
# Wire tables must remain byte-identical; only status prose changes in RF_PROTOCOL.
old=(src/'RF_PROTOCOL.md').read_text()
assert [l for l in old.splitlines() if l.startswith('|')]==[l for l in texts['RF_PROTOCOL.md'].splitlines() if l.startswith('|')]
receipt={'base_commit':'733a3e061d1094d24e8b05b656d7e59f12599027','paths':list(texts),'wire_tables_unchanged':True,'core_targets_unchanged':True,'route_R8_pending':True,'new_R8_hardware_not_claimed':True,'sha256':{n:hashlib.sha256(t.encode()).hexdigest() for n,t in texts.items()}}
(out/'draft-checks.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
