import logging
import os
import struct
import time
import zlib
from datetime import datetime
from logging.handlers import RotatingFileHandler
from multiprocessing.connection import Connection as PipeConnection
from threading import Event, Thread

import numpy as np
import serial
from more_itertools import chunked

from constants import *
from hexlink import Packet


def setup_logging(
    *,
    level: int = logging.INFO,
    logfile: str | None = "logs/robot_interface.log",
    max_bytes: int = 2_000_000,
    backups: int = 5,
) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s [%(processName)s/%(threadName)s]: %(message)s"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)

    if logfile:
        fh = RotatingFileHandler(logfile, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)


def traj_to_bytes_le(trajectory) -> bytes:
    b = bytearray()
    pack = struct.Struct("<6f").pack  # 6x float32, little-endian
    for row in trajectory:
        if len(row) != 6:
            raise ValueError("trajectory rows must have 6 values")
        b += pack(*map(float, row))
    return bytes(b)


class TelemetryRecorder:
    """Writes raw 64-byte packets to a binary log file."""

    def __init__(self, output_dir: str = "output"):
        self.log = logging.getLogger("telem_recorder")
        self.output_dir = output_dir
        self._file = None
        self._count = 0

    @property
    def recording(self) -> bool:
        return self._file is not None

    def start(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.output_dir, f"telem_{ts}.bin")
        self._file = open(path, "wb")
        self._count = 0
        self.log.info("[recorder] started -> %s", path)

    def write(self, raw_pkt: bytes) -> None:
        f = self._file
        if f and not f.closed:
            try:
                f.write(raw_pkt)
                self._count += 1
            except ValueError:
                pass

    def stop(self) -> None:
        f = self._file
        self._file = None
        if f:
            f.close()
            self.log.info("[recorder] stopped, %d packets saved", self._count)
            self._count = 0


