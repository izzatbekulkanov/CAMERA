from calendar import monthrange
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.db.models import Q, Exists, OuterRef, Count
from django.utils import timezone

from attendance.models import Attendance
from users.models import FaceEncoding, CustomUser, TelegramProfile
from urllib.parse import urlencode


def login_view(request):
    next_url = request.GET.get('next') or request.POST.get('next') or 'dashboard'
    
    # Agar foydalanuvchi allaqachon login bo'lsa
    if request.user.is_authenticated:
        return redirect(next_url)

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        # Login/parol bo'sh bo'lsa
        if not username or not password:
            messages.error(request, "Iltimos, login va parolni to‘liq kiriting.")
            return redirect(f"/login/?next={next_url}" if next_url != 'dashboard' else 'login')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if not user.is_active:
                messages.error(
                    request,
                    "Sizning profilingiz faol emas. Iltimos, administratorga murojaat qiling."
                )
                return redirect(f"/login/?next={next_url}" if next_url != 'dashboard' else 'login')

            login(request, user)
            display_name = user.get_full_name() or user.username
            messages.success(request, f"Xush kelibsiz, {display_name}!")
            return redirect(next_url)
        else:
            messages.error(
                request,
                "Login yoki parol noto‘g‘ri. Iltimos, qayta urinib ko‘ring."
            )
            return redirect(f"/login/?next={next_url}" if next_url != 'dashboard' else 'login')

    return render(request, 'base/login.html')


def logout_view(request):
    """Tizimdan chiqish va logout sahifasini ko‘rsatish."""
    logout(request)
    messages.info(request, "Tizimdan chiqdingiz.")
    return render(request, 'base/logout.html')


def reset_password_view(request):
    """Parolni tiklash sahifasi."""
    if request.method == 'POST':
        email = request.POST.get('email')
        # Bu joyda keyingi bosqichda email orqali tiklash jarayonini qo‘shasiz
        messages.success(request, f"{email} manziliga parolni tiklash bo‘yicha yo‘riqnoma yuborildi.")
        return redirect('login')

    return render(request, 'base/reset_password.html')

