# 女娲补天双机器人声波通信 V2

本版针对3分钟比赛中的短状态消息、物料交接和重试恢复优化。保留五个通信类，增加可选的 `TransferCoordinator` 交接检查器；所有代码、命令行和离线测试均在一个 Python 文件内。

**两台机器人必须同时更新为 V2。V2 与之前版本不能互通。** 目前通过的是离线模拟测试，尚未在真实机器人和比赛场馆验证。

## 1 文件与安装

```text
acoustic_competition_v2/
├── acoustic_comm.py
└── README.md
```

Python 3.10 或更新版本。在文件所在目录打开终端，安装依赖：

```bash
python -m pip install "numpy>=1.26,<3" "scipy>=1.12,<2" "sounddevice>=0.5,<0.6"
python acoustic_comm.py --self-test
```

自测不打开麦克风、不播放声音，成功时显示 `OK`。Windows 找不到 `python` 时，可改用 `py -3`。Linux 上 sounddevice 还可能需要系统提供 PortAudio 运行库。

## 2 先在两台设备测试

各自在本机查看音频设备：

```bash
python acoustic_comm.py --list-devices
```

第一台设备运行 TR，第二台设备运行 BR：

```bash
python acoustic_comm.py --role tr --message-type READY --task-id 100 --text "TR到位"
```

```bash
python acoustic_comm.py --role br --message-type STATE --task-id 100 --text "BR就绪"
```

程序先自动握手，再各发送一条业务消息，随后保持通信。终端会显示连接事件、收到的消息和发送状态，按 Ctrl+C 结束。

需要指定设备时使用 `--input-device 1 --output-device 3`，其中编号必须替换成本机设备列表中的真实编号。设备应支持48kHz单声道采集和播放。初次测试建议安静、静止、相距0.5至1米，使用适中音量。

命令行用于演示收发，不会实际控制夹爪，也不证明机器人已经到位。用于机器人主程序时，按下面的方法调用类，并接入实际传感器。

## 3 这一版改进了什么

| 改进 | 比赛中的作用 |
| --- | --- |
| 14字节固定头，消息类型用1字节编号 | 降低短消息的固定开销 |
| 确认序号随业务报文携带 | BR可以同时确认TR消息并报告自己的状态 |
| 自动建立新会话 | 重试或进程重启后，重新区分新旧任务消息 |
| 优先级、发送有效期、状态替换 | 减少重要交接信息排在过时库存信息后面的情况 |
| 取消任务和重试接口 | 撤销旧任务的未完成发送，要求重新核对现场状态 |
| 交接状态检查器 | 由本机传感器确认到位、夹稳、释放，避免把通信ACK当成机械动作完成 |
| 阶段超时和短期释放许可 | 迟到或过时的信息不会持续保留释放许可 |

扫频仍为4～10kHz、4ms扫频加1ms间隔，保留前导检测、多普勒恒定比例补偿和CRC32校验。本次主要节省协议开销，没有通过缩短符号来冒充已验证的声学性能改善。

### 发声时长对比

| 报文 | 上一版 | V2 |
| --- | ---: | ---: |
| READY加2字节数据 | 44字节 / 1.968秒 | 20字节 / 1.008秒 |
| 无业务内容的确认/回复 | ACK为41字节 / 1.848秒 | IDLE为18字节 / 0.928秒 |

以上按代码波形长度计算，包括声学前导和4字节物理帧头，不是实机测速。

会话建立后，一条2字节业务消息加空回复的发声总时长从3.816秒降到1.936秒；加上约0.25秒的回复保护时间，理想单次确认约2.2秒，实际还受设备延迟、解调耗时、排队和重传影响。若BR也有业务信息，这次回复可直接携带它；BR消息的确认再由下一条TR请求携带。

首次握手另有开销：HELLO发声约1.568秒，WELCOME约2.208秒，再加切换时间。比赛开始前是否允许预先建立会话，应服从现场规则；不能假定握手时间一定能从比赛计时中扣除。

