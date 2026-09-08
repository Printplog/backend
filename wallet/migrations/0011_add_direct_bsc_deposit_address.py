import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("wallet", "0010_add_onchain_deposit")]

    operations = [
        migrations.CreateModel(
            name="DirectBSCDepositAddress",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("address", models.CharField(max_length=42, unique=True)),
                ("encrypted_private_key", models.TextField()),
                ("start_block", models.PositiveBigIntegerField()),
                ("last_scanned_block", models.PositiveBigIntegerField()),
                ("detected_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "transaction",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="direct_bsc_route",
                        to="wallet.transaction",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["last_scanned_block", "created_at"], name="wallet_dire_last_sc_b2efa5_idx")],
            },
        ),
    ]