@login_required(login_url="login")
def profile_view(request):
    # ✅ profile? id=... bo‘lsa o‘sha userni ochamiz
    user_id = request.GET.get("id")
    if user_id:
        user = get_object_or_404(CustomUser, id=user_id)
    else:
        user = request.user

    # ✅ TelegramProfile
    telegram_profile = getattr(user, "telegram_profile", None)
    recent_attendances = Attendance.objects.filter(user=user).order_by("-date")[:7]

    # ✅ Tanlangan yil va oyni olish (default: joriy yil va oy)
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
        if not (1900 <= year <= 2100) or not (1 <= month <= 12):
            raise ValueError
    except (TypeError, ValueError):
        year = today.year
        month = today.month

    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    # ✅ Shu oy bo‘yicha attendance’lar
    attendances = (
        Attendance.objects.filter(user=user, date__range=(first_day, last_day))
        .select_related("user")
        .prefetch_related("photos", "psychology")
        .order_by("date")
    )

    # ✅ Attendance map (date -> attendance)
    attendance_map = {a.date: a for a in attendances}

    # ✅ Kalendar matritsasini yaratish (Dushanbadan Yakshanbagacha)
    import calendar
    matrix = calendar.monthcalendar(year, month)

    calendar_weeks = []
    for week in matrix:
        week_days = []
        for day_num in week:
            if day_num == 0:
                week_days.append(None)
            else:
                d = date(year, month, day_num)
                att = attendance_map.get(d)
                day_data = {
                    "day": day_num,
                    "date": d,
                    "status": "future" if d > today else ("present" if att and att.is_present else ("exited" if att else "absent")),
                    "entry_time": timezone.localtime(att.entry_time).strftime("%H:%M") if att and att.entry_time else "-",
                    "exit_time": timezone.localtime(att.exit_time).strftime("%H:%M") if att and att.exit_time else "-",
                    "duration": att.duration_minutes or 0 if att else 0,
                    "psychology": None
                }
                if att and hasattr(att, "psychology"):
                    p = att.psychology
                    day_data["psychology"] = {
                        "dominant_emotion": p.dominant_emotion,
                        "stress_level": p.stress_level,
                        "energy_level": p.energy_level,
                        "mood_score": p.mood_score,
                        "summary_text": p.summary_text or "",
                        "positive_ratio": p.positive_ratio,
                        "negative_ratio": p.negative_ratio,
                        "neutral_ratio": p.neutral_ratio,
                    }
                week_days.append(day_data)
        calendar_weeks.append(week_days)

    # ✅ Kelgan / kelmaganlar statistikasini hisoblash
    total_present = sum(1 for att in attendances)
    
    end_date_for_stats = min(today, last_day)
    if year < today.year or (year == today.year and month < today.month):
        end_date_for_stats = last_day
        
    if end_date_for_stats >= first_day:
        total_absent = sum(1 for d_num in range(1, end_date_for_stats.day + 1) if date(year, month, d_num) not in attendance_map)
    else:
        total_absent = 0

    UZ_MONTHS = {
        1: "Yanvar", 2: "Fevral", 3: "Mart", 4: "Aprel",
        5: "May", 6: "Iyun", 7: "Iyul", 8: "Avgust",
        9: "Sentyabr", 10: "Oktyabr", 11: "Noyabr", 12: "Dekabr"
    }
    month_label = f"{UZ_MONTHS[month]} {year}"

    # ✅ Tanlangan yil bo'yicha yillik va oylik statistika
    annual_attendances = Attendance.objects.filter(user=user, date__year=year)
    attendance_by_month = {m: [] for m in range(1, 13)}
    for a in annual_attendances:
        attendance_by_month[a.date.month].append(a)

    annual_stats = []
    for m in range(1, 13):
        month_atts = attendance_by_month[m]
        came_count = len(month_atts)
        sunday_came_count = sum(1 for a in month_atts if a.date.weekday() == 6)
        
        # O'tgan/joriy/kelasi oyni aniqlash
        if year > today.year or (year == today.year and m > today.month):
            absent_count = 0
            has_data = False
        else:
            has_data = True
            if year == today.year and m == today.month:
                total_days = today.day
            else:
                total_days = monthrange(year, m)[1]
            absent_count = max(0, total_days - came_count)
            
        annual_stats.append({
            "month_num": m,
            "month_name": UZ_MONTHS[m],
            "came_count": came_count,
            "absent_count": absent_count,
            "sunday_came_count": sunday_came_count,
            "has_data": has_data,
        })

    breadcrumbs = [
        {"name": "Bosh sahifa", "url": "/"},
        {"name": "Foydalanuvchilar", "url": "/users/"},
        {"name": "Profil", "url": None},
    ]

    # ✅ Dropdown ro'yxatlari uchun yil va oylar
    years_range = list(range(2024, today.year + 2))
    months_range = [
        {"val": i, "name": UZ_MONTHS[i]} for i in range(1, 13)
    ]

    context = {
        "user_obj": user,
        "telegram_profile": telegram_profile,
        "calendar_weeks": calendar_weeks,
        "total_present": total_present,
        "total_absent": total_absent,
        "month_label": month_label,
        "selected_year": year,
        "selected_month": month,
        "years_range": years_range,
        "months_range": months_range,
        "annual_stats": annual_stats,  # Yillik statistika qo'shildi
        "breadcrumbs": breadcrumbs,
        "recent_attendances": recent_attendances
    }

    return render(request, "users/profile.html", context)



@login_required(login_url='login')
def face_encoding_list_view(request):
    # === Asosiy queryset ===
    encodings_qs = FaceEncoding.objects.select_related('user').order_by('-created_at')

    # === Qidiruv (foydalanuvchi ismi, ID raqamlari, email) ===
    search_query = request.GET.get('q', '').strip()
    if search_query:
        encodings_qs = encodings_qs.filter(
            Q(user__full_name__icontains=search_query) |
            Q(user__username__icontains=search_query) |
            Q(user__student_id_number__icontains=search_query) |
            Q(user__employee_id_number__icontains=search_query) |
            Q(user__email__icontains=search_query)
        )

    # === Rol bo‘yicha filter (student/employee/superadmin) ===
    role_filter = request.GET.get('role')
    if role_filter == 'student':
        encodings_qs = encodings_qs.filter(user__role=CustomUser.Role.STUDENT, user__is_superuser=False)
    elif role_filter == 'employee':
        encodings_qs = encodings_qs.filter(user__role=CustomUser.Role.EMPLOYEE, user__is_superuser=False)
    elif role_filter == 'superadmin':
        encodings_qs = encodings_qs.filter(user__is_superuser=True)

    # === Pagination ===
    paginator = Paginator(encodings_qs, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # === Breadcrumbs ===
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Yuz ma’lumotlari (Face Encodings)', 'url': None},
    ]

    # === Context ===
    context = {
        'encodings': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'breadcrumbs': breadcrumbs,
        'search_query': search_query,
        'current_role': role_filter,
    }
    return render(request, 'users/face_encodings_list.html', context)





