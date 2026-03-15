import time
import logging
from logging.handlers import RotatingFileHandler
from threading import Thread
from multiprocessing.connection import Connection as PipeConnection

from hexapod import Hexapod


def setup_logging(
    *,
    level: int = logging.INFO,
    logfile: str | None = "serial_server.log",
    max_bytes: int = 2_000_000,
    backups: int = 5,
) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s"

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


class SerialServer:
    def __init__(self, conn: PipeConnection) -> None:
        self.conn: PipeConnection = conn
        self.running: bool = True

        self.log = logging.getLogger("serial_server")
        self.robot = Hexapod()
        self.request_handler_thread: Thread | None = None

    def _safe_send(self, msg) -> None:
        try:
            self.conn.send(msg)
        except Exception:
            self.log.exception("[pipe] send failed")

    def _shutdown_robot(self) -> None:
        try:
            self.robot.disconnect()
        except Exception:
            self.log.exception("[robot] disconnect failed")

    def request_handler(self) -> None:
        self.log.info("[handler] started")
        try:
            while self.running:
                try:
                    request = self.conn.recv()
                except EOFError:
                    self.log.warning("[handler] pipe closed (EOF); stopping")
                    self.running = False
                    break
                except Exception:
                    self.log.exception("[handler] error receiving request")
                    continue

                if request is None:
                    self.log.info("[handler] received None -> shutdown")
                    self._safe_send(None)
                    self.running = False
                    self._shutdown_robot()
                    break

                if not isinstance(request, dict) or not request:
                    self.log.warning("[handler] invalid request: %r", request)
                    continue

                key, value = next(iter(request.items()))
                self.log.debug("[handler] request: %s=%r", key, value)

                try:
                    match key:
                        case "PORT":
                            self.robot.set_port(value)
                        case "CONNECT":
                            self.robot.connect()
                        case "DISCONNECT":
                            self.robot.disconnect()
                        case "ENABLE":
                            self.robot.enable()
                        case "DISABLE":
                            self.robot.disable()
                        case "CALIBRATE":
                            self.robot.calibrate()
                        case "STAGE":
                            self.robot.stage()
                        case "PARK":
                            self.robot.park()
                        case "UPLOAD":
                            self.robot.upload(value)
                        case "VALIDATE":
                            self.robot.validate_trajectory()
                        case "PLAY":
                            self.robot.play()
                        case "PAUSE":
                            self.robot.pause()
                        case "STOP":
                            self.robot.stop()
                        case "ESTOP":
                            self.robot.estop()
                        case "RESET":
                            self.robot.reset()
                        case "SEND":  # MOVE
                            self.robot.move(value)
                        case "QUIT":
                            self.log.info("[handler] QUIT received")
                            self.running = False
                        case _:
                            self.log.warning("[handler] unknown request: %r", request)
                except Exception:
                    self.log.exception("[handler] failed handling %r", request)

        finally:
            self.running = False
            self.log.info("[handler] stopped")

    def run(self) -> None:
        self.request_handler_thread = Thread(
            target=self.request_handler,
            name="request_handler",
            daemon=True,  # keep True unless you need strict join-before-exit behavior
        )
        self.request_handler_thread.start()

        self.log.info("[run] started")
        try:
            while self.running:
                time.sleep(0.1)
                # Optional heartbeat:
                # self._safe_send({"STATUS": "RUNNING"})
        except KeyboardInterrupt:
            self.log.info("[run] KeyboardInterrupt -> stopping")
            self.running = False
        except Exception:
            self.log.exception("[run] crashed")
            self.running = False
        finally:
            self._shutdown_robot()
            if self.request_handler_thread:
                self.request_handler_thread.join()
            self.log.info("[run] stopped")


if __name__ == "__main__":
    setup_logging(level=logging.INFO, logfile="serial_server.log")
    # This file is usually imported and run by a parent process that passes `conn`.
    # If you want a standalone runner, create the PipeConnection and start SerialServer(conn).
    pass