import logging
import os
from logging.handlers import RotatingFileHandler
from multiprocessing import Pipe, Process
from multiprocessing.connection import Connection as PipeConnection

from RobotInterface import RobotInterface, setup_logging as setup_robot_logging
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
    setup_logging(level=logging.INFO, logfile="logs/app2.log")
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


def run_robot_interface(conn: PipeConnection) -> None:
    setup_robot_logging(level=logging.INFO, logfile="logs/robot_interface_process.log")
    log = logging.getLogger("robot_interface_process")
    try:
        robot = RobotInterface(conn=conn)
        robot.run()
        log.info("[run_robot_interface] exited")
    except KeyboardInterrupt:
        log.info("[run_robot_interface] KeyboardInterrupt")
    except Exception:
        log.exception("[run_robot_interface] crashed")
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("[run_robot_interface] conn.close failed")


if __name__ == "__main__":
    setup_logging(level=logging.INFO, logfile="logs/main.log")
    log = logging.getLogger("main")

    clear_screen()

    parent_conn, child_conn = Pipe(duplex=True)

    app_process = Process(target=run_app, args=(parent_conn,), name="AppProcess")
    robot_process = Process(target=run_robot_interface, args=(child_conn,), name="RobotInterfaceProcess")

    app_process.start()
    robot_process.start()

    try:
        app_process.join()

        if robot_process.is_alive():
            try:
                parent_conn.send(None)
            except Exception:
                log.exception("[main] failed to send shutdown to robot interface")

            robot_process.join(timeout=3.0)
            if robot_process.is_alive():
                log.warning("[main] robot interface did not stop; terminating")
                robot_process.terminate()
                robot_process.join()

    except KeyboardInterrupt:
        log.info("[main] KeyboardInterrupt -> terminating processes")
        for p in (app_process, robot_process):
            if p.is_alive():
                p.terminate()
        for p in (app_process, robot_process):
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

        log.info(
            "[main] processes terminated (app_exitcode=%s, robot_exitcode=%s)",
            app_process.exitcode,
            robot_process.exitcode,
        )