@login_required(login_url="login")
def users_list_view(request):
    search_query = (request.GET.get("q") or "").strip()
    current_role = (request.GET.get("role") or "").strip()
    academic_group_id = (request.GET.get("academic_group") or "").strip()
    page_number = request.GET.get("page") or 1

    qs = CustomUser.objects.all()

    # ✅ Qidiruv (ID raqam + telegram username + position ham qo‘shildi)
    if search_query:
        qs = qs.filter(
            Q(full_name__icontains=search_query)
            | Q(short_name__icontains=search_query)
            | Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(student_id_number__icontains=search_query)
            | Q(employee_id_number__icontains=search_query)
            | Q(group_name__icontains=search_query)
            | Q(position__icontains=search_query)
            | Q(department_name__icontains=search_query)
            | Q(specialty__icontains=search_query)
        )

    # ✅ Role filter
    if current_role == "student":
        qs = qs.filter(role=CustomUser.Role.STUDENT, is_superuser=False)
    elif current_role == "employee":
        qs = qs.filter(role=CustomUser.Role.EMPLOYEE, is_superuser=False)
    elif current_role == "superadmin":
        qs = qs.filter(is_superuser=True)

    # ✅ Academic group filter
    if academic_group_id:
        qs = qs.filter(academic_group_id=academic_group_id)

    # ✅ Face encoding bor/yo‘qligi
    face_exists_qs = FaceEncoding.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_face_encoding=Exists(face_exists_qs))

    # ✅ Telegram profile bor/yo‘qligi
    tg_exists_qs = TelegramProfile.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_telegram=Exists(tg_exists_qs))

    # ✅ Top stats cards
    total_count = qs.count()
    student_count = qs.filter(role=CustomUser.Role.STUDENT, is_superuser=False).count()
    employee_count = qs.filter(role=CustomUser.Role.EMPLOYEE, is_superuser=False).count()
    norole_count = qs.filter(Q(role__isnull=True) | Q(role="")).count()

    users_with_face = qs.filter(has_face_encoding=True).count()
    users_without_face = max(0, total_count - users_with_face)

    users_with_tg = qs.filter(has_telegram=True).count()
    users_without_tg = max(0, total_count - users_with_tg)

    # ✅ Sorting
    qs = qs.order_by("-created_at")

    # ✅ Pagination
    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(page_number)

    # ✅ URL params preserve
    keep_params = {}
    if search_query:
        keep_params["q"] = search_query
    if current_role:
        keep_params["role"] = current_role
    if academic_group_id:
        keep_params["academic_group"] = academic_group_id

    base_qs = urlencode(keep_params)

    context = {
        "users": page_obj.object_list,
        "page_obj": page_obj,

        "search_query": search_query,
        "current_role": current_role,
        "selected_academic_group_id": academic_group_id,
        "base_qs": base_qs,

        # stats
        "total_count": total_count,
        "student_count": student_count,
        "employee_count": employee_count,
        "norole_count": norole_count,
        "users_with_face": users_with_face,
        "users_without_face": users_without_face,

        "users_with_tg": users_with_tg,
        "users_without_tg": users_without_tg,
    }

    return render(request, "users/users_list.html", context)


@login_required(login_url="login")
def students_list_view(request):
    search_query = (request.GET.get("q") or "").strip()
    academic_group_id = (request.GET.get("academic_group") or "").strip()
    page_number = request.GET.get("page") or 1

    qs = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, is_superuser=False)

    if search_query:
        qs = qs.filter(
            Q(full_name__icontains=search_query)
            | Q(short_name__icontains=search_query)
            | Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(student_id_number__icontains=search_query)
            | Q(group_name__icontains=search_query)
        )

    if academic_group_id:
        qs = qs.filter(academic_group_id=academic_group_id)

    face_exists_qs = FaceEncoding.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_face_encoding=Exists(face_exists_qs))

    tg_exists_qs = TelegramProfile.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_telegram=Exists(tg_exists_qs))

    total_count = qs.count()
    users_with_face = qs.filter(has_face_encoding=True).count()
    users_without_face = max(0, total_count - users_with_face)
    users_with_tg = qs.filter(has_telegram=True).count()
    users_without_tg = max(0, total_count - users_with_tg)

    qs = qs.order_by("-created_at")

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(page_number)

    from users.models import AcademicGroup
    academic_groups = AcademicGroup.objects.all().order_by("name")

    keep_params = {}
    if search_query:
        keep_params["q"] = search_query
    if academic_group_id:
        keep_params["academic_group"] = academic_group_id

    base_qs = urlencode(keep_params)

    context = {
        "users": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "selected_academic_group_id": academic_group_id,
        "academic_groups": academic_groups,
        "base_qs": base_qs,
        "total_count": total_count,
        "users_with_face": users_with_face,
        "users_without_face": users_without_face,
        "users_with_tg": users_with_tg,
        "users_without_tg": users_without_tg,
    }

    return render(request, "users/students_list.html", context)


