**第二版已发布：[查看 V2 代码与使用说明](acoustic_competition_v2/README.md)。根目录保留第一版；V1 与 V2 不能互通，两台机器人需要使用同一版本。**

# Python 双机器人声波通信 单文件版

把 `acoustic_comm.py` 复制到两台机器人或电脑上，即可使用扬声器和麦克风交换短消息。该文件包含五个通信类、命令行入口和离线测试，没有对上一版其他 Python 文件的依赖。

适用于先验证“到位、请求物料、交接完成”等任务状态通信。程序不控制电机或夹爪；收到一条通信消息，不等于机器人已完成动作。

## 1 文件说明

```text
acoustic_single/
├── acoustic_comm.py    五个类、运行入口、自测代码
└── README.md           本说明
```

单文件不等于不需要第三方库：还需要安装 NumPy、SciPy 和 sounddevice。

## 2 相比上一版的改进

- 五个类和运行示例合并，可直接运行，也可作为模块导入。
- 消息在创建时统一检查类型、范围和编码，避免错误参数进入发送队列；会话号统一为小写。
- 修正多包同时输入时可能优先锁定后面强信号、漏收前面消息的问题。
- 链路解调时接入 CRC 校验。损坏或截断包校验失败后保留其后的采样并重新找帧，减少吞掉后续完整消息的情况。
- 缓存固定匹配模板和窗函数，减少解调时的重复计算。
- 音频读取仅取当前已有数据；增加音频上下文管理，退出时关闭设备。
- 解调后刷新真实计时，避免计算耗时侵占收发切换保护时间。
- 支持命令行指定消息内容和任务号，并报告校验失败、音频不连续计数。
- 自测直接集成进同一个文件，运行一条命令即可验证。

基本扫频参数和正常报文的线格式沿用上一版，没有通过提高速率来换取表面上的性能提升。建议两端都更新为本版。

## 3 环境与安装

推荐 Python 3.10 或更新版本。每台设备需要麦克风和扬声器，支持 48000 Hz、单声道输入及输出。

在这两个文件所在的目录打开终端：

```bash
python -m pip install "numpy>=1.26,<3" "scipy>=1.12,<2" "sounddevice>=0.5,<0.6"
```

Windows 若找不到 `python`，可以把命令中的 `python` 换成 `py -3`。Linux 如果提示缺少 PortAudio，Ubuntu/Debian 常见安装命令为 `sudo apt install libportaudio2`，其他发行版请使用对应包管理器。

## 4 先运行离线自测

```bash
python acoustic_comm.py --self-test
```

自测不打开麦克风，也不播放声音。成功时末尾显示 `OK`，退出码为 0；失败时退出码非 0。导入模块不会自动运行自测或启动音频。

## 5 在两台设备运行

先列出各自的音频设备：

```bash
python acoustic_comm.py --list-devices
```

TR 端启动：

```bash
python acoustic_comm.py --role tr
```

BR 端启动：

```bash
python acoustic_comm.py --role br
```

两端默认各发送一次 `READY` 消息，随后继续接收和轮询。TR 发起通信；BR 在 TR 轮询时发送自己的待发消息，因此两端必须同时保持程序运行，而且只能有一个 TR 发起端。

如果默认设备不正确，例如本机麦克风编号为 1、扬声器编号为 3：

```bash
python acoustic_comm.py --role tr --input-device 1 --output-device 3
```

这里的 1 和 3 只是示例，必须以本机设备列表为准。

发送自定义内容：

```bash
python acoustic_comm.py --role tr --message-type READY --task-id 100 --text "塔基，数量1"
python acoustic_comm.py --role br --message-type NEED --task-id 100 --text "请送塔顶"
```

这两条分别在对应设备上运行，不是在一台电脑上同时运行。命令行模式每次启动只排队一条业务消息；需要持续发送多条消息时，使用下方的 Python 调用方式。

按 `Ctrl+C` 退出。第一次测试建议两台设备相距约 0.5～1 米、保持静止、使用适中音量，先确认接收内容正确且发送状态出现 `acknowledged`。如果一端启动过晚导致发送失败，可重启该端重新进行演示。

