from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("wallet", "0011_add_direct_bsc_deposit_address")]

    operations = [
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="gas_funding_signed_transaction",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="gas_funding_transaction_hash",
            field=models.CharField(blank=True, default="", max_length=66),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="sweep_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="sweep_signed_transaction",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="sweep_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("funding", "Funding gas"),
                    ("sweeping", "Sweeping"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="sweep_transaction_hash",
            field=models.CharField(blank=True, default="", max_length=66),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="swept_amount",
            field=models.DecimalField(
                decimal_places=18,
                default=Decimal("0"),
                max_digits=30,
            ),
        ),
        migrations.AddField(
            model_name="directbscdepositaddress",
            name="swept_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
