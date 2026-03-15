from fastcrc.crc16 import xmodem as crc16
import struct
import numpy as np
from more_itertools import chunked
from constants import *

""" Packet structure:
[0]     : START_BYTE
[1]     : FROM_ID
[2]     : TO_ID
[3]     : SEQ (0-255, wraps around)
[4]     : MSGID
[5..60] : Payload (e.g. 24 bytes, 6 floats for JOG)
[61..62]: CRC16-XMODEM of bytes [0..60]
[63]    : END_BYTE
Total: 64 bytes per packet (56-byte payload).
"""


class Packet:
    def __init__(self):
        self.data = bytearray(PACKET_SIZE)
        self.data[0] = START_BYTE
        self.data[1] = PC_ID
        self.data[2] = MASTER_ID
        self.data[-1] = END_BYTE
        self.seq = 0

    def _finalize_crc(self, pkt: bytearray) -> None:
        # CRC over bytes 0..60 (61 bytes). Store at 61..62 (MSB,LSB).
        crc = crc16(bytes(pkt[0:61]))
        pkt[61] = (crc >> 8) & 0xFF
        pkt[62] = crc & 0xFF

    def _build_packet_type_1(self, msgid: int) -> bytes:
        pkt = self.data.copy()
        pkt[3] = self.seq & 0xFF
        pkt[4] = msgid

        self._finalize_crc(pkt)
        self.seq = (self.seq + 1) & 0xFF
        return bytes(pkt)

    def ping(self) -> bytes:
        return self._build_packet_type_1(MSGID_PING)

    def pong(self) -> bytes:
        return self._build_packet_type_1(MSGID_PONG)

    def enable(self) -> bytes:
        return self._build_packet_type_1(MSGID_ENABLE)

    def disable(self) -> bytes:
        return self._build_packet_type_1(MSGID_DISABLE)

    def calibrate(self) -> bytes:
        return self._build_packet_type_1(MSGID_CALIBRATE)

    def stage(self) -> bytes:
        return self._build_packet_type_1(MSGID_STAGE)

    def park(self) -> bytes:
        return self._build_packet_type_1(MSGID_PARK)

    def play(self) -> bytes:
        return self._build_packet_type_1(MSGID_PLAY)

    def pause(self) -> bytes:
        return self._build_packet_type_1(MSGID_PAUSE)

    def stop(self) -> bytes:
        return self._build_packet_type_1(MSGID_STOP)

    def estop(self) -> bytes:
        return self._build_packet_type_1(MSGID_ESTOP)

    def reset(self) -> bytes:
        return self._build_packet_type_1(MSGID_RESET)

    def get_status(self) -> bytes:
        return self._build_packet_type_1(MSGID_STATUS)

    def jog(self, pose: list[float]) -> bytes:
        if len(pose) != 6:
            raise ValueError("Pose must be 6 floats.")
        pkt = self.data.copy()
        pkt[3] = self.seq & 0xFF
        pkt[4] = MSGID_JOG
        pkt[5:29] = struct.pack("<6f", *pose)

        self._finalize_crc(pkt)
        self.seq = (self.seq + 1) & 0xFF
        return bytes(pkt)

    def upload(self, trajectory: list[list[float]]) -> bytes:
        out = bytearray(len(trajectory) * PACKET_SIZE)
        for i, pose in enumerate(trajectory):
            if len(pose) != 6:
                raise ValueError("Each point must be 6 floats.")
            pkt = self.data.copy()
            pkt[3] = self.seq & 0xFF
            pkt[4] = MSGID_UPLOAD
            pkt[5:29] = struct.pack("<6f", *pose)

            self._finalize_crc(pkt)
            self.seq = (self.seq + 1) & 0xFF
            out[i * PACKET_SIZE : (i + 1) * PACKET_SIZE] = pkt
        return bytes(out)

    def validate_trajectory(self, crc32: int = 0, length: int = 0) -> bytes:
        pkt = self.data.copy()
        pkt[3] = self.seq & 0xFF
        pkt[4] = MSGID_VALIDATE
        pkt[5:9] = struct.pack("<I", length)
        pkt[9:13] = struct.pack("<I", crc32)
        print(f"[Packet.validate_trajectory] -> length: {length}, crc32: {crc32:08X}")
        self._finalize_crc(pkt)
        self.seq = (self.seq + 1) & 0xFF
        return bytes(pkt)

    @staticmethod
    def validate(packet: bytes) -> bool:
        if len(packet) != PACKET_SIZE:
            for p in chunked(packet, PACKET_SIZE):
                Packet.validate(bytes(p))
        if packet[0] != START_BYTE or packet[-1] != END_BYTE:
            return False
        # residue trick: CRC over bytes 0..62 (slice 0:63) should be 0
        return crc16(packet[0:63]) == 0


if __name__ == "__main__":
    # Example usage
    pktf = Packet()
    pkts = []
    pkts.append(pktf.ping())
    pkts.append(pktf.pong())
    pkts.append(pktf.enable())
    pkts.append(pktf.disable())
    pkts.append(pktf.play())
    pkts.append(pktf.pause())
    pkts.append(pktf.stop())
    pkts.append(pktf.estop())
    pkts.append(pktf.reset())
    pkts.append(pktf.jog([100.0, 200.0, 300.0, 0.0, 90.0, 180.0]))
    pkts.append(pktf.upload(np.random.rand(100, 6).tolist()))  # Upload 100 random trajectory points

    for pkt in pkts:
        for byte in pkt:
            print(f"{byte:02X}", end=" ")
        print()

    # Test CRC validation using residue trick
    print("\n=== CRC Validation Test ===")
    for i, pkt in enumerate(pkts):
        is_valid = Packet.validate(pkt)
        print(f"Packet {i}: {'✓ VALID' if is_valid else '✗ INVALID'}")
