import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_payment_account_from_machine_owner(apps, schema_editor):
    """
    STEP 2.4 data migration: STEP 2.3's Payment rows were FK'd to a Machine, not an Account.
    Before dropping that FK, backfill the new `account` field from each existing Payment's
    machine.owner, so no historical payment record is silently lost or orphaned.

    This matters concretely: the STEP 2.3 live sandbox test run created at least one real
    Payment row in production tied to a real Machine. This migration preserves that row's
    account linkage, amount, status, and timestamps. What does NOT survive: `bundle_type` and
    `days`, which are dropped in this same migration -- those concepts don't apply to a
    wallet-funding Payment at all post-STEP 2.4, so there's nothing meaningful to migrate them
    into. This was a deliberate, flagged call (see the STEP 2.4 report), not a silent drop.
    """
    Payment = apps.get_model("machines", "Payment")
    for payment in Payment.objects.select_related("machine__owner").all():
        payment.account_id = payment.machine.owner_id
        payment.save(update_fields=["account"])


def noop_reverse(apps, schema_editor):
    """
    Intentional no-op reverse: reversing this migration would need to re-attach every Payment to
    *some* Machine, but a Payment created after STEP 2.4 (account-level wallet funding) may not
    correspond to any single machine at all -- there's no correct machine to reverse-assign it
    to. If this migration is ever rolled back, any Payment rows created after STEP 2.4 will need
    manual review rather than an automatic reverse.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('machines', '0002_payment'),
    ]

    operations = [
        # Step 1: add the new FK as nullable so existing rows don't need a value yet.
        migrations.AddField(
            model_name='payment',
            name='account',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='payments', to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Step 2: backfill account from each Payment's machine.owner (data-preserving).
        migrations.RunPython(backfill_payment_account_from_machine_owner, noop_reverse),
        # Step 3: now that every row has an account, make it required.
        migrations.AlterField(
            model_name='payment',
            name='account',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='payments', to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Step 4: drop the old machine FK and the bundle-specific fields that no longer apply to
        # an account-level wallet-funding Payment (see backfill function docstring above).
        migrations.RemoveField(model_name='payment', name='machine'),
        migrations.RemoveField(model_name='payment', name='bundle_type'),
        migrations.RemoveField(model_name='payment', name='days'),
    ]
