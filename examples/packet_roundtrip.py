"""离线演示：消息打包、扫频调制、分块解调，不使用音频设备。"""

from acoustic_comm import AcousticMessage, ChirpModem, PacketCodec


def main():
    message = AcousticMessage(1, 2, 123456, 1, "READY", 100, b"TR")
    codec = PacketCodec()
    waveform = ChirpModem().modulate(codec.encode(message))
    receiver = ChirpModem()
    decoded = []
    for start in range(0, len(waveform), 1024):
        for packet in receiver.feed_samples(waveform[start : start + 1024], validator=codec.decode):
            decoded.append(codec.decode(packet))
    if decoded != [message]:
        raise RuntimeError("离线往返失败")
    print("往返成功：", decoded[0])


if __name__ == "__main__":
    main()
