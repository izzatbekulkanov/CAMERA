from urllib.parse import quote

from django import template


register = template.Library()


@register.filter
def thumb(image_field, preset="avatar"):
    """
    Return a lightweight thumbnail URL for an ImageField/FileField.
    Original media URLs are still used for full-size preview links.
    """
    name = getattr(image_field, "name", "") or ""
    if not name:
        return ""

    version = ""
    try:
        modified = image_field.storage.get_modified_time(name)
        version = f"?v={int(modified.timestamp())}"
    except Exception:
        pass

    return f"/media-thumb/{quote(str(preset), safe='')}/{quote(name, safe='/')}{version}"
