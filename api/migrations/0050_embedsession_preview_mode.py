from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0049_documentrenderjob_apiidempotencyrecord_render_job_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="embedsession",
            name="preview_mode",
            field=models.CharField(
                choices=[("standard", "Standard"), ("protected", "Protected")],
                default="standard",
                max_length=16,
            ),
        ),
    ]
