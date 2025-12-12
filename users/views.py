from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages

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
