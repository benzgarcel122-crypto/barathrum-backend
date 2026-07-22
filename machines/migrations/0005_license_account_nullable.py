import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    STEP 2.6 (Session 32): reverses the Session 31 rule that a License must have an owner at
    creation time. License.account becomes nullable -- newly generated licenses (via
    generate_license_view or the admin's raw Add form) now start with account=None and only get
    an owner once a Machine successfully claims them (see dashboard/views.py::add_machine_view).

    THIS IS A SCHEMA-ONLY CHANGE (a single AlterField), not a data migration -- there is no
    operation here that writes to any row's account column. Relaxing a NOT NULL column to
    nullable is, at the SQL level, just `ALTER TABLE machines_license ALTER COLUMN account_id
    DROP NOT NULL` on Postgres: it loosens a constraint going forward, it does not read, touch,
    or rewrite any existing row's data. Every License row created under the old Session 31 rule
    keeps whatever account_id it already had -- nothing in this migration nulls, resets, or
    otherwise modifies those values. Only *future* License.objects.create() calls that don't
    pass an account (as generate_license_view and the admin's Add form now do, per this same
    session's view/admin changes) will end up with account=None -- and that's a consequence of
    the application code no longer setting it, not of anything this migration does.
    """

    dependencies = [
        ('machines', '0004_license'),
    ]

    operations = [
        migrations.AlterField(
            model_name='license',
            name='account',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='licenses',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
