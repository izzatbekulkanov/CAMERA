# camera/telegram_bot.py
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.db import transaction

from attendance.models import SiteSettings
from users.models import CustomUser, TelegramProfile

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 30  # 20 -> 30 sekundga oshirdik
_POLL_SLEEP = 2.0  # 1.0 -> 2.0 sekundga oshirdik

# chat_id -> awaiting_id (True/False)
_pending: dict[int, bool] = {}
_pending_lock = threading.Lock()

_worker_thread: Optional[threading.Thread] = None
_stop_flag = False

# kanal log spam bo'lmasin (user+camera bo'yicha)
CHANNEL_LOG_COOLDOWN_S = 5.0
_last_channel_log: dict[tuple[str, str], float] = {}
_last_channel_log_lock = threading.Lock()


# ------------------------
# Telegram API helpers with retry logic
# ------------------------

def _get_session_with_retry() -> requests.Session:
    session = requests.Session()

    retry_strategy = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Global session yaratamiz
_session = _get_session_with_retry()


def _tg_call(token: str, method: str, payload: dict, silent: bool = False) -> Optional[dict]:
    """
    silent=True bo'lsa faqat ERROR darajasidagi xatolarni log qiladi
    """
    url = _TELEGRAM_API.format(token=token, method=method)
    try:
        r = _session.post(url, json=payload, timeout=_TIMEOUT)
        if r.status_code != 200:
            if not silent:
                logger.warning("[TG] %s status=%s body=%s", method, r.status_code, (r.text or "")[:400])
            return None
        return r.json()
    except requests.exceptions.ConnectionError as exc:
        # Connection xatolarini kamroq log qilamiz
        if not silent:
            logger.debug("[TG] %s connection error: %s", method, str(exc)[:100])
        return None
    except requests.exceptions.Timeout:
        if not silent:
            logger.debug("[TG] %s timeout", method)
        return None
    except Exception as exc:
        # Boshqa jiddiy xatolar
        logger.error("[TG] %s unexpected error: %s", method, exc)
        return None


def _main_keyboard() -> list[list[dict]]:
    return [
        [{"text": "ℹ️ Men haqimda"}, {"text": "🚪 Chiqish"}],
    ]