class RobotInterface:
    """Merged replacement for serial_server + hexapod."""

    def __init__(self, conn: PipeConnection) -> None:
        self.conn = conn
        self.running = True
        self.log = logging.getLogger("robot_interface")

        self.port = serial.Serial(port=None, timeout=None)
        self.byteBuffer = bytearray()

        self.inLoop = Event()
        self.listen = Event()

        self.start_us = time.perf_counter_ns() // 1000
        self.packet = Packet()
        self.recorder = TelemetryRecorder()
        self._info_buffer = bytearray()
        self._info_expected_seq: int | None = None

    def micros(self) -> int:
        return (time.perf_counter_ns() // 1000) - self.start_us

    @property
    def portStr(self) -> str | None:
        return self.port.port

    @property
    def is_connected(self) -> bool:
        return self.port.is_open

    def _safe_send(self, msg) -> None:
        try:
            self.conn.send(msg)
        except Exception:
            self.log.exception("[pipe] send failed")

    def set_port(self, port: str) -> None:
        if not port:
            self.log.warning("[set_port] Invalid port: %r", port)
            return

        if port != self.portStr:
            self.port.port = port

    def connect(self) -> None:
        if self.is_connected:
            self.log.info("[connect] Already connected to %s", self.portStr)
            return
        if not self.portStr:
            self.log.warning("[connect] No port specified")
            return

        try:
            self.port.open()
        except serial.SerialException as exc:
            msg = str(exc).lower()
            if "filenotfounderror" in msg or "cannot find the file" in msg:
                self.log.error("[connect] Port %s was not found. Check cable/device and selected COM port.", self.portStr)
            elif "permissionerror" in msg or "access is denied" in msg:
                self.log.error("[connect] Port %s is busy or access denied. Close other apps using this COM port.", self.portStr)
            else:
                self.log.error("[connect] Could not open %s: %s", self.portStr, exc)
            return
        except Exception:
            self.log.exception("[connect] Error connecting to %s", self.portStr)
            return

        self.log.info("[connect] Connected to %s", self.portStr)
        self.listen.set()
        Thread(target=self.serial_listener, daemon=True, name="RobotSerialListener").start()

    def serial_listener(self) -> None:
        self.inLoop.set()
        self.log.debug("[serial_listener] started")

        try:
            while self.listen.is_set():
                n = max(getattr(self.port, "in_waiting", 0), 1)
                data = self.port.read(n)
                if not data:
                    continue

                self.byteBuffer.extend(data)
                self._consume_packets()

        except serial.SerialException as exc:
            self.log.warning("[serial_listener] Serial link lost (%s)", exc)
            self.listen.clear()
            if self.port.is_open:
                try:
                    self.port.close()
                except Exception:
                    self.log.debug("[serial_listener] close after disconnect failed", exc_info=True)
        except Exception:
            self.log.exception("[serial_listener] crashed")
        finally:
            self.inLoop.clear()
            self.log.debug("[serial_listener] stopped")

    def _consume_packets(self) -> None:
        while len(self.byteBuffer) >= PACKET_SIZE:
            if self.byteBuffer[0] != START_BYTE:
                del self.byteBuffer[0]
                continue

            if len(self.byteBuffer) < PACKET_SIZE:
                return

            candidate = bytes(self.byteBuffer[:PACKET_SIZE])

            if candidate[-1] != END_BYTE or not Packet.validate(candidate):
                del self.byteBuffer[0]
                continue

            del self.byteBuffer[:PACKET_SIZE]
            self.process_packet(candidate)

    def _reset_info_assembly(self) -> None:
        self._info_buffer.clear()
        self._info_expected_seq = None

    def _handle_info_packet(self, pkt: bytes) -> str | None:
        seq = pkt[3]
        more = pkt[5] != 0
        payload = pkt[6:61]

        if self._info_expected_seq is not None and seq != self._info_expected_seq:
            self.log.warning(
                "[recv] INFO sequence gap (expected=%d got=%d), restarting assembly",
                self._info_expected_seq,
                seq,
            )
            self._reset_info_assembly()

        nul_idx = payload.find(0)
        chunk = payload if nul_idx < 0 else payload[:nul_idx]
        self._info_buffer.extend(chunk)

        if more:
            self._info_expected_seq = (seq + 1) & 0xFF
            return None

        msg = self._info_buffer.decode(errors="ignore")
        self._reset_info_assembly()
        return msg

    def process_packet(self, pkt: bytes) -> None:
        msgid = pkt[4]

        if self.recorder.recording:
            self.recorder.write(pkt)

        if msgid == MSGID_INFO:
            msg = self._handle_info_packet(pkt)
            if msg is not None:
                self.log.info("[recv] INFO: %s", msg)

    def sendData(self, data: bytes) -> bool:
        if not self.is_connected:
            self.log.warning("[send] Not connected")
            return False

        try:
            t0 = time.perf_counter_ns()
            sent = 0

            for c in chunked(data, 512):
                chunk_size = self.port.write(bytes(c))
                if chunk_size is None:
                    self.log.warning("[send] write returned None, stopping")
                    break
                sent += chunk_size

            dt = (time.perf_counter_ns() - t0) / 1e9

            ok = sent == len(data)
            if sent > 64:
                self.log.info(
                    "[send] Sent %d bytes in %.2f s (%.2f MB/s)%s",
                    sent,
                    dt,
                    (sent / (dt * 1024 * 1024)) if dt > 0 else 0.0,
                    "" if ok else " [INCOMPLETE]",
                )
            elif not ok:
                self.log.warning("[send] Incomplete send (%d/%d)", sent, len(data))

            return ok

        except Exception:
            self.log.exception("[send] Error")
            return False

    def disconnect(self) -> None:
        self.listen.clear()

        if self.inLoop.is_set():
            try:
                self.port.cancel_read()
            except Exception:
                self.log.exception("[disconnect] cancel_read failed")

        if self.port.is_open:
            try:
                self.port.close()
                self.log.info("[disconnect] Disconnected from %s", self.portStr)
            except Exception:
                self.log.exception("[disconnect] Error closing %s", self.portStr)

    def enable(self) -> None:
        self.sendData(self.packet.enable())

    def disable(self) -> None:
        self.sendData(self.packet.disable())

    def calibrate(self) -> None:
        self.sendData(self.packet.calibrate())

    def stage(self) -> None:
        self.sendData(self.packet.stage())

    def park(self) -> None:
        self.sendData(self.packet.park())

    def upload(self, filename: str) -> None:
        try:
            data = np.loadtxt(filename, delimiter=",")
            if data.ndim == 1:
                data = data.reshape(1, -1)

            if data.shape[1] != 6:
                self.log.error("[upload] File must have 6 columns, got %d", data.shape[1])
                return

            trajectory = data.tolist()
            self.sendData(self.packet.upload(trajectory))

            db = traj_to_bytes_le(trajectory)
            crc = zlib.crc32(db)

            self.log.info("[upload] First row bytes: %s", db[:24].hex().upper())
            self.log.info("[upload] Total bytes: %d, CRC32: 0x%08X", len(db), crc)

            self.validate_trajectory(crc32=crc, length=len(trajectory))
            self.log.info("[upload] Uploaded %s (%d points)", filename, len(trajectory))

        except Exception:
            self.log.exception("[upload] Error loading %s", filename)

    def validate_trajectory(self, crc32: int = 0, length: int = 0) -> None:
        self.sendData(self.packet.validate_trajectory(crc32=crc32, length=length))

    def play(self) -> None:
        self.recorder.start()
        self.sendData(self.packet.play())

    def pause(self) -> None:
        self.sendData(self.packet.pause())

    def stop(self) -> None:
        self.recorder.stop()
        self.sendData(self.packet.stop())

    def estop(self) -> None:
        self.recorder.stop()
        self.sendData(self.packet.estop())

    def reset(self) -> None:
        self.sendData(self.packet.reset())

    def move(self, positions: list[float]) -> None:
        if isinstance(positions, np.ndarray):
            positions = positions.tolist()

        if not isinstance(positions, list):
            self.log.error("[move] positions must be list or ndarray")
            return

        self.sendData(self.packet.jog(positions))

    def handle_request(self, request) -> bool:
        """Returns False when caller should stop."""
        if request is None:
            self.log.info("[handler] received None -> shutdown")
            self._safe_send(None)
            return False

        if not isinstance(request, dict) or not request:
            self.log.warning("[handler] invalid request: %r", request)
            return True

        key, value = next(iter(request.items()))
        self.log.debug("[handler] request: %s=%r", key, value)

        match key:
            case "PORT":
                self.set_port(value)
            case "CONNECT":
                self.connect()
            case "DISCONNECT":
                self.disconnect()
            case "ENABLE":
                self.enable()
            case "DISABLE":
                self.disable()
            case "CALIBRATE":
                self.calibrate()
            case "STAGE":
                self.stage()
            case "PARK":
                self.park()
            case "UPLOAD":
                self.upload(value)
            case "VALIDATE":
                self.validate_trajectory()
            case "PLAY":
                self.play()
            case "PAUSE":
                self.pause()
            case "STOP":
                self.stop()
            case "ESTOP":
                self.estop()
            case "RESET":
                self.reset()
            case "SEND":
                self.move(value)
            case "QUIT":
                self.log.info("[handler] QUIT received")
                return False
            case _:
                self.log.warning("[handler] unknown request: %r", request)

        return True

    def run(self) -> None:
        self.log.info("[run] started")
        try:
            while self.running:
                try:
                    request = self.conn.recv()
                except EOFError:
                    self.log.warning("[run] pipe closed (EOF); stopping")
                    break
                except Exception:
                    self.log.exception("[run] error receiving request")
                    continue

                try:
                    self.running = self.handle_request(request)
                except Exception:
                    self.log.exception("[run] failed handling request: %r", request)
        finally:
            self.running = False
            self.disconnect()
            self.log.info("[run] stopped")
