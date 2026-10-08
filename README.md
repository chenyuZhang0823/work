# 女娲补天双机器人声波通信 · 模块版

使用扬声器和麦克风在 TR、BR 间交换短状态消息。项目版本 **2.1.0**，声波协议仍为 **V2**。代码按职责拆分，使用 **uv** 管理环境，保留原来的六个类与公开接口。

目录组织参考 [WallyHao/chirp-comm](https://github.com/WallyHao/chirp-comm)，通信算法和比赛交接逻辑沿用自己的第二版。V2 与 V1 不能互通；通信确认不等于机械动作完成。当前验证范围是离线模拟，尚未完成机器人及赛场实测。

## 1. 项目结构

```text
acoustic_competition_v2_modular/
├── acoustic_comm/          # 运行代码
│   ├── __init__.py         # 统一公开导入接口
│   ├── __main__.py         # python -m 启动
│   ├── config.py           # 消息类型和协议保留类型
│   ├── message.py          # AcousticMessage
│   ├── codec.py            # PacketCodec
│   ├── audio_io.py         # AudioIO
│   ├── modem.py            # ChirpModem
│   ├── link.py             # ReliableAcousticLink
│   ├── transfer.py         # TransferCoordinator
│   ├── cli.py              # 命令行、演示循环和退出处理
│   └── selftest.py         # 兼容源码 --self-test 的小入口
├── tests/                  # 37 项离线测试
│   ├── __init__.py
│   ├── helpers.py          # 模拟通道、设备、时钟与共享工具
│   ├── test_codec.py
│   ├── test_audio_io.py
│   ├── test_modem.py
│   ├── test_link.py
│   └── test_transfer.py
├── examples/
│   ├── packet_roundtrip.py # 离线消息→声音→消息
│   └── robot_link.py       # TR/BR 主程序接入示例
├── docs/
│   ├── ARCHITECTURE.md     # 模块职责、依赖与迁移说明
│   └── USAGE.md            # API、交接、比赛场景和调试
├── pyproject.toml
├── MANIFEST.in             # 源码分发包含测试、示例与文档
├── uv.lock
├── .python-version
├── .gitignore
├── README.md
├── CONTRIBUTING.md
└── CHANGELOG.md
```

## 2. 安装与离线验证

先按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv，确认 `uv --version` 可运行。进入本项目目录，也就是包含 `pyproject.toml` 的目录：

```bash
uv sync --locked
uv run --locked pytest
uv run --locked python examples/packet_roundtrip.py
```

项目声明支持 Python 3.10 起，本次使用 `.python-version` 指定的 **Python 3.12.14**。uv 会按需下载解释器并创建 `.venv`，无需手动激活。首次安装通常需要联网，请在赛前于两台设备分别安装，不要跨机器复制 `.venv`。

`--locked` 要求依赖配置与锁文件一致，防止运行时悄悄改变锁定版本。Linux 上真实音频可能还需要安装 PortAudio 系统库；uv 不管理声卡驱动或系统库。

原来的源码自测命令仍可用，执行同一批 37 项测试：

```bash
uv run --locked acoustic-comm --self-test
```

## 3. 两台设备收发

各自在设备上先查询麦克风和扬声器编号：

```bash
uv run --locked acoustic-comm --list-devices
```

TR 端运行：

```bash
uv run --locked acoustic-comm --role tr --message-type READY --task-id 100 --text "TR到位"
```

BR 端运行：

```bash
uv run --locked acoustic-comm --role br --message-type STATE --task-id 100 --text "BR就绪"
```

需要手动选择音频设备时，加上 `--input-device 1 --output-device 3`，数字须替换为本机查询结果。两端都保持程序运行，按 Ctrl+C 结束；每次启动只发送一条演示消息，随后持续接收和轮询。首次调试建议静止、安静、相距 0.5～1 米，设备支持 48kHz 单声道。

也可使用模块入口，例如：

```bash
uv run --locked python -m acoustic_comm --role tr
```

这两种启动方式使用相同的参数解析和通信实现。

## 4. 在自己的程序中调用

原有公开导入不变：

```python
from acoustic_comm import (
    AcousticMessage,
    PacketCodec,
    AudioIO,
    ChirpModem,
    ReliableAcousticLink,
    TransferCoordinator,
)
```

在项目中创建自己的机器人主程序，再用 `uv run --locked python 你的主程序.py` 运行。接入示例位于 `examples/robot_link.py`：

```bash
uv run --locked python examples/robot_link.py --role tr
uv run --locked python examples/robot_link.py --role br
```

这两条分别在对应设备执行。示例展示音频生命周期、排队、发送状态、链路事件和收消息；实际机构动作仍由业务程序结合传感器执行。

详细的消息类型、优先级、TTL、重试恢复、交接检查器接口见 [使用说明](docs/USAGE.md)；需要修改某项功能时，先查看 [模块职责](docs/ARCHITECTURE.md)。

## 5. 开发检查

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

Ruff 启用 F 类错误检查和 I 类导入顺序检查，格式单独检查。只运行某一部分时，例如 `uv run --locked pytest tests/test_link.py`。开发约定见 [CONTRIBUTING](CONTRIBUTING.md)。

修改依赖使用 `uv add 包名` 或 `uv add --dev 包名`，同时提交更新后的 `pyproject.toml` 与 `uv.lock`，再运行验证。比赛期间避免临时升级依赖。`.venv`、缓存、构建产物不提交。

机器人不需要开发工具时，可使用 `uv sync --locked --no-dev` 安装，以及 `uv run --locked --no-dev acoustic-comm --role tr` 运行。pytest 和 Ruff 需要开发依赖。

## 6. 从单文件版迁移

- 下载完整模块版，运行 `uv sync --locked`，不再只复制一个 `.py` 文件。
- 类名、方法参数和 `from acoustic_comm import ...` 保持不变。
- `python acoustic_comm.py ...` 改为 `uv run --locked acoustic-comm ...`。
- 项目版本 2.1.0 是文件组织变化；V2 协议、消息类型和波形参数保持不变。
- 避免在同一导入路径放置旧的 `acoustic_comm.py`，以免使用到错误版本。
- `--self-test` 需要完整源码的 `tests/` 目录；用 `uv build` 生成的 wheel 仅包含运行包。

## 7. 验证范围

现有 37 项测试覆盖编码校验、音频回调、流式同步、多普勒模拟、丢包重传、去重、优先级、过期、重试恢复和交接状态约束。测试中的 10 个 subtest 参数子情况不另外计入 37 项。

测试无需麦克风或扬声器，不证明赛场距离、误码率或移动速度。电机噪声、朝向、遮挡、其他队伍声源，以及机械传感器联调仍需实测。更新记录见 [CHANGELOG](CHANGELOG.md)。
