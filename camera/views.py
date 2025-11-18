#camera/views.py

import cv2
from django.shortcuts import render
from django.contrib.auth.decorators import login_required

def get_usb_cameras(max_cams=5):
    cams = []
    for i in range(max_cams):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            cams.append({'id': i, 'name': f'USB Kamera {i}'})
            cap.release()
    return cams

@login_required(login_url='login')
def usb_camera_view(request):
    cameras = get_usb_cameras()
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Kameralar', 'url': '/cameras/list/'},
        {'name': 'USB Kamera', 'url': None},
    ]
    return render(request, 'cameras/usb_camera.html', {
        'cameras': cameras,
        'breadcrumbs': breadcrumbs
    })