@login_required(login_url="login")
def employees_list_view(request):
    search_query = (request.GET.get("q") or "").strip()
    page_number = request.GET.get("page") or 1

    qs = CustomUser.objects.filter(role=CustomUser.Role.EMPLOYEE, is_superuser=False)

    if search_query:
        qs = qs.filter(
            Q(full_name__icontains=search_query)
            | Q(short_name__icontains=search_query)
            | Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(employee_id_number__icontains=search_query)
            | Q(position__icontains=search_query)
            | Q(department_name__icontains=search_query)
            | Q(specialty__icontains=search_query)
        )

    face_exists_qs = FaceEncoding.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_face_encoding=Exists(face_exists_qs))

    tg_exists_qs = TelegramProfile.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_telegram=Exists(tg_exists_qs))

    total_count = qs.count()
    users_with_face = qs.filter(has_face_encoding=True).count()
    users_without_face = max(0, total_count - users_with_face)
    users_with_tg = qs.filter(has_telegram=True).count()
    users_without_tg = max(0, total_count - users_with_tg)

    qs = qs.order_by("-created_at")

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(page_number)

    keep_params = {}
    if search_query:
        keep_params["q"] = search_query

    base_qs = urlencode(keep_params)

    context = {
        "users": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "base_qs": base_qs,
        "total_count": total_count,
        "users_with_face": users_with_face,
        "users_without_face": users_without_face,
        "users_with_tg": users_with_tg,
        "users_without_tg": users_without_tg,
    }

    return render(request, "users/employees_list.html", context)


