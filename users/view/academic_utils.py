# users/view/academic_utils.py
import logging
from django.db import transaction
from users.models import CustomUser, Faculty, Curriculum, AcademicGroup

logger = logging.getLogger(__name__)

def populate_academic_groups_from_existing_students():
    """
    Mavjud talabalar ma'lumotlaridan (group_name, department_name, specialty, education_year)
    foydalanib Faculty, Curriculum va AcademicGroup obyektlarini yaratadi va bog'laydi.
    """
    students = CustomUser.objects.filter(role=CustomUser.Role.STUDENT)
    updated_count = 0
    created_groups = 0
    created_faculties = 0
    created_curriculums = 0

    for student in students:
        group_name = student.group_name
        dept_name = student.department_name
        dept_code = student.department_code
        specialty_name = student.specialty
        education_year = student.education_year

        if not group_name:
            continue

        try:
            with transaction.atomic():
                faculty_obj = None
                if dept_name:
                    faculty_obj, created_f = Faculty.objects.get_or_create(
                        name=dept_name,
                        defaults={"code": dept_code}
                    )
                    if created_f:
                        created_faculties += 1
                    if dept_code and faculty_obj.code != dept_code:
                        faculty_obj.code = dept_code
                        faculty_obj.save(update_fields=['code'])

                curriculum_obj = None
                if specialty_name:
                    curriculum_obj, created_c = Curriculum.objects.get_or_create(
                        name=specialty_name
                    )
                    if created_c:
                        created_curriculums += 1

                academic_group, created_g = AcademicGroup.objects.get_or_create(
                    name=group_name,
                    defaults={
                        "faculty": faculty_obj,
                        "curriculum": curriculum_obj,
                        "education_year": education_year
                    }
                )
                if created_g:
                    created_groups += 1

                # Sync fields if group existed but differed
                updated_g = False
                if academic_group.faculty != faculty_obj:
                    academic_group.faculty = faculty_obj
                    updated_g = True
                if academic_group.curriculum != curriculum_obj:
                    academic_group.curriculum = curriculum_obj
                    updated_g = True
                if education_year and academic_group.education_year != education_year:
                    academic_group.education_year = education_year
                    updated_g = True
                if updated_g:
                    academic_group.save()

                if student.academic_group != academic_group:
                    student.academic_group = academic_group
                    student.save(update_fields=['academic_group'])
                    updated_count += 1
        except Exception:
            logger.exception("Error syncing student %s", student.id)
            continue

    return {
        "updated_students": updated_count,
        "created_groups": created_groups,
        "created_faculties": created_faculties,
        "created_curriculums": created_curriculums
    }
