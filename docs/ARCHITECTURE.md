# 模块职责与依赖

本次参照 [chirp-comm](https://github.com/WallyHao/chirp-comm) 的“运行包 / tests / examples / 项目配置”组织方式拆分自己的代码。没有复制其协议实现，没有改变现有 V2 线格式。

## 到哪里修改

| 文件 | 负责内容 | 主要接口 |
| --- | --- | --- |
| `acoustic_comm/config.py` | 消息类型编号、协议保留类型、状态类型 | MESSAGE_TYPES、CONTROL_TYPES、STATE_TYPES |
| `acoustic_comm/message.py` | 消息数据与整数、载荷等字段校验 | AcousticMessage |
| `acoustic_comm/codec.py` | 二进制打包、拆包、CRC32、版本校验 | PacketCodec |
| `acoustic_comm/audio_io.py` | 录音播放、缓存、音频流关闭、尾音屏蔽 | AudioIO |
| `acoustic_comm/modem.py` | 生成扫频、前导同步、分块解调、多普勒比例搜索 | ChirpModem |
| `acoustic_comm/link.py` | 握手、半双工收发、确认、重试、去重、优先级和过期 | ReliableAcousticLink |
| `acoustic_comm/transfer.py` | 直接交接状态及传感器确认条件 | TransferCoordinator |
| `acoustic_comm/cli.py` | 参数解析、演示主循环、异常和退出码 | main、entrypoint |
| `acoustic_comm/__init__.py` | 公开导入接口 | from acoustic_comm import ... |
| `acoustic_comm/__main__.py` | Python 模块启动入口 | python -m acoustic_comm |
| `acoustic_comm/selftest.py` | 兼容旧的源码自测命令，发现 tests 中的测试 | run_self_tests |

默认采样率、频率等构造参数仍在其所属类中，避免为拆文件而改动参数来源和函数签名。`config.py` 只收纳原有共用协议常量。

## 调用关系

```text
机器人业务程序 / examples / cli
              │
              ├── TransferCoordinator（可选交接检查）
              │             │
              └── ReliableAcousticLink
                    ├── AcousticMessage + PacketCodec
                    ├── ChirpModem
                    └── AudioIO

tests ── 模拟设备 / 模拟时钟 ── 各运行模块
```

消息和编解码模块不导入音频模块。链路通过传入的 audio 和 modem 对象工作；交接检查器通过传入的 link 工作。它们不自行启动电机或夹爪。包根目录提供便利导出，因此 `import acoustic_comm` 仍会间接加载 NumPy/SciPy；本次没有另加延迟导入机制。

## 测试拆分

| 文件 | 测试内容 |
| --- | --- |
| `tests/test_codec.py` | 字段、短报文、CRC、旧版本拒绝 |
| `tests/test_audio_io.py` | 回调搬运、播放期间屏蔽、采集不连续 |
| `tests/test_modem.py` | 分块、多包、噪声、回声、多普勒、坏帧恢复 |
| `tests/test_link.py` | 握手、双向消息、重试、去重、队列与会话恢复 |
| `tests/test_transfer.py` | 交接时序、载荷、过期许可、暂停与重试 |
| `tests/helpers.py` | 样例消息、模拟通道、模拟音频、共享测试方法 |

原有 TransferTests 继承 LinkTests 再由手工 suite 排除继承的测试。迁移后两者只共用不含测试方法的 LinkTestMixin，pytest 和 unittest 都收集原本的 37 项，不重复执行 LinkTests。

## 兼容范围

- 项目版本升至 2.1.0；报文协议仍为 V2。六个运行类的逻辑和公开参数保持不变。
- `from acoustic_comm import AcousticMessage, AudioIO, ChirpModem, PacketCodec, ReliableAcousticLink, TransferCoordinator` 保持可用。
- 原 `python acoustic_comm.py ...` 改用 `uv run --locked acoustic-comm ...` 或 `uv run --locked python -m acoustic_comm ...`。
- 迁移业务程序时复制完整项目并执行 uv 安装，不能只复制某一个模块文件；不要在同一导入路径里另放旧的 `acoustic_comm.py`。
- `--self-test` 用于带有 tests 目录的完整源码项目。安装 wheel 后的生产环境不附带测试，命令会明确提示使用源码项目进行验证。

这次整理不引入新的纠错算法、线程调度或动作控制。文件职责清楚以后，再根据目标设备的实测结果修改对应模块。
