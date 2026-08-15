from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0050_embedsession_preview_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="embedsession",
            name="operation",
            field=models.CharField(
                choices=[("create", "Create"), ("edit", "Edit")],
                default="create",
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="embedsession",
            name="document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="embed_sessions",
                to="api.purchasedtemplate",
            ),
        ),
    ]