## 4 五个通信类

| 类 | 作用 |
| --- | --- |
| `AcousticMessage` | 不可变消息及字段检查 |
| `PacketCodec` | V2短包编码、解码和CRC32 |
| `AudioIO` | 单声道采集、播放、发送尾音屏蔽，支持with语句 |
| `ChirpModem` | 扫频调制、流式解调、恒定多普勒补偿 |
| `ReliableAcousticLink` | 握手、轮流收发、确认、重传、队列管理和重试同步 |

`TransferCoordinator` 是可选的业务层检查器，不替代定位、视觉、夹爪控制或裁判信号检测。

## 5 在自己的Python程序里调用

将自己的主程序与 `acoustic_comm.py` 放在同一目录，不要将自己的主程序也命名为 `acoustic_comm.py`。下面是能直接运行的TR通信示例：

```python
import time
from acoustic_comm import AudioIO, ChirpModem, ReliableAcousticLink

try:
    with AudioIO() as audio:
        link = ReliableAcousticLink(
            local_id=1, peer_id=2,
            audio=audio, modem=ChirpModem(), initiator=True,
        )
        sent = False
        while True:
            link.tick()
            if link.connected and not sent:
                # 示例数据：物料类型0、数量2。实际应由传感器/任务状态生成。
                sequence = link.send(
                    "READY", task_id=100, payload=bytes([0, 2]),
                    priority=2, ttl=5,
                )
                print("已排队，序号", sequence)
                sent = True

            event = link.receive_event()
            while event is not None:
                print("连接事件", event)
                # local_retry/peer_retry出现时，先撤销动作许可并核对现场。
                # 不要在这里直接重发旧任务。
                event = link.receive_event()

            message = link.receive()
            while message is not None:
                print(message.message_type, message.task_id, message.payload)
                message = link.receive()
            time.sleep(0.01)
except KeyboardInterrupt:
    print("已退出")
```

BR改用 `local_id=2, peer_id=1, initiator=False`。两端都可以调用 `send()`；只有TR主动开启发送回合，BR只在收到请求后应答。所有链路方法应由同一线程调用，持续运行 `tick()`。

`tick()`不等待声音播放或远端回复，但解调仍会消耗CPU时间，不能作为硬实时电机控制循环使用。

### 常用接口

```python
# 这些是调用片段，需要先按上面的示例创建link。
seq = link.send("GRIPPED", task_id=100, payload=bytes([0, 2]), priority=3, ttl=5)
state = link.status(seq)
message = link.receive()          # 没有消息时返回None
event = link.receive_event()      # 没有连接事件时返回None
link.cancel_task(100)             # 只取消本机未完成的发送
```

`send()`参数：

- `priority`：0～3，数字越大越优先。建议库存0、一般状态1、交接准备2、夹稳/释放/暂停3。
- `ttl`：排队或重传前允许等待的秒数，默认10秒，`None`表示不设此期限。它不是接收端的动作有效期。
- `latest_only=True`：替换同类型、同任务号的未完成旧状态，适合库存和需求；不用于“已经释放”这类事件。

例如：

```python
seq = link.send(
    "INVENTORY", task_id=100, payload=bytes([0, 2, 1]),
    priority=0, ttl=3, latest_only=True,
)
```

这三个字节可由两端约定为物料类型、数量、存放位置。通用链路不自行解释载荷；与交接检查器一起用时，交接消息必须使用它生成的两字节格式。

高优先级消息只能在下一次合法发送机会优先发送，不能打断正在播放的声波或对方的应答窗口。队列最多32条，满时抛出 `BufferError`；避免连续排入无用的状态快照。

### 消息状态

