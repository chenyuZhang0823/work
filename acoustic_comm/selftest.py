"""兼容原 --self-test 命令，测试实现独立放在源码项目的 tests 目录。"""

import unittest
from pathlib import Path


def run_self_tests():
    """执行源码目录内的离线测试；wheel 安装不包含开发测试。"""
    root = Path(__file__).resolve().parents[1]
    test_dir = root / "tests"
    if not (test_dir / "test_codec.py").is_file():
        raise RuntimeError("未找到源码测试，请在完整项目目录执行 uv run --locked pytest")
    suite = unittest.defaultTestLoader.discover(
        str(test_dir), pattern="test_*.py", top_level_dir=str(root)
    )
    if suite.countTestCases() == 0:
        raise RuntimeError("没有发现离线测试，请检查 tests 目录")
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