def _send_message(
        token: str,
        chat_id: int | str,
        text: str,
        keyboard: Optional[list[list[dict]]] = None,
        parse_mode: Optional[str] = None,
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if keyboard is not None:
        payload["reply_markup"] = {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    _tg_call(token, "sendMessage", payload)


def _send_remove_keyboard(
        token: str,
        chat_id: int | str,
        text: str,
        parse_mode: Optional[str] = None,
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": {"remove_keyboard": True},
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    _tg_call(token, "sendMessage", payload)


def _get_bot_config():
    settings = SiteSettings.get_settings()
    token = (getattr(settings, "bot_token", "") or "").strip()
    channel_id = str(getattr(settings, "bot_channel_id", "") or "").strip()
    return token, channel_id


# ------------------------
# DB helpers
# ------------------------

def _find_user_by_person_id(pid: str) -> Optional[CustomUser]:
    pid = (pid or "").strip()
    if not pid:
        return None
    return (
            CustomUser.objects.filter(employee_id_number=pid).first()
            or CustomUser.objects.filter(student_id_number=pid).first()
    )


def _get_profile_by_chat_id(chat_id: int) -> Optional[TelegramProfile]:
    return TelegramProfile.objects.select_related("user").filter(chat_id=chat_id).first()


@transaction.atomic
def _bind_chat_to_user(chat_id: int, user: CustomUser, tg_user: dict) -> None:
    TelegramProfile.objects.update_or_create(
        user=user,
        defaults={
            "chat_id": chat_id,
            "tg_username": (tg_user.get("username") or ""),
            "first_name": (tg_user.get("first_name") or ""),
            "last_name": (tg_user.get("last_name") or ""),
        },
    )


@transaction.atomic
def _unbind_chat(chat_id: int) -> int:
    """
    chat_id bo'yicha bog'lanishni o'chiradi.
    return: o'chgan yozuvlar soni
    """
    return TelegramProfile.objects.filter(chat_id=chat_id).delete()[0]


def _set_pending(chat_id: int, val: bool) -> None:
    with _pending_lock:
        if val:
            _pending[chat_id] = True
        else:
            _pending.pop(chat_id, None)


def _is_pending(chat_id: int) -> bool:
    with _pending_lock:
        return bool(_pending.get(chat_id, False))


# ------------------------
# Update handler
# ------------------------

def _ask_id(token: str, chat_id: int) -> None:
    """
    Faqat ID so'raydi.
    """
    _set_pending(chat_id, True)
    _send_message(
        token,
        chat_id,
        "👋 Assalomu alaykum!\n\n🆔 Iltimos, ID raqamingizni kiriting:",
        keyboard=_main_keyboard(),
    )


def _handle_update(token: str, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    chat_id = int(chat_id)

    text = (msg.get("text") or "").strip()
    tg_user = msg.get("from") or {}

    if not text:
        return

    # /start -> agar allaqachon ro'yxatdan o'tgan bo'lsa ID so'ramaymiz
    if text.startswith("/start"):
        logger.info("[TG] /start chat_id=%s", chat_id)

        tp = _get_profile_by_chat_id(chat_id)
        if tp and tp.user:
            name = tp.user.full_name or tp.user.username or "Noma'lum"
            _set_pending(chat_id, False)
            _send_message(
                token,
                chat_id,
                "✅ Siz allaqachon ro'yxatdan o'tgansiz.\n\n"
                f"👤 {name}\n\n"
                "ℹ️ Men haqimda tugmasini bosing yoki boshqa foydalanuvchi uchun 🚪 Chiqish qiling.",
                keyboard=_main_keyboard(),
            )
            return

        _ask_id(token, chat_id)
        return

    # ℹ️ Men haqimda
    if text.lower() in {"ℹ️ men haqimda", "men haqimda", "/me"}:
        tp = _get_profile_by_chat_id(chat_id)
        if not tp or not tp.user:
            _send_message(
                token,
                chat_id,
                "ℹ️ Siz hali ro'yxatdan o'tmagansiz.\n\n/start bosing va ID kiriting.",
                keyboard=_main_keyboard(),
            )
            return

        u = tp.user
        role = "Xodim" if u.role == "employee" else "Talaba"
        pid_show = u.employee_id_number if u.role == "employee" else u.student_id_number

        name = u.full_name or u.username or "Noma'lum"
        dept = u.department_name or "-"
        grp = u.group_name or "-"
        pos = u.position or "-"
        spec = u.specialty or "-"

        text_me = (
            "👤 *Men haqimda*\n\n"
            f"🧾 *Turi:* {role}\n"
            f"🆔 *ID:* {pid_show or '-'}\n"
            f"👤 *Ism:* {name}\n"
            f"🏢 *Bo'lim:* {dept}\n"
            f"👥 *Guruh:* {grp}\n"
            f"💼 *Lavozim:* {pos}\n"
            f"🎓 *Mutaxassislik:* {spec}\n"
        )
        _send_message(token, chat_id, text_me, keyboard=_main_keyboard(), parse_mode="Markdown")
        return

    # 🚪 Chiqish (logout) -> keyboardni remove qilamiz va yana ID so'raymiz
    if text.lower() in {"🚪 chiqish", "chiqish", "/logout", "/exit"}:
        deleted = _unbind_chat(chat_id)
        logger.info("[TG] logout chat_id=%s deleted=%s", chat_id, deleted)

        _set_pending(chat_id, True)

        _send_remove_keyboard(
            token,
            chat_id,
            "✅ Chiqildi.\n\n🆔 Yangi ID kiriting:",
        )
        return

    # ID kutilayotgan bo'lsa
    if _is_pending(chat_id):
        pid = text
        user = _find_user_by_person_id(pid)
        if not user:
            _send_message(
                token,
                chat_id,
                "❌ ID topilmadi.\n\n🆔 Qayta kiriting:",
                keyboard=_main_keyboard(),
            )
            return

        _bind_chat_to_user(chat_id, user, tg_user)
        _set_pending(chat_id, False)

        role = "Xodim" if user.role == "employee" else "Talaba"
        name = user.full_name or user.username or "Noma'lum"
        pid_show = user.employee_id_number if user.role == "employee" else user.student_id_number

        _send_message(
            token,
            chat_id,
            "✅ *Bog'landi!*\n\n"
            f"👤 *Foydalanuvchi:* {name}\n"
            f"🧾 *Turi:* {role}\n"
            f"🆔 *ID:* {pid_show or pid}\n\n"
            "Endi kirganingizda sizga xabar yuboriladi.\n\n"
            "Boshqa foydalanuvchini bog'lash uchun: 🚪 *Chiqish*",
            keyboard=_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    # default: foydalanuvchi pending emas -> yordam
    _send_message(
        token,
        chat_id,
        "ℹ️ Ro'yxatdan o'tish uchun /start bosing.\n"
        "Agar avval ro'yxatdan o'tgan bo'lsangiz: ℹ️ Men haqimda.",
        keyboard=_main_keyboard(),
    )


# ------------------------
# Worker loop
# ------------------------

def _worker_loop() -> None:
    global _stop_flag
    logger.info("[TG] worker started")
    offset = 0
    webhook_cleared = False
    consecutive_errors = 0
    max_backoff = 60  # maksimal 60 sekund kutish

    while not _stop_flag:
        try:
            token, _channel_id = _get_bot_config()
            if not token:
                time.sleep(5)
                continue

            # ✅ getUpdates ishlashi uchun webhook o'chirib qo'yamiz (1 marta)
            if not webhook_cleared:
                result = _tg_call(token, "deleteWebhook", {"drop_pending_updates": False})
                if result:
                    webhook_cleared = True
                    logger.info("[TG] deleteWebhook called successfully")
                else:
                    logger.warning("[TG] deleteWebhook failed, will retry")
                    time.sleep(5)
                    continue

            # getUpdates - silent=True qilib log spamni kamaytirdik
            resp = _tg_call(token, "getUpdates", {"timeout": 50, "offset": offset}, silent=True)

            if not resp or not resp.get("ok"):
                consecutive_errors += 1
                # Exponential backoff
                wait_time = min(consecutive_errors * 2, max_backoff)
                if consecutive_errors > 5:
                    logger.warning("[TG] %d consecutive errors, waiting %ds", consecutive_errors, wait_time)
                time.sleep(wait_time)
                continue

            # Muvaffaqiyatli javob - error counterni reset qilamiz
            consecutive_errors = 0

            updates = resp.get("result") or []
            if not updates:
                continue

            for upd in updates:
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                _handle_update(token, upd)

        except Exception as exc:
            consecutive_errors += 1
            logger.error("[TG] loop error: %s", exc)
            wait_time = min(consecutive_errors * 2, max_backoff)
            time.sleep(wait_time)


def start_telegram_worker() -> None:
    global _worker_thread, _stop_flag
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_flag = False
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    logger.info("[TG] worker thread launched")


# ------------------------
# Channel log + user notify
# ------------------------

def _channel_log_throttle(camera_ip: str, person_key: str) -> bool:
    nowm = time.monotonic()
    key = (camera_ip or "unknown", person_key or "unknown")
    with _last_channel_log_lock:
        last = _last_channel_log.get(key, 0.0)
        if nowm - last < CHANNEL_LOG_COOLDOWN_S:
            return False
        _last_channel_log[key] = nowm
        return True


def _format_channel_log(
        user: Optional[CustomUser],
        person_id: str | None,
        camera_ip: str,
        when_str: str,
        sim: float | None
) -> str:
    sim_txt = f"{sim:.2f}" if sim is not None else "-"

    if user:
        role = "Xodim" if user.role == "employee" else "Talaba"
        pid_show = user.employee_id_number if user.role == "employee" else user.student_id_number
        dept = user.department_name or "-"
        grp = user.group_name or "-"
        name = user.full_name or user.username or "Noma'lum"
        return (
            "🟢 *KIRISH*\n"
            f"👤 {name}\n"
            f"🧾 {role} | ID: {pid_show or (person_id or '-')}\n"
            f"🏢 Bo'lim: {dept}\n"
            f"👥 Guruh: {grp}\n"
            f"📷 Kamera: KIRISH ESHIGI\n"
            f"🎯 Similarity: {sim_txt}\n"
            f"⏰ {when_str}"
        )

    return (
        "🟢 *KIRISH*\n"
        "👤 Noma'lum\n"
        f"🧾 ID: {person_id or '-'}\n"
        f"📷 Kamera: KIRISH ESHIGI\n"
        f"🎯 Similarity: {sim_txt}\n"
        f"⏰ {when_str}"
    )


def notify_user_arrival(
        user_id: int | None = None,
        person_id: str | None = None,
        camera_ip: str = "",
        similarity: float | None = None,
) -> None:
    """
    1) TelegramProfile bog'langan user bo'lsa -> userga xabar
    2) Kanal chat_id bo'lsa -> kanalga tushunarli log
    """
    try:
        token, channel_id = _get_bot_config()
        if not token:
            return

        when_str = time.strftime("%Y-%m-%d %H:%M:%S")

        user = None
        if user_id:
            user = CustomUser.objects.filter(id=user_id).first()
        if user is None and person_id:
            user = _find_user_by_person_id(person_id)

        # 1) USERGA XABAR
        if user:
            tp = TelegramProfile.objects.filter(user=user).first()
            if tp:
                full_name = user.full_name or user.username or "Noma'lum"
                msg_user = (
                    "✅ *Siz keldingiz.*\n\n"
                    f"👤 *{full_name}*\n"
                    f"📷 *Kamera:* KIRISH ESHIGI\n"
                    f"⏰ *Vaqt:* {when_str}\n"
                )
                _send_message(token, int(tp.chat_id), msg_user, keyboard=_main_keyboard(), parse_mode="Markdown")

        # 2) KANALGA LOG
        if channel_id:
            person_key = str((person_id or "") or (user_id or "") or "unknown")
            if _channel_log_throttle(camera_ip=camera_ip, person_key=person_key):
                text = _format_channel_log(user, person_id, camera_ip, when_str, similarity)
                _send_message(
                    token,
                    channel_id,
                    text,
                    parse_mode="Markdown",
                )

    except Exception as exc:
        logger.warning("[TG] notify failed: %s", exc)