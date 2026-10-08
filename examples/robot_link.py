"""业务接入示例：两台设备各指定 tr/br，持续发送及接收状态。"""

import argparse
import time

from acoustic_comm import AudioIO, ChirpModem, ReliableAcousticLink


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("tr", "br"), required=True)
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    args = parser.parse_args()
    initiator = args.role == "tr"

    try:
        with AudioIO(input_device=args.input_device, output_device=args.output_device) as audio:
            link = ReliableAcousticLink(
                local_id=1 if initiator else 2,
                peer_id=2 if initiator else 1,
                audio=audio,
                modem=ChirpModem(),
                initiator=initiator,
            )
            sequence = link.send("STATE", task_id=100, payload=args.role.encode(), ttl=30)
            previous_status = None
            while True:
                link.tick()
                status = link.status(sequence)
                if status != previous_status:
                    print("发送状态：", status, flush=True)
                    previous_status = status
                event = link.receive_event()
                while event is not None:
                    print("链路事件：", event, flush=True)
                    # 重试或会话变化后，在业务层重新核对任务及本机传感器。
                    event = link.receive_event()
                message = link.receive()
                while message is not None:
                    print("收到：", message, flush=True)
                    # 在这里交给业务状态机；通信收到不等于机械动作完成。
                    message = link.receive()
                time.sleep(0.01)
    except KeyboardInterrupt:
        print("已退出")


if __name__ == "__main__":
    main()
