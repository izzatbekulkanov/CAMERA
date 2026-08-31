import re

with open('/home/smartgate/web/SmartGate/templates/attendance/attendance.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Remove custom border-radius block
content = re.sub(r'/\* ===== Filter panel mobile friendly ===== \*/.*?border-radius: 12px;\n    }', '', content, flags=re.DOTALL)

# 2. Update container-fluid
content = content.replace('<div class="container-fluid py-4">', '<div class="container-fluid">')

# 3. Add Top header
header = """
    <div class="d-flex justify-content-between align-items-center mb-3">
        <div>
            <h4 class="mb-0">Bugungi Davomat</h4>
            <small class="text-muted">Foydalanuvchilarning kirish, chiqish va hududdagi holatini kuzatish (Face ID)</small>
        </div>
    </div>
"""
content = content.replace('{% include \'includes/breadcrumbs.html\' with breadcrumbs=breadcrumbs %}', '{% include \'includes/breadcrumbs.html\' with breadcrumbs=breadcrumbs %}\n' + header)

# 4. Update Cards
content = content.replace('card border-0 shadow mb-3', 'card shadow-sm border mb-3')
content = content.replace('card border-0 shadow', 'card shadow-sm border')
content = content.replace('card-header bg-white', 'card-header bg-transparent')

# 5. Buttons
content = content.replace('btn btn-dark btn-sm w-100 rounded-3', 'btn btn-primary btn-sm w-100')

# 6. Badges
content = content.replace('badge bg-success', 'badge bg-soft-success text-success fw-semibold font-size-12')
content = content.replace('badge bg-danger', 'badge bg-soft-danger text-danger fw-semibold font-size-12')

# 7. Card footer
content = content.replace('card-footer bg-white', 'card-footer bg-transparent')

with open('/home/smartgate/web/SmartGate/templates/attendance/attendance.html', 'w', encoding='utf-8') as f:
    f.write(content)
