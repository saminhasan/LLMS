import os
import logging
from logging.handlers import RotatingFileHandler
from multiprocessing import Pipe, Process
from multiprocessing.connection import Connection as PipeConnection

from serial_server import SerialServer
from ui import App


def setup_logging(
    *,
    level: int = logging.INFO,
    logfile: str | None = "logs/main.log",
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


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def run_app(conn: PipeConnection) -> None:
    setup_logging(level=logging.INFO, logfile="logs/app.log")
    log = logging.getLogger("app_process")
    try:
        App(conn=conn).run()
        log.info("[run_app] application closed")
    except KeyboardInterrupt:
        log.info("[run_app] KeyboardInterrupt")
    except Exception:
        log.exception("[run_app] crashed")
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("[run_app] conn.close failed")


def run_serial_server(conn: PipeConnection) -> None:
    setup_logging(level=logging.INFO, logfile="logs/serial_server_process.log")
    log = logging.getLogger("ss_process")
    try:
        ss = SerialServer(conn=conn)
        ss.run()
        log.info("[run_serial_server] server exited")
    except KeyboardInterrupt:
        log.info("[run_serial_server] KeyboardInterrupt")
    except Exception:
        log.exception("[run_serial_server] crashed")
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("[run_serial_server] conn.close failed")


if __name__ == "__main__":
    setup_logging(level=logging.INFO, logfile="logs/main.log")
    log = logging.getLogger("main")

    clear_screen()

    parent_conn, child_conn = Pipe(duplex=True)

    appProcess = Process(target=run_app, args=(parent_conn,), name="AppProcess")
    ssProcess = Process(target=run_serial_server, args=(child_conn,), name="SerialServerProcess")

    appProcess.start()
    ssProcess.start()

    try:
        # Wait for the app to finish first; then tell the server to stop.
        appProcess.join()

        if ssProcess.is_alive():
            try:
                # Convention used in your server: None means shutdown + echo None back
                parent_conn.send(None)
            except Exception:
                log.exception("[main] failed to send shutdown to server")

            # Give server a moment to exit cleanly, then force terminate if stuck.
            ssProcess.join(timeout=3.0)
            if ssProcess.is_alive():
                log.warning("[main] server did not stop; terminating")
                ssProcess.terminate()
                ssProcess.join()

    except KeyboardInterrupt:
        log.info("[main] KeyboardInterrupt -> terminating processes")
        for p in (appProcess, ssProcess):
            if p.is_alive():
                p.terminate()
        for p in (appProcess, ssProcess):
            p.join()

    finally:
        try:
            parent_conn.close()
        except Exception:
            log.exception("[main] parent_conn.close failed")
        try:
            child_conn.close()
        except Exception:
            log.exception("[main] child_conn.close failed")

        log.info("[main] processes terminated (app_exitcode=%s, ss_exitcode=%s)",
                 appProcess.exitcode, ssProcess.exitcode)