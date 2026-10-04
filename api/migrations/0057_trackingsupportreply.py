import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0056_trackingsupportmessage"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrackingSupportReply",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("direction", models.CharField(choices=[("customer", "Customer"), ("owner", "Document owner")], max_length=12)),
                ("body", models.TextField(max_length=10000)),
                ("sender_email", models.EmailField(max_length=254)),
                ("delivery_status", models.CharField(choices=[("received", "Received"), ("queued", "Queued"), ("sent", "Sent"), ("delivered", "Delivered"), ("delayed", "Delayed"), ("bounced", "Bounced"), ("failed", "Failed"), ("suppressed", "Suppressed"), ("complained", "Complained")], default="received", max_length=16)),
                ("resend_email_id", models.CharField(blank=True, max_length=100, null=True, unique=True)),
                ("external_message_id", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("support_message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="replies", to="api.trackingsupportmessage")),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.AddIndex(
            model_name="trackingsupportreply",
            index=models.Index(fields=["support_message", "created_at"], name="support_reply_thread_idx"),
        ),
    ]