@login_required(login_url="login")
def import_users_view(request):
    staff_count = CustomUser.objects.filter(role=CustomUser.Role.EMPLOYEE).count()
    student_count = CustomUser.objects.filter(role=CustomUser.Role.STUDENT).count()

    staff_stats = (
        CustomUser.objects
        .filter(role=CustomUser.Role.EMPLOYEE, department_name__isnull=False)
        .values("department_name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    student_stats = (
        CustomUser.objects
        .filter(role=CustomUser.Role.STUDENT, group_name__isnull=False)
        .values("group_name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    return render(
        request,
        "users/users_import.html",
        {
            "breadcrumbs": [
                {"name": "Bosh sahifa", "url": "/"},
                {"name": "Foydalanuvchilarni import qilish", "url": None},
            ],
            "staff_count": staff_count,
            "student_count": student_count,
            "staff_stats": list(staff_stats),
            "student_stats": list(student_stats),
        },
    )


def dc_oauth_login(request):
    import os
    import urllib.parse
    from django.urls import reverse
    from django.shortcuts import redirect
    
    client_id = os.getenv("DC_OAUTH_CLIENT_ID", "6ceea8d1-8d9e-4436-a625-4f9e0857d873")
    
    custom_redirect = os.getenv("DC_OAUTH_REDIRECT_URI", "")
    if custom_redirect:
        redirect_uri = custom_redirect
    else:
        redirect_uri = request.build_absolute_uri(reverse('dc_oauth_callback'))
        if not request.get_host().startswith(('127.0.0.1', 'localhost')):
            if redirect_uri.startswith('http://'):
                redirect_uri = redirect_uri.replace('http://', 'https://', 1)
    
    # Save next URL and origin host in state
    next_url = request.GET.get('next', '')
    current_host = request.get_host()
    if next_url:
        request.session['dc_oauth_next'] = next_url
        state = f"dc_state_host={current_host}&next={next_url}"
    else:
        request.session.pop('dc_oauth_next', None)
        state = f"dc_state_host={current_host}"
    
    auth_url = (
        f"https://dc.namspi.uz/oauth/authorize"
        f"?response_type=code"
        f"&client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&state={urllib.parse.quote(state)}"
    )
    return redirect(auth_url)


def dc_oauth_callback(request):
    import os
    import requests
    import urllib.parse
    import urllib3
    import logging
    from django.urls import reverse
    from django.contrib.auth import login
    from django.contrib import messages
    from django.shortcuts import redirect
    from django.db.models import Q
    from users.models import CustomUser

    logger = logging.getLogger(__name__)

    code = request.GET.get('code')
    state = request.GET.get('state', '')
    
    # Extract host and next_url from state parameter
    origin_host = None
    next_url_from_state = None
    if state and state.startswith("dc_state_"):
        raw_state = state[len("dc_state_"):]
        if raw_state and raw_state != "none":
            if "host=" in raw_state:
                parsed_params = urllib.parse.parse_qs(raw_state)
                origin_host = parsed_params.get('host', [None])[0]
                next_url_from_state = parsed_params.get('next', [None])[0]
            else:
                next_url_from_state = raw_state
    
    if not code:
        messages.error(request, "Avtorizatsiya kodi topilmadi.")
        return redirect('login')
        
    client_id = os.getenv("DC_OAUTH_CLIENT_ID", "6ceea8d1-8d9e-4436-a625-4f9e0857d873")
    client_secret = os.getenv("DC_OAUTH_CLIENT_SECRET", "iB1z1LJQePVCRKhiiTSddw0QbO2N9DBaZQyKkjRmKkJMvOcRKhJYYn0al3cLocCD")
    
    custom_redirect = os.getenv("DC_OAUTH_REDIRECT_URI", "")
    if custom_redirect:
        redirect_uri = custom_redirect
    else:
        redirect_uri = request.build_absolute_uri(reverse('dc_oauth_callback'))
        if not request.get_host().startswith(('127.0.0.1', 'localhost')):
            if redirect_uri.startswith('http://'):
                redirect_uri = redirect_uri.replace('http://', 'https://', 1)
            
    token_url = "https://dc.namspi.uz/oauth/token"
    
    try:
        token_data = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'redirect_uri': redirect_uri,
        }
        
        # Disable insecure request warning for self-signed certificates
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        response = requests.post(token_url, data=token_data, verify=False, timeout=15)
        if response.status_code != 200:
            logger.error("DC OAuth token exchange failed: status=%s response=%s", response.status_code, response.text)
            messages.error(request, f"Token olishda xatolik yuz berdi (status: {response.status_code})")
            return redirect('login')
            
        token_json = response.json()
        access_token = token_json.get('access_token')
        
        if not access_token:
            messages.error(request, "Access token topilmadi.")
            return redirect('login')
            
        # Query user info
        userinfo_url = "https://dc.namspi.uz/oauth/userinfo"
        headers = {
            'Authorization': f'Bearer {access_token}'
        }
        
        user_response = requests.get(userinfo_url, headers=headers, verify=False, timeout=15)
        if user_response.status_code != 200:
            logger.error("DC OAuth userinfo failed: status=%s response=%s", user_response.status_code, user_response.text)
            messages.error(request, "Foydalanuvchi ma'lumotlarini olishda xatolik yuz berdi.")
            return redirect('login')
            
        user_data = user_response.json()
        
        # Support both flat and nested JSON
        payload = user_data.get('data') or user_data.get('user') or user_data
        
        # Extract HEMIS ID / identification
        hemis_id = None
        for key in ["hemis_id", "employee_id_number", "student_id_number", "username", "id", "login", "student_id", "employee_id"]:
            val = payload.get(key)
            if val:
                hemis_id = str(val).strip()
                break
                
        if not hemis_id:
            logger.error("No identifier key found in userinfo: %s", user_data)
            messages.error(request, "OAuth javobidan HEMIS ID aniqlab bo'lmadi.")
            return redirect('login')
            
        # Match user by student_id_number or employee_id_number or username
        user = CustomUser.objects.filter(
            Q(student_id_number=hemis_id) |
            Q(employee_id_number=hemis_id) |
            Q(username=hemis_id)
        ).first()
        
        if user is not None:
            if not user.is_active:
                messages.error(request, "Sizning profilingiz faol emas. Administratorga murojaat qiling.")
                return redirect('login')
                
            login(request, user)
            display_name = user.get_full_name() or user.username
            messages.success(request, f"Xush kelibsiz, {display_name}!")
            
            # Determine target host base
            target_base = f"https://{origin_host}" if origin_host and not origin_host.startswith(('127.0.0.1', 'localhost')) else ""
            
            # Redirect to 'next' URL if it was saved in session or state
            next_url = next_url_from_state or request.session.pop('dc_oauth_next', None)
            if next_url and next_url.startswith('/lesson/v1'):
                from camera.dars_api import generate_dars_token
                token = generate_dars_token(user.id)
                return redirect(f"{target_base}/lesson/v1/?token={token}")
            elif next_url and next_url.startswith('/'):
                return redirect(f"{target_base}{next_url}")
            return redirect(f"{target_base}/dashboard/" if target_base else "dashboard")
        else:
            messages.error(request, f"Tizimda HEMIS ID '{hemis_id}' bo'lgan foydalanuvchi topilmadi.")
            return redirect('login')
            
    except Exception as e:
        logger.exception("DC OAuth exception: %s", e)
        messages.error(request, f"Tizimga kirishda kutilmagan xatolik yuz berdi: {str(e)}")
        return redirect('login')


@login_required(login_url='login')
def unknown_clusters_list_view(request):
    from camera.models import UnknownFaceCluster
    
    # Faqat foydalanuvchiga bog'lanmagan (nomlanmagan) guruhlarni ko'rsatamiz
    clusters = UnknownFaceCluster.objects.filter(associated_user__isnull=True).prefetch_related('faces').order_by('-created_at')
    
    # Qidiruv
    search_query = request.GET.get('q', '').strip()
    if search_query:
        clusters = clusters.filter(name__icontains=search_query)

    # Pagination
    paginator = Paginator(clusters, 15)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': "Noma'lum yuzlar guruhlari", 'url': None},
    ]
    
    context = {
        'clusters': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'breadcrumbs': breadcrumbs,
        'search_query': search_query,
    }
    return render(request, 'users/unknown_clusters_list.html', context)


