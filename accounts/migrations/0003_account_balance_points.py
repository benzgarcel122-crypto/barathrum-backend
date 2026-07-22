from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_otpcode_failed_attempts'),
    ]

    operations = [
        migrations.AddField(
            model_name='account',
            name='balance_points',
            field=models.IntegerField(default=0),
        ),
    ]
