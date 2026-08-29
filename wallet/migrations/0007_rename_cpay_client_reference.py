from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("wallet", "0006_cpaydepositroute_cryptapiwebhookevent_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="cpaydepositroute",
            old_name="cpay_client_id",
            new_name="client_reference",
        ),
    ]
