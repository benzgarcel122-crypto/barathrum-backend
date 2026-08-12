from django.db import migrations


def backfill_license_points(apps, schema_editor):
    License = apps.get_model("machines", "License")
    Machine = apps.get_model("machines", "Machine")

    # Every Machine row (regardless of removed_at -- released-but-kept machines still carry a
    # real days_remaining balance per existing design, so their license should align too),
    # mapped by the license_key string match this codebase always uses between the two tables
    # (no FK exists between them, by original design).
    days_by_license_key = dict(Machine.objects.values_list("license_key", "days_remaining"))

    licenses_to_update = []
    for lic in License.objects.filter(license_key__in=days_by_license_key.keys()):
        lic.license_points = days_by_license_key[lic.license_key]
        licenses_to_update.append(lic)

    if licenses_to_update:
        License.objects.bulk_update(licenses_to_update, ["license_points"])


def noop_reverse(apps, schema_editor):
    # Deliberately irreversible in any meaningful sense -- there is no way to recover what
    # license_points held before this backfill ran (the whole point was that those old values
    # were wrong/mismatched). A no-op reverse lets `migrate` still walk backward through this
    # migration without erroring, rather than silently re-mismatching data on a rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("machines", "0011_license_license_points"),
    ]

    operations = [
        migrations.RunPython(backfill_license_points, noop_reverse),
    ]
