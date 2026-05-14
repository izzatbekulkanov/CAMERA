import os
import django
import urllib.request
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from camera.models import Camera

data = json.loads(urllib.request.urlopen('http://10.10.0.48:1984/api/streams').read())
streams = list(data.keys())
updated = 0

for stream in streams:
    if '@' in stream and ':' in stream:
        try:
            ip_part = stream.split('@')[1].split(':')[0]
            if ip_part:
                cams = Camera.objects.filter(ip=ip_part)
                for c in cams:
                    c.rtsp_url = stream
                    c.save()
                    updated += 1
        except Exception:
            pass

print('Updated', updated, 'cameras')
