from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0053_sitesettings_payment_gateway"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="sitesettings",
            name="payment_gateway",
        ),
    ]
