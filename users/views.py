from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.db.models import Q, Exists, OuterRef, Count

from users.models import FaceEncoding, CustomUser
from urllib.parse import urlencode


def login_view(request):
    # Agar foydalanuvchi allaqachon login bo'lsa — dashboardga yuboramiz
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        # Login/parol bo'sh bo'lsa
        if not username or not password:
            messages.error(request, "Iltimos, login va parolni to‘liq kiriting.")
            return redirect('login')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if not user.is_active:
                messages.error(
                    request,
                    "Sizning profilingiz faol emas. Iltimos, administratorga murojaat qiling."
                )
                return redirect('login')

            login(request, user)
            display_name = user.get_full_name() or user.username
            messages.success(request, f"Xush kelibsiz, {display_name}!")
            return redirect('dashboard')
        else:
            messages.error(
                request,
                "Login yoki parol noto‘g‘ri. Iltimos, qayta urinib ko‘ring."
            )
            return redirect('login')

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

@login_required(login_url='login')
def profile_view(request):
    user = request.user  # hozirgi foydalanuvchi

    # Breadcrumbs ma’lumotlari
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},  # agar sizda home url mavjud bo‘lsa
        {'name': 'Profil', 'url': None},  # oxirgi element faqat matn, link bo‘lmaydi
    ]

    context = {
        'user': user,
        'breadcrumbs': breadcrumbs,  # shu yerda qo‘shildi
    }
    return render(request, 'users/my_profile.html', context)



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




@login_required(login_url='login')
def users_list_view(request):
    """
    Users list:
      - qidiruv: ?q=
      - role filter: ?role=student|employee|superadmin (superadmin=role bo'yicha emas, is_superuser bo'yicha)
      - pagination: ?page=
      - top cards: student/employee/norole + face encoding bor/yo'q
    """

    search_query = (request.GET.get("q") or "").strip()
    current_role = (request.GET.get("role") or "").strip()
    page_number = request.GET.get("page") or 1

    # --- Base queryset ---
    qs = CustomUser.objects.all()

    # --- Qidiruv ---
    if search_query:
        qs = qs.filter(
            Q(full_name__icontains=search_query)
            | Q(short_name__icontains=search_query)
            | Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(contact_phone__icontains=search_query)
            | Q(student_id_number__icontains=search_query)
            | Q(employee_id_number__icontains=search_query)
        )

    # --- Role filter ---
    # Eslatma: modelda faqat student/employee bor, ammo siz template’da superadmin ham qo‘yibsiz.
    # Shuning uchun superadmin’ni is_superuser orqali filter qilamiz.
    if current_role == "student":
        qs = qs.filter(role=CustomUser.Role.STUDENT, is_superuser=False)
    elif current_role == "employee":
        qs = qs.filter(role=CustomUser.Role.EMPLOYEE, is_superuser=False)
    elif current_role == "superadmin":
        qs = qs.filter(is_superuser=True)

    # --- has_face_encoding (tez, N+1 yo'q) ---
    face_exists_qs = FaceEncoding.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_face_encoding=Exists(face_exists_qs))

    # --- Top stats cards (FILTERLANGAN queryset bo‘yicha) ---
    total_count = qs.count()

    # student / employee (superuserlarni alohida ko‘rsatish uchun is_superuser=False deb ham qo‘shdik)
    student_count = qs.filter(role=CustomUser.Role.STUDENT, is_superuser=False).count()
    employee_count = qs.filter(role=CustomUser.Role.EMPLOYEE, is_superuser=False).count()

    # "role yo‘q" — role bo‘sh/null bo‘lsa (data quality)
    norole_count = qs.filter(Q(role__isnull=True) | Q(role="")).count()

    # face encoding bor/yo‘q
    users_with_face = qs.filter(has_face_encoding=True).count()
    users_without_face = max(0, total_count - users_with_face)

    # --- Ordering (xohlasangiz) ---
    qs = qs.order_by("-created_at")

    # --- Pagination ---
    paginator = Paginator(qs, 50)  # sahifada 20 ta user
    page_obj = paginator.get_page(page_number)

    # Pagination linklarida q va role saqlanib qolishi uchun (template’ga beramiz)
    keep_params = {}
    if search_query:
        keep_params["q"] = search_query
    if current_role:
        keep_params["role"] = current_role
    base_qs = urlencode(keep_params)
    # template’da ishlatish: ?{{ base_qs }}&page=2 yoki faqat page

    context = {
        "users": page_obj.object_list,
        "page_obj": page_obj,

        "search_query": search_query,
        "current_role": current_role,
        "base_qs": base_qs,

        # top cards
        "total_count": total_count,
        "student_count": student_count,
        "employee_count": employee_count,
        "norole_count": norole_count,
        "users_with_face": users_with_face,
        "users_without_face": users_without_face,

        # breadcrumbs bo‘lsa:
        # "breadcrumbs": [...],
    }

    return render(request, "users/users_list.html", context)


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

