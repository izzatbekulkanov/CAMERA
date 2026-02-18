# camera/management/commands/camera_daemon
import logging
import os
import sys
import threading
import time

from django.core.management.base import BaseCommand


def _setup_logging(verbosity: int) -> None:
    """
    systemd / journalctl uchun qulay logger:
    - stdout ga chiqadi
    - verbosity oshsa DEBUG ham chiqadi
    """
    level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)

    # shovqinni kamaytirish
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ======================================================
# ✅ SYSTEMD NOTIFY (WATCHDOG)
# ======================================================

class SystemdWatchdog:
    """
    systemd WatchdogSec ishlashi uchun:
      - READY=1
      - WATCHDOG=1 (har interval)
    sdnotify o'rnatilmagan bo'lsa ham ishlaydi (fallback).
    """

    def __init__(self, interval: int = 10):
        self.interval = max(2, int(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # systemd notify socket mavjudmi?
        self.enabled = bool(os.environ.get("NOTIFY_SOCKET"))

        self.notifier = None
        if self.enabled:
            try:
                from sdnotify import SystemdNotifier
                self.notifier = SystemdNotifier()
            except Exception:
                self.notifier = None
                self.enabled = False

    def notify(self, msg: str) -> None:
        if not self.enabled or not self.notifier:
            return
        try:
            self.notifier.notify(msg)
        except Exception:
            pass

    def start(self):
        """
        READY=1 yuboradi va watchdog ping threadni yoqadi
        """
        if not self.enabled:
            return

        self.notify("READY=1")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        """
        Har interval sekundda WATCHDOG=1 yuboradi.
        Agar python loop qotsa -> bu thread ham qotadi -> watchdog ping ketmaydi -> systemd restart qiladi
        """
        logger = logging.getLogger(__name__)
        logger.info("[SYSTEMD] watchdog started interval=%ss", self.interval)

        while not self._stop.is_set():
            self.notify("WATCHDOG=1")
            time.sleep(self.interval)


class Command(BaseCommand):
    help = "Run RTSP recognition daemon (runs as a long-lived process)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll",
            type=int,
            default=0,
            help="Har N soniyada active kameralarni qayta yuklash (0 = o'chirilgan)",
        )
        parser.add_argument(
            "--watchdog",
            type=int,
            default=10,
            help="systemd watchdog ping interval (sekund). Default: 10",
        )

    def handle(self, *args, **options):
        verbosity = int(options.get("verbosity") or 1)
        _setup_logging(verbosity)

        poll = int(options.get("poll") or 0)
        watchdog_interval = int(options.get("watchdog") or 10)

        logger = logging.getLogger(__name__)
        logger.info(
            "camera_daemon starting (poll=%ss, pid=%s, cwd=%s)",
            poll,
            os.getpid(),
            os.getcwd(),
        )

        # ======================================================
        # ✅ SYSTEMD WATCHDOG START
        # ======================================================
        watchdog = SystemdWatchdog(interval=watchdog_interval)
        watchdog.start()
        if watchdog.enabled:
            logger.info("[SYSTEMD] notify enabled (WATCHDOG active)")
        else:
            logger.info("[SYSTEMD] notify disabled (NOTIFY_SOCKET missing or sdnotify not installed)")

        # ======================================================
        # 🔥 HIKVISION ASYNC WORKER (1 marta ishga tushadi)
        # ======================================================
        try:
            from camera.hikvision import start_hikvision_worker
            start_hikvision_worker()
            logger.info("[HIKVISION] worker started")
        except Exception as exc:
            logger.exception("[HIKVISION] worker failed to start: %s", exc)

        # ======================================================
        # 🤖 TELEGRAM BOT WORKER (polling)
        # ======================================================
        try:
            from camera.telegram_bot import start_telegram_worker
            start_telegram_worker()
            logger.info("[TELEGRAM] worker started")
        except Exception as exc:
            logger.exception("[TELEGRAM] worker failed to start: %s", exc)

        # ======================================================
        # 🚀 RTSP DAEMON
        # ======================================================
        try:
            from camera.rtsp_runner import run_blocking
            run_blocking(poll_seconds=poll)
        except KeyboardInterrupt:
            logger.warning("camera_daemon interrupted by user")
        except Exception as exc:
            logger.exception("camera_daemon crashed: %s", exc)
            raise
        finally:
            watchdog.stop()
