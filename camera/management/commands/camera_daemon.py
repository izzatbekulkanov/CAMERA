# camera/management/commands/camera_daemon.py
import logging
import os
import sys

from django.core.management.base import BaseCommand


def _setup_logging(verbosity: int) -> None:
    """
    systemd/journalctl uchun qulay logger:
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


class Command(BaseCommand):
    help = "Run RTSP recognition daemon (runs as a long-lived process)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll",
            type=int,
            default=0,
            help="Har N soniyada active kameralarni qayta yuklash (0 = o'chirilgan)",
        )

    def handle(self, *args, **options):
        verbosity = int(options.get("verbosity") or 1)
        _setup_logging(verbosity)

        poll = int(options.get("poll") or 0)

        logging.getLogger(__name__).info(
            "camera_daemon starting (poll=%ss, pid=%s, cwd=%s)",
            poll,
            os.getpid(),
            os.getcwd(),
        )

        # ✅ runner
        from camera.rtsp_runner import run_blocking

        run_blocking(poll_seconds=poll)
