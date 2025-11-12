from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages

def login_view(request):
    # 🔹 Agar foydalanuvchi allaqachon login bo‘lgan bo‘lsa — dashboard sahifasiga yo‘naltirish
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, f"Xush kelibsiz, {user.username}!")
            return redirect('dashboard')  # Dashboard sahifasiga o‘tadi
        else:
            messages.error(request, "Login yoki parol noto‘g‘ri!")
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
