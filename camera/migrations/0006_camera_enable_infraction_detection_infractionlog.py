# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('camera', '0005_camera_rtsp_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='camera',
            name='enable_infraction_detection',
            field=models.BooleanField(default=False, verbose_name='Huquqbuzarliklarni aniqlash yoqilsinmi?'),
        ),
        migrations.CreateModel(
            name='InfractionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('infraction_type', models.CharField(choices=[('fight', 'Urush/Janjal'), ('smoking', 'Sigaret chekish'), ('other', 'Boshqa huquqbuzarlik')], max_length=50, verbose_name='Huquqbuzarlik turi')),
                ('image', models.ImageField(blank=True, null=True, upload_to='infraction_photos/%Y/%m/%d/', verbose_name='Rasm')),
                ('timestamp', models.DateTimeField(auto_now_add=True, verbose_name="Sodir bo'lgan vaqt")),
                ('confidence', models.FloatField(default=0.0, verbose_name='Ishonchlilik')),
                ('is_resolved', models.BooleanField(default=False, verbose_name='Hal etildi')),
                ('camera', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='infractions', to='camera.camera', verbose_name='Kamera')),
            ],
            options={
                'verbose_name': 'Huquqbuzarlik',
                'verbose_name_plural': 'Huquqbuzarliklar',
                'ordering': ['-timestamp'],
            },
        ),
    ]
