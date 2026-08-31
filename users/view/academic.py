# users/view/academic.py
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.db.models import Count, Q, OuterRef, Exists
from django.contrib import messages
from users.models import Faculty, Curriculum, AcademicGroup, CustomUser, FaceEncoding, TelegramProfile
from users.view.academic_utils import populate_academic_groups_from_existing_students


@login_required(login_url="login")
def academic_groups_view(request):
    # Sinxronizatsiya qilinmagan talabalar bo'lsa va guruhlar jadvali bo'sh bo'lsa,
    # avtomatik ravishda birinchi marta to'ldirib qo'yamiz.
    if AcademicGroup.objects.count() == 0:
        populate_academic_groups_from_existing_students()

    # Queryset
    qs = AcademicGroup.objects.select_related('faculty', 'curriculum').annotate(
        student_count=Count('students')
    ).order_by('name')

    # Qidiruv
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(name__icontains=q)

    # Fakultet filtri
    faculty_id = request.GET.get('faculty')
    if faculty_id:
        qs = qs.filter(faculty_id=faculty_id)

    # O'quv reja filtri
    curriculum_id = request.GET.get('curriculum')
    if curriculum_id:
        qs = qs.filter(curriculum_id=curriculum_id)

    # O'quv yili filtri
    edu_year = request.GET.get('education_year')
    if edu_year:
        qs = qs.filter(education_year=edu_year)

    # Stats
    total_groups = AcademicGroup.objects.count()
    total_students_in_groups = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, academic_group__isnull=False).count()
    total_faculties = Faculty.objects.count()
    total_curriculums = Curriculum.objects.count()

    # Pagination
    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Dropdowns
    faculties = Faculty.objects.all().order_by('name')
    curriculums = Curriculum.objects.all().order_by('name')
    education_years = AcademicGroup.objects.exclude(education_year__isnull=True).exclude(education_year="").values_list('education_year', flat=True).distinct().order_by('education_year')

    context = {
        "page_obj": page_obj,
        "faculties": faculties,
        "curriculums": curriculums,
        "education_years": list(education_years),
        "total_groups": total_groups,
        "total_students_in_groups": total_students_in_groups,
        "total_faculties": total_faculties,
        "total_curriculums": total_curriculums,
        "selected_faculty": faculty_id,
        "selected_curriculum": curriculum_id,
        "selected_education_year": edu_year,
        "q": q,
        "breadcrumbs": [
            {"name": "Bosh sahifa", "url": "/"},
            {"name": "Akademik guruhlar", "url": None},
        ]
    }
    return render(request, "users/academic_groups_list.html", context)


@login_required(login_url="login")
def faculties_view(request):
    # Queryset
    qs = Faculty.objects.annotate(
        group_count=Count('groups', distinct=True),
        student_count=Count('groups__students', distinct=True)
    ).order_by('name')

    # Stats
    total_faculties = Faculty.objects.count()
    total_groups = AcademicGroup.objects.count()
    total_students = CustomUser.objects.filter(role=CustomUser.Role.STUDENT).count()

    # Chart data for all faculties
    faculties_list = Faculty.objects.all().order_by('name')
    chart_labels = []
    chart_males = []
    chart_females = []
    for fac in faculties_list:
        chart_labels.append(fac.name)
        males = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, academic_group__faculty=fac, gender='Erkak').count()
        females = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, academic_group__faculty=fac, gender='Ayol').count()
        chart_males.append(males)
        chart_females.append(females)

    # Pagination
    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "total_faculties": total_faculties,
        "total_groups": total_groups,
        "total_students": total_students,
        "chart_labels": chart_labels,
        "chart_males": chart_males,
        "chart_females": chart_females,
        "breadcrumbs": [
            {"name": "Bosh sahifa", "url": "/"},
            {"name": "Fakultetlar", "url": None},
        ]
    }
    return render(request, "users/faculties_list.html", context)


@login_required(login_url="login")
def curriculums_view(request):
    # Queryset
    qs = Curriculum.objects.annotate(
        group_count=Count('groups', distinct=True),
        student_count=Count('groups__students', distinct=True)
    ).order_by('name')

    # Stats
    total_curriculums = Curriculum.objects.count()
    total_groups = AcademicGroup.objects.count()
    total_students = CustomUser.objects.filter(role=CustomUser.Role.STUDENT).count()

    # Pagination
    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "total_curriculums": total_curriculums,
        "total_groups": total_groups,
        "total_students": total_students,
        "breadcrumbs": [
            {"name": "Bosh sahifa", "url": "/"},
            {"name": "O'quv rejalari", "url": None},
        ]
    }
    return render(request, "users/curriculums_list.html", context)


@login_required(login_url="login")
def sync_existing_academic_data(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    res = populate_academic_groups_from_existing_students()
    return JsonResponse({
        "success": True,
        "message": f"Sinxronizatsiya muvaffaqiyatli yakunlandi! "
                   f"{res['updated_students']} ta talaba yangi akademik guruhlar bilan bog'landi, "
                   f"{res['created_groups']} ta guruh, {res['created_faculties']} ta fakultet, "
                   f"{res['created_curriculums']} ta o'quv reja yaratildi."
    })


@login_required(login_url="login")
def academic_group_students_view(request, group_id):
    group = get_object_or_404(AcademicGroup, id=group_id)

    # Queryset for students in this group
    qs = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, academic_group=group)

    # Search
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(username__icontains=q) |
            Q(student_id_number__icontains=q)
        )

    # Annotate face and telegram profiles
    face_exists_qs = FaceEncoding.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_face_encoding=Exists(face_exists_qs))

    tg_exists_qs = TelegramProfile.objects.filter(user_id=OuterRef("pk"))
    qs = qs.annotate(has_telegram=Exists(tg_exists_qs))

    # Stats
    total_students = qs.count()
    male_count = qs.filter(gender='Erkak').count()
    female_count = qs.filter(gender='Ayol').count()

    # Sorting
    qs = qs.order_by('full_name')

    # Pagination
    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        "group": group,
        "page_obj": page_obj,
        "total_students": total_students,
        "male_count": male_count,
        "female_count": female_count,
        "q": q,
        "breadcrumbs": [
            {"name": "Bosh sahifa", "url": "/"},
            {"name": "Akademik guruhlar", "url": "/users/academic/groups/"},
            {"name": f"{group.name} talabalari", "url": None},
        ]
    }
    return render(request, "users/academic_group_students.html", context)