## 6 五个类的职责与接口

| 类 | 主要接口 | 作用 |
| --- | --- | --- |
| `AcousticMessage` | 构造函数 | 保存并校验消息，创建后不可修改 |
| `AudioIO` | `start()`、`play(samples)`、`read_samples()`、`close()`、`busy` | 采集与播放音频，也支持 `with AudioIO() as audio` |
| `ChirpModem` | `modulate(packet)`、`feed_samples(samples, validator=None)`、`reset()`、`duration(n)` | 字节和声音转换，流式同步及恒定多普勒补偿 |
| `PacketCodec` | `encode(message)`、`decode(packet)` | 二进制打包与 CRC32 校验 |
| `ReliableAcousticLink` | `send()`、`tick()`、`receive()`、`status()` | 排队、轮流发送、确认、重传和去重 |

通常业务程序只调用 `ReliableAcousticLink`。它自动组织另外四个类，不需要手工完成每一步。

发送路径：业务程序 → 消息对象 → 打包校验 → 扫频调制 → 扬声器。

接收路径：麦克风 → 扫频解调 → 拆包校验 → 去重及确认 → 业务程序。

### Python 调用示例

把你的主程序与 `acoustic_comm.py` 放在同一目录。下面是 TR 端的完整示例；不要把自己的主程序也命名为 `acoustic_comm.py`。

```python
import time
from acoustic_comm import AudioIO, ChirpModem, ReliableAcousticLink

try:
    with AudioIO() as audio:
        link = ReliableAcousticLink(
            local_id=1,
            peer_id=2,
            audio=audio,
            modem=ChirpModem(),
            initiator=True,
        )

        sequence = link.send(
            "READY",
            task_id=100,
            payload="塔基，数量1".encode("utf-8"),
        )
        last_status = None

        while True:
            link.tick()

            status = link.status(sequence)
            if status != last_status:
                print("发送状态：", status)
                last_status = status

            message = link.receive()
            while message is not None:
                print("收到：", message.message_type, message.task_id, message.payload)
                # 在这里交给你的任务状态机处理。
                # 夹取或释放动作，还必须检查任务号、当前阶段和本机传感器。
                message = link.receive()

            # 在业务需要时，可以再次调用link.send()发送下一条消息。
            time.sleep(0.01)
except KeyboardInterrupt:
    print("已退出")
```

BR 端改为 `local_id=2, peer_id=1, initiator=False`。`send()` 自动生成消息对象、会话号和序号，返回用于查询状态的消息序号。

`tick()` 必须持续调用，包括没有业务消息时。四个链路方法应在同一主线程调用。函数不等待播放和远端回复，但解调本身需要 CPU 时间，不是硬实时电机控制接口。

### 消息字段

| 字段 | 要求 |
| --- | --- |
| `sender_id`、`receiver_id` | 0～65535 的整数；链路两端编号不同 |
| `session_id` | 32 位十六进制字符串，通常为 `uuid4().hex` |
| `sequence` | 0～4294967295 的整数；重传不改变序号 |
| `message_type` | 1～24 个不含空白的可打印 ASCII 字符 |
| `task_id` | 0～4294967295 的整数，由业务程序指定 |
| `payload` | 最多 64 字节的 `bytes`，中文须先 UTF-8 编码 |

以下划线开头的类型保留给协议。编号不是身份认证；同场多队应分配不同的编号，在两端使用相同的 `--tr-id` 和 `--br-id` 配置。编号过滤不会消除声波互相干扰。

### 发送状态的含义

| 状态 | 含义 |
| --- | --- |
| `queued` | 已排队，尚未获得发送机会 |
| `waiting_ack` | 已提交发送，等待确认或准备重传 |
| `acknowledged` | 已收到对方的通信确认 |
| `failed` | 已达到发送次数上限，仍未收到确认 |
| `None` | 查询的序号不存在 |