@login_required(login_url='login')
def associate_cluster_view(request, cluster_id):
    if request.method != 'POST':
        messages.error(request, "Noto'g'ri so'rov usuli.")
        return redirect('unknown_clusters_list')
        
    from camera.models import UnknownFaceCluster
    try:
        cluster = UnknownFaceCluster.objects.get(pk=cluster_id)
        user_id = request.POST.get('user_id')
        if not user_id:
            messages.error(request, "Foydalanuvchi tanlanmadi.")
            return redirect('unknown_clusters_list')
            
        user = CustomUser.objects.get(pk=user_id)
        cluster.associated_user = user
        cluster.save() # Vektorlarni o'rtachalashtiradi va FaceEncoding yaratadi
        
        messages.success(request, f"Guruh muvaffaqiyatli ravishda {user.full_name or user.username} profiliga biriktirildi!")
    except UnknownFaceCluster.DoesNotExist:
        messages.error(request, "Guruh topilmadi.")
    except CustomUser.DoesNotExist:
        messages.error(request, "Foydalanuvchi topilmadi.")
    except Exception as exc:
        logger.exception("Association failed: %s", exc)
        messages.error(request, f"Biriktirishda xatolik yuz berdi: {exc}")
        
    return redirect('unknown_clusters_list')


@login_required(login_url='login')
def run_clustering_trigger_view(request):
    from camera.tasks import cluster_unknown_faces_task
    try:
        cluster_unknown_faces_task.delay()
        messages.success(request, "Noma'lum yuzlarni klasterlash jarayoni fonda ishga tushirildi! Sahifani bir ozdan so'ng yangilang.")
    except Exception as exc:
        messages.error(request, f"Jarayonni ishga tushirishda xatolik yuz berdi: {exc}")
    return redirect('unknown_clusters_list')
@login_required(login_url='login')
def api_search_users(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    users = CustomUser.objects.filter(
        Q(full_name__icontains=q) |
        Q(username__icontains=q) |
        Q(student_id_number__icontains=q) |
        Q(employee_id_number__icontains=q)
    )[:15]
    results = []
    for u in users:
        role_str = "Talaba" if u.role == CustomUser.Role.STUDENT else "Xodim" if u.role == CustomUser.Role.EMPLOYEE else "Admin"
        id_str = u.student_id_number or u.employee_id_number or ""
        results.append({
            'id': u.id,
            'text': f"{u.full_name or u.username} ({role_str} - {id_str})"
        })
    return JsonResponse({'results': results})
