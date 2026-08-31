import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
from users.models import CustomUser
user = CustomUser.objects.filter(first_name__icontains="XUSAN").first()
if user:
    print("Full_name:", user.full_name)
    print("First_name:", user.first_name)
    print("Second_name:", user.second_name)
    print("Third_name:", user.third_name)
    print("Last_name:", getattr(user, 'last_name', ''))
    print("get_full_name():", user.get_full_name())
