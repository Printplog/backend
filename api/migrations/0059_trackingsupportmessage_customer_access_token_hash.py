from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0058_integrationsecret"),
    ]

    operations = [
        migrations.AddField(
            model_name="trackingsupportmessage",
            name="customer_access_token_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
