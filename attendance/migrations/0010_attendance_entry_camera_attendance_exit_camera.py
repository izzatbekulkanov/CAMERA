# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('attendance', '0009_sitesettings_bot_token'),
        ('camera', '0005_camera_rtsp_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='attendance',
            name='entry_camera',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='entry_attendances',
                to='camera.camera',
                verbose_name='Kirish kamerasi'
            ),
        ),
        migrations.AddField(
            model_name='attendance',
            name='exit_camera',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='exit_attendances',
                to='camera.camera',
                verbose_name='Chiqish kamerasi'
            ),
        ),
    ]
