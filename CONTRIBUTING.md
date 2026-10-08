# 开发约定

在项目根目录执行：

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

新增依赖用 `uv add 包名`；新增开发工具用 `uv add --dev 包名`。提交源码、文档、`pyproject.toml`、`uv.lock` 和 `.python-version`，不提交 `.venv`、缓存或构建产物。

## 修改代码时

- 根据 `docs/ARCHITECTURE.md` 找到相应模块；不要在 CLI 或示例中复制通信算法。
- 通信收到与机械动作完成分别处理。机构控制接入机器人业务程序，测试不得操作真实机构。
- 协议或算法行为改变时，更新对应的测试和 `CHANGELOG.md`；纯文件移动应保持原用例和报文字节不变。
- 调整频率、符号时间、消息类型或二进制头时，检查两端兼容性，不把离线测试结果当成赛场实测。
- 共用的模拟设备放在 `tests/helpers.py`。测试类不互相继承，避免重复收集测试。

## 单独检查一部分

```bash
uv run --locked pytest tests/test_codec.py
uv run --locked pytest tests/test_link.py -k retry
uv run --locked python examples/packet_roundtrip.py
```

源码项目还支持 `uv run --locked acoustic-comm --self-test`，使用标准库 unittest 发现相同的 37 项回归用例。

如需构建分发包，可执行 `uv build`。wheel 只包含运行包，不包含 tests、examples 或开发工具；测试应在完整源码项目中运行。
