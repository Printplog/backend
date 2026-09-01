from decimal import Decimal

from django.db import migrations, models


def backfill_received_amount(apps, schema_editor):
    webhook_event = apps.get_model("wallet", "CryptAPIWebhookEvent")
    webhook_event.objects.update(amount_received=models.F("amount_forwarded"))


class Migration(migrations.Migration):
    dependencies = [
        ("wallet", "0008_cpaydepositroute_cpay_transaction_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="cryptapiwebhookevent",
            name="amount_received",
            field=models.DecimalField(
                decimal_places=6,
                default=Decimal("0"),
                max_digits=18,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="cryptapiwebhookevent",
            name="cost_absorbed",
            field=models.DecimalField(
                decimal_places=6,
                default=Decimal("0"),
                max_digits=18,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_received_amount, migrations.RunPython.noop),
    ]