| 状态 | 含义 |
| --- | --- |
| queued | 等待连接或发送机会 |
| waiting_ack | 已提交发送，等待确认或重试 |
| acknowledged | 已收到通信确认 |
| failed | 已达到发送次数限制，未收到确认 |
| expired | 等待或重试期限已到，不再开始后续发送 |
| cancelled | 任务取消或会话重置 |
| superseded | 被同类型、同任务号的新状态替换 |

`failed`、`expired`、`cancelled`或`superseded`都不能撤回已经发出的声音，也不能证明对方尚未收到或执行。接收端仍需核对当前任务与动作阶段，不能把未收到ACK解释为“什么都没发生”。默认业务消息最多发送3次，握手会持续尝试建立连接。

## 6 重试后如何恢复

规则V0第5.2条要求重试时重新处理机器人载荷，已正确放置的物料可能保留，比赛计时也不会暂停。因此重试后应重新观察现场，不能直接延续旧交接任务。

在本机已经安全停止、进入获准重试流程后调用：

```python
link.reset_for_retry()
```

这会取消本机未完成任务、清空尚未交付的接收消息、改变本地任务代数 `generation`，并重新建立会话。远端在收到后续握手/复位交流后同样取消旧任务并发出 `peer_retry` 事件。原任务不会自动重新排队。

恢复顺序：安全停机 → 重试归位 → 调用重试接口 → 等待重新连接 → 重新核对载荷/存放区/塔状态 → 分配新任务号 → 开始新任务。

**远端不会在本机调用该函数的瞬间就获知重试。** 已经在传播中的消息也无法撤回。两机停止、人员接近机器人和比赛结束时的停车，必须依靠符合规则的独立安全措施，不能等待声波确认。

内部握手交换两端各16字节随机数，确认后以32位会话标识发送短包。会话匹配和CRC用于区分正常新旧报文与传输错误，不是密码学身份认证。

## 7 直接交接检查器

用于TR将物料直接交给BR的场景，约束来自V0第4.4.2、4.4.3条。两端分别创建：

```python
from acoustic_comm import TransferCoordinator

transfer = TransferCoordinator(link)

# 连接成功、核对载荷后，两端约定同一task_id、物料类型、数量。
# 0=塔基/中段，1=塔顶，2=五色石。
if link.initiator:
    transfer.begin(100, item_type=0, count=2, local_load=2)
else:
    transfer.begin(100, item_type=0, count=2, local_load=0)
```

这里的载荷数字只是示例，实机必须来自当前传感器或已确认库存。TR载荷上限3件；BR接收后上限2件。一次直接交接批次不超过2件，五色石每批1件。检查器采用这一保守容量限制；它不裁定规则没有写清的五色石混载情况。

每次主循环先 `link.tick()`，再把 `link.receive()` 取出的消息逐条交给 `transfer.update(message)`；即使没有消息，也应调用 `transfer.update()` 检查超时和重试事件。

双方由本机定位确认交接区边界条件后调用 `transfer.arrived()`。之后：

| 本机情况 | 调用顺序 |
| --- | --- |
| BR允许开始夹取 | 检查 `can_grip` → `begin_grip()` → 由现有控制程序夹取 |
| BR传感器确认夹稳 | `confirm_gripped()`，自动发送GRIPPED |
| TR允许开始释放 | 检查 `can_release`，再次核对本机在位与物料状态 → `begin_release()` → 控制机构释放 |
| TR传感器确认已经释放 | `confirm_released()`，自动发送RELEASED |
| BR状态为verify_received且传感器确认可靠接收 | `confirm_received()`，自动发送RECEIVED |
| 位置失效、夹持丢失或需要暂停 | 本机立即停止相应动作，并调用 `hold()` |

这些方法不执行任何电机操作。`begin_grip()`、`begin_release()`标记开始动作，确认方法只能在真实传感器证实完成后调用，不要把它们写成没有检查的连续调用。

