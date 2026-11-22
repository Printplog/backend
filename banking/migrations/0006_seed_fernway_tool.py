from decimal import Decimal
from django.db import migrations


def seed_fernway_tool(apps, schema_editor):
    """
    Safely ensure the Fernway Online Banking Tool record exists in production.
    Never overwrites an existing row so admin edits (price, name, active flag, badge)
    are preserved across future deployments.
    """
    Tool = apps.get_model('api', 'Tool')

    if Tool.objects.filter(slug='fernway').exists():
        return

    existing_by_name = Tool.objects.filter(name__iexact='Fernway Online Banking').first()
    if existing_by_name:
        if not existing_by_name.slug:
            existing_by_name.slug = 'fernway'
            existing_by_name.save(update_fields=['slug'])
        return

    Tool.objects.create(
        name='Fernway Online Banking',
        slug='fernway',
        description=(
            'Create and manage interactive online banking accounts with custom balances, '
            'physical debit cards, transaction PINs, and transfer approval.'
        ),
        price=Decimal('15.00'),
        is_active=True,
        is_pinned=True,
        badge='HOT TOOL',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0061_alter_tool_options_tool_badge_tool_is_pinned_and_more'),
        ('banking', '0005_fernwayprofile_transaction_pin'),
    ]

    operations = [
        migrations.RunPython(seed_fernway_tool, migrations.RunPython.noop),
    ]
