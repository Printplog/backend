import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0055_apiusageevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrackingSupportMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("tracking_id", models.CharField(max_length=100)),
                ("source", models.CharField(choices=[("parcel_finda", "ParcelFinda"), ("flight_lookup", "MyFlightLookup")], max_length=24)),
                ("customer_name", models.CharField(max_length=120)),
                ("customer_email", models.EmailField(max_length=254)),
                ("subject", models.CharField(max_length=160)),
                ("message", models.TextField(max_length=5000)),
                ("status", models.CharField(choices=[("new", "New"), ("read", "Read"), ("closed", "Closed")], default="new", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="support_messages", to="api.purchasedtemplate")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["document", "-created_at"], name="support_doc_created_idx"),
                    models.Index(fields=["status", "-created_at"], name="support_status_created_idx"),
                ],
            },
        ),
    ]