默认阶段超时10秒，GRIPPED被接收后的“开始释放”许可有效期1秒；开始释放后重新使用阶段超时。超时进入held，重试进入resync_required，不会自动恢复动作。需要结合机械机构实测调整构造参数 `stage_timeout`、`release_valid_for`。

同一会话不能重复使用已经用过的交接任务号。检查器会拒绝其他任务、旧会话和物料类型/数量不一致的消息。释放许可仍不是安全证明，必须结合本机定位、在位与夹持检查。

## 8 落地交接和五色石流程

**落地交接：**V0第4.4.3条允许TR将物料放在传递区内，随后BR拾取。此场景不使用上述手递手状态机。可约定固定位置编号，用 `PLACED`报告“已放好”，BR独立观察后拾取，再报告`RECEIVED`。通信失败时依据现场传感器重新核对，不把一条丢失消息当作物料不存在。机器人与物料的区域边界需要由定位和机构设计保证。

**五色石：**`GUARD_VALID`和`STONE_TAKEN`消息类型已预留，但取球资格必须由业务程序依据实际场上状态判断。按V0第3.6.1—3.6.2条，应在取球当时满足两座全塔且至少一座位于公共区的条件；满足条件并从基座移除后资格才永久满足。不能仅凭过时的“两塔完成”消息取球。

本文件没有视觉识别塔顶颜色、判断对手翻转或检测裁判终场信号的能力，也没有接入你的运动控制程序。这些条件不能由通信模拟测试替代。比赛结束立即停止，应在本机控制层独立落实。

## 9 V1迁移到V2

| 项目 | V2要求 |
| --- | --- |
| 两端版本 | 必须同时更新，不兼容旧报文 |
| sender_id、receiver_id | 0～255，两端不同；可用--tr-id和--br-id指定 |
| session_id | 32位整数，由链路自动协商，不再手填UUID字符串 |
| sequence、task_id | 0～65535；业务序号不能为0，进程内不循环复用 |
| payload | 最多32字节；默认建议1～4字节状态数据 |
| message_type | 必须使用MESSAGE_TYPES中的固定名称 |
| ack_sequence | 协议自动填充，不需要业务程序维护 |

固定业务类型包括：READY、NEED、INVENTORY、PLACED、RECEIVED、CAN_RECEIVE、GRIPPED、RELEASED、TASK_DONE、HOLD、GUARD_VALID、STONE_TAKEN、STATE、TEXT。

`NEED`、`INVENTORY`、`STATE`、`GUARD_VALID`会在接收端拒绝同类型、同任务号的倒序旧状态。其他事件按会话及序号去重。同场队伍使用不同机器人编号有助于过滤误收，但不能消除声学碰撞。

## 10 验证与调试

本次37项离线测试通过，涵盖：

- 短包编码、CRC损坏检查、旧协议拒绝。
- 分段收音、多包顺序、截断后恢复、噪声/单路回声/恒定多普勒模拟。
- 真实生成波形的双向通信及完整直接交接流程。
- 丢回复、丢捎带确认后的重传和去重。
- 优先级、状态替换、有效期、队列上限、最大发送次数。
- 两端重试、进程重启、旧握手及旧业务消息隔离。
- 载荷约束、任务号复用拒绝、释放许可过期与暂停后迟到消息拒绝。

测试不使用真实音频设备。没有实测距离、误码率或移动速度保证。接收器仅补偿包内近似恒定的多普勒比例，没有包内加速度跟踪、多径均衡或前向纠错。

实机调试建议依次加入：电机运转、不同朝向、L1/L2高差、物料遮挡、现场噪声、移动、其他队伍声源。重点记录交接成功率、误触发次数和含重传的延迟分布。

`audio_discontinuities`表示音频采集或播放不连续，`crc_or_packet_errors`表示检测到的坏包，`handshake_timeouts`表示握手等待超时。它们不是完整丢包率，完全没有检测到的信号不一定进入这些计数。

参考API：[sounddevice](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)、[SciPy相关检测](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html)。
