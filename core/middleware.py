class DefaultLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Django da til uchun ishlatiladigan session kaliti: '_language'
        LANGUAGE_SESSION_KEY = '_language'
        
        # Agar foydalanuvchi tilni o'zi tanlamagan bo'lsa (cookie yoki sessionda yo'q bo'lsa),
        # uni default 'uz' tiliga o'tkazamiz va brauzer yuborgan Accept-Language sarlavhasini o'chiramiz.
        # Bu yangi foydalanuvchilar uchun saytni doimo O'zbek tilida yuklanishini kafolatlaydi,
        # biroq foydalanuvchi boshqa tilni tanlasa (Masalan, Ruscha), uni hurmat qiladi.
        language_cookie = request.COOKIES.get('django_language')
        language_session = request.session.get(LANGUAGE_SESSION_KEY)
        
        if not language_cookie and not language_session:
            request.session[LANGUAGE_SESSION_KEY] = 'uz'
            if 'HTTP_ACCEPT_LANGUAGE' in request.META:
                del request.META['HTTP_ACCEPT_LANGUAGE']
                
        response = self.get_response(request)
        return response