默认每条业务消息最多发送 3 次，包含首次发送。TR 每完成一次自己的业务发送后，给 BR 一次轮询机会，避免自己的队列持续占用链路。

**确认消息不等于确认动作。** BR 若夹取完成，应再发送一条例如 `PICKED` 的业务消息。`failed` 也不代表对方一定没收到：可能只是 ACK 丢失。因此失败后应重新核对任务状态，不能直接换任务号重复释放物料。

BR 一直收不到轮询时，待发消息保持 `queued`；业务程序应另设任务等待超时。去重信息保存在当前进程内，重启后需要重新同步任务，不能依靠通信类保证跨重启的动作只执行一次。

## 7 参数与实际性能边界

| 项目 | 默认值 |
| --- | --- |
| 采样率 | 48000 Hz |
| 扫频范围 | 4000～10000 Hz，可听声 |
| 每位扫频时长 | 4 ms |
| 位间保护间隔 | 1 ms |
| 同步前导 | 48 ms |
| 播放结束后的接收屏蔽 | 100 ms |
| 多普勒时间伸缩搜索 | 0.98～1.02 |

原始速率约 200 bit/s，协议头、同步和确认会进一步降低有效吞吐。`READY` 携带两字节内容时发声约 1.968 秒，ACK 约 1.848 秒；加上切换，一次确认约 4 秒以上。BR 等待轮询或发生重传时会更久。适合低频任务状态交换，不适合连续传递位置控制指令。

如需调整频段或符号时长，可修改两端 `ChirpModem(...)` 的对应参数。两端调制参数必须一致；若改变采样率，`AudioIO` 也必须同步修改。更短的符号可能提高速率，但需要重新测量误码和回声影响，不能直接视为性能改善。

多普勒补偿只适用于一个包内近似恒定的时间伸缩，不包含包内加速度跟踪、自适应多径均衡或前向纠错，也不是 LoRa 协议。搜索范围不是已验证的机器人移动速度保证。

接收损坏帧后可以重新寻找后续前导，但如果旧帧声明的长度尚未收齐，仍可能等待更多录音样本后才恢复。因此仍需要重传和业务超时，不能保证每次截断都立即恢复。

## 8 排查问题

| 现象 | 检查内容 |
| --- | --- |
| 缺少模块 | 用运行程序的同一个 Python 执行依赖安装命令 |
| 无音频设备、无法打开设备 | 检查麦克风权限、设备编号、设备是否被独占，以及是否支持 48kHz |
| 只有 `queued` | 检查是否一端 TR、一端 BR；TR 是否持续运行 `tick()` |
| 一直 `waiting_ack`，最后 `failed` | 检查另一端是否运行、两端编号是否对应、音量、距离和扬声器朝向 |
| `audio_discontinuities` 增加 | 录音/播放发生溢出、欠载或主循环处理过慢 |
| `crc_or_packet_errors` 增加 | 有帧被解出但校验失败，检查噪声、削波、回声和运动 |
| 接收成功但没有机械动作 | 此程序只提供通信，需要业务程序接入电机/夹爪控制 |

两项错误计数不是全部丢包数，完全未检测到的声音不会被记作 CRC 失败。

## 9 验证范围

本版本包含 23 项离线测试，覆盖：消息字段与 CRC、分段接收、多包顺序、噪声、单路回声、恒定多普勒、截断帧后恢复、音频回调、双向轮询、ACK 丢失、去重、旧 ACK 拒绝、会话区分和发送次数限制。

声学模拟包含固定种子的随机噪声、4ms 延迟且幅度为直达声20%的单路回声，以及若干约±1%的时间伸缩。协议故障注入部分使用模拟音频设备；另有使用实际生成波形的双向流程测试。

**这些结果不等于实机或赛场验收。** 尚未验证实际声卡、机器人电机运行、遮挡、多队同时发声或比赛场馆混响。实际部署时应测量成功率、误触发和含重传的端到端延迟。

## 10 API参考

- [sounddevice音频流](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)
- [SciPy相关检测](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html)
- [SciPy重采样，自测使用](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html)
