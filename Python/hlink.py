import struct


commands_dict = {
    # 0x0_ : safety / lifecycle
    "estop":     0x00, # no arguments
    "enable":    0x01, # no arguments
    "disable":   0x02, # no arguments
    "reset":     0x03, # no arguments
    "calibrate": 0x04, #one byte [1-6] for stage number #

    # 0x1_ : execution control
    "play":      0x10, # no arguments
    "pause":     0x11, # no arguments
    "stop":      0x12, # no arguments

    # 0x2_ : motion / machine actions
    "move":      0x20, # [x, y, z, a, b, c] 6 floats for target position #
    "park":      0x21, # no arguments
    "stage":     0x22, # no arguments

    # 0x3_ : data / runtime parameters
    "push":      0x30, # [n, x, y, z, a, b, c] 1 index + 6 floats for target position
    "pop":       0x31, # no arguments
    "feedback":  0x32, # 28 bytes for feedback data (e.g. current position, velocity, etc.)
    "feedrate":  0x33, # one float for feedrate
    "setGains": 0x34, # [kP, kI, kD] 3 floats for PID gains

    # 0x4_ : query / diagnostics
    "status":    0x40, # no arguments
    "info":      0x41, # no arguments
    "validate":  0x42, # crc32 4 bytes

    #
    'ack': 0x50, # one byte for acknowledged message ID
    'nak': 0x51, # one byte for rejected message ID
}


# Protocol constants (adjust to match your firmware)
START_BYTE = 0xAA
END_BYTE = 0x55
PAYLOAD_SIZE = 56  # bytes in [5..60]
PACKET_SIZE = 64


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _packet_core(msg_id: int, from_id: int, to_id: int, seq: int, payload: bytes) -> bytes:
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError(f"payload too large: {len(payload)} > {PAYLOAD_SIZE}")

    frame_0_to_60 = bytes([
        START_BYTE,
        from_id & 0xFF,
        to_id & 0xFF,
        seq & 0xFF,
        msg_id & 0xFF,
    ]) + payload.ljust(PAYLOAD_SIZE, b"\x00")

    crc = crc16_xmodem(frame_0_to_60)
    packet = frame_0_to_60 + crc.to_bytes(2, "big") + bytes([END_BYTE])

    if len(packet) != PACKET_SIZE:
        raise AssertionError(f"invalid packet length: {len(packet)}")

    return packet


def _raw(data: bytes = b"") -> bytes:
    return data


def _u8(value: int) -> bytes:
    return struct.pack("<B", value & 0xFF)


def _f32(value: float) -> bytes:
    return struct.pack("<f", value)


def _f32x3(kp: float, ki: float, kd: float) -> bytes:
    return struct.pack("<3f", kp, ki, kd)


def _f32x6(x: float, y: float, z: float, a: float, b: float, c: float) -> bytes:
    return struct.pack("<6f", x, y, z, a, b, c)


def _push(index: int, x: float, y: float, z: float, a: float, b: float, c: float) -> bytes:
    return struct.pack("<B6f", index & 0xFF, x, y, z, a, b, c)


def _feedback(data: bytes) -> bytes:
    if len(data) > 28:
        raise ValueError("feedback payload must be <= 28 bytes")
    return data


def _crc32_be(value: int) -> bytes:
    return int(value).to_bytes(4, "big", signed=False)


payload_encoders = {
    "estop": _raw,
    "enable": _raw,
    "disable": _raw,
    "reset": _raw,
    "calibrate": _u8,
    "play": _raw,
    "pause": _raw,
    "stop": _raw,
    "move": _f32x6,
    "park": _raw,
    "stage": _raw,
    "push": _push,
    "pop": _raw,
    "feedback": _feedback,
    "feedrate": _f32,
    "setGains": _f32x3,
    "status": _raw,
    "info": _raw,
    "validate": _crc32_be,
    "ack": _u8,
    "nak": _u8,
}


def make_packet(command: str, from_id: int, to_id: int, seq: int, *payload_args) -> bytes:
    if command not in commands_dict:
        raise KeyError(f"unknown command: {command}")
    if command not in payload_encoders:
        raise KeyError(f"missing payload encoder for command: {command}")

    payload = payload_encoders[command](*payload_args)
    return _packet_core(commands_dict[command], from_id, to_id, seq, payload)


def make_estop_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("estop", from_id, to_id, seq)


def make_enable_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("enable", from_id, to_id, seq)


def make_disable_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("disable", from_id, to_id, seq)


def make_reset_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("reset", from_id, to_id, seq)


def make_calibrate_packet(from_id: int, to_id: int, seq: int, stage: int) -> bytes:
    return make_packet("calibrate", from_id, to_id, seq, stage)


def make_play_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("play", from_id, to_id, seq)


def make_pause_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("pause", from_id, to_id, seq)


def make_stop_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("stop", from_id, to_id, seq)


def make_move_packet(from_id: int, to_id: int, seq: int, x: float, y: float, z: float, a: float, b: float, c: float) -> bytes:
    return make_packet("move", from_id, to_id, seq, x, y, z, a, b, c)


def make_park_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("park", from_id, to_id, seq)


def make_stage_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("stage", from_id, to_id, seq)


def make_push_packet(
    from_id: int,
    to_id: int,
    seq: int,
    index: int,
    x: float,
    y: float,
    z: float,
    a: float,
    b: float,
    c: float,
) -> bytes:
    return make_packet("push", from_id, to_id, seq, index, x, y, z, a, b, c)


def make_pop_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("pop", from_id, to_id, seq)


def make_feedback_packet(from_id: int, to_id: int, seq: int, data: bytes) -> bytes:
    return make_packet("feedback", from_id, to_id, seq, data)


def make_feedrate_packet(from_id: int, to_id: int, seq: int, feedrate: float) -> bytes:
    return make_packet("feedrate", from_id, to_id, seq, feedrate)


def make_set_gains_packet(from_id: int, to_id: int, seq: int, k_p: float, k_i: float, k_d: float) -> bytes:
    return make_packet("setGains", from_id, to_id, seq, k_p, k_i, k_d)


def make_status_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("status", from_id, to_id, seq)


def make_info_packet(from_id: int, to_id: int, seq: int) -> bytes:
    return make_packet("info", from_id, to_id, seq)


def make_validate_packet(from_id: int, to_id: int, seq: int, crc32_value: int) -> bytes:
    return make_packet("validate", from_id, to_id, seq, crc32_value)


def make_ack_packet(from_id: int, to_id: int, seq: int, msgid_ack: int) -> bytes:
    return make_packet("ack", from_id, to_id, seq, msgid_ack)


def make_nak_packet(from_id: int, to_id: int, seq: int, msgid_rejected: int) -> bytes:
    return make_packet("nak", from_id, to_id, seq, msgid_rejected)


