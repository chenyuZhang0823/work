"""命令行参数、演示循环和退出处理。"""

from __future__ import annotations

import argparse
import sys
import time

from .audio_io import AudioIO
from .config import CONTROL_TYPES, MESSAGE_TYPES
from .link import ReliableAcousticLink
from .modem import ChirpModem
from .selftest import run_self_tests


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROBOCON声波通信V2：两端必须使用同一版本")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--role", choices=("tr", "br"))
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    parser.add_argument("--tr-id", type=int, default=1)
    parser.add_argument("--br-id", type=int, default=2)
    parser.add_argument(
        "--message-type", choices=sorted(set(MESSAGE_TYPES) - CONTROL_TYPES), default="READY"
    )
    parser.add_argument("--task-id", type=int, default=1)
    parser.add_argument("--text", default="")
    args = parser.parse_args(argv)
    if args.self_test:
        return 0 if run_self_tests() else 1
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0
    initiator = args.role == "tr"
    audio = AudioIO(input_device=args.input_device, output_device=args.output_device)
    link = ReliableAcousticLink(
        args.tr_id if initiator else args.br_id,
        args.br_id if initiator else args.tr_id,
        audio,
        ChirpModem(),
        initiator,
    )
    sequence = link.send(args.message_type, args.task_id, args.text.encode("utf-8"), ttl=30)
    print("先自动建立会话，再发送一条消息。ACK仅表示通信收到；本程序不操作机构。")
    print("Ctrl+C退出。重试或断电恢复后，业务程序需要重新核对任务。")
    last_status = None
    try:
        with audio:
            while True:
                link.tick()
                state = link.status(sequence)
                if state != last_status:
                    print("发送状态:", state, flush=True)
                    last_status = state
                event = link.receive_event()
                while event is not None:
                    print("链路事件:", event, flush=True)
                    event = link.receive_event()
                message = link.receive()
                while message is not None:
                    print(
                        f"收到 {message.message_type} task={message.task_id} "
                        f"payload={message.payload!r}",
                        flush=True,
                    )
                    message = link.receive()
                time.sleep(0.01)
    except KeyboardInterrupt:
        print("已退出")
    return 0


def entrypoint():
    """为控制台命令和 python -m 提供统一的错误与退出码处理。"""
    try:
        return main()
    except Exception as exc:
        print(f"运行失败：{exc}。请检查README中的依赖、设备与参数。", file=sys.stderr)
        return 1
