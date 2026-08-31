"""
Django management command: cleanup_unknown_facelogs

Har kuni soat 00:00 da cron job sifatida ishga tushadi.
Bazada matched_user=NULL bo'lgan (noma'lum shaxslar) FaceLog yozuvlarini
BUGUNDAN OLDINGI kunlarga oid bo'lsa o'chiradi.

Usage:
    python manage.py cleanup_unknown_facelogs

Cron:
    0 0 * * * cd /home/smartgate/web/SmartGate && source venv/bin/activate && python manage.py cleanup_unknown_facelogs >> /var/log/smartgate_cleanup.log 2>&1
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction


class Command(BaseCommand):
    help = "Noma'lum shaxslar (matched_user=NULL) yuz qayd yozuvlarini tozalaydi"

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=0,
            help='Necha kundan eski yozuvlarni o\'chirish (default: 0 = bugungilar qoladi, hammasi o\'chiriladi)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Haqiqatda o\'chirmasdan, faqat nechta yozuv o\'chirilishini ko\'rsatish',
        )

    def handle(self, *args, **options):
        from camera.models import FaceLog

        days = options['days']
        dry_run = options['dry_run']

        now = timezone.now()

        if days == 0:
            # Bugun yarim tun boshidan oldin yaratilgan noma'lumlarni o'chir
            today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            qs = FaceLog.objects.filter(
                matched_user__isnull=True,
                captured_at__lt=today_midnight,
            )
            time_desc = f"bugundan oldingi (< {today_midnight.strftime('%Y-%m-%d %H:%M')})"
        else:
            cutoff = now - timezone.timedelta(days=days)
            qs = FaceLog.objects.filter(
                matched_user__isnull=True,
                captured_at__lt=cutoff,
            )
            time_desc = f"{days} kundan eski (< {cutoff.strftime('%Y-%m-%d %H:%M')})"

        count = qs.count()

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY-RUN] O'chirilishi kerak: {count} ta noma'lum yuz qaydi ({time_desc})"
                )
            )
            return

        if count == 0:
            self.stdout.write(self.style.SUCCESS(f"O'chirish uchun hech narsa topilmadi ({time_desc})."))
            return

        with transaction.atomic():
            deleted_count, _ = qs.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {deleted_count} ta noma'lum yuz qaydi o'chirildi ({time_desc})"
            )
        )
