import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    STEP 2.5 (Session 31): adds the new License table (standalone license-key generation,
    decoupled from Add Machine). This is a pure additive CreateModel -- no data migration is
    included here.

    Deliberately NOT backfilling License rows for existing Machine rows' license_keys: those
    keys were already generated *and claimed* inline under the old Add Machine flow, so a
    retroactive License row would misrepresent them as "generated, possibly still unclaimed"
    when they've in fact always been attached to a Machine since the moment they were created.
    There's also currently very little real production data to reconcile. Flagged explicitly in
    the Session 31 report for the PM/Investigator to confirm this call.
    """

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('machines', '0003_payment_account_wallet_rework'),
    ]

    operations = [
        migrations.CreateModel(
            name='License',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('license_key', models.CharField(editable=False, max_length=15, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'account',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='licenses',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
