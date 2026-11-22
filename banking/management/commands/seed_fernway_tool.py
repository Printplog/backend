from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand, CommandError
from api.models import Tool


class Command(BaseCommand):
    help = (
        "Safely seed the Fernway Online Banking tool in production without overwriting "
        "existing admin edits unless --update is explicitly passed."
    )

    def add_arguments(self, parser):
        parser.add_argument('--name', type=str, help='Tool display name')
        parser.add_argument('--description', type=str, help='Tool description')
        parser.add_argument('--price', type=str, help='Monthly subscription price in USD (e.g. 15.00)')
        parser.add_argument('--badge', type=str, help='Tool badge label (e.g. "HOT TOOL")')
        parser.add_argument('--active', action='store_true', help='Mark tool active/visible')
        parser.add_argument('--inactive', action='store_true', help='Mark tool inactive/hidden')
        parser.add_argument(
            '--update',
            action='store_true',
            help='Apply supplied options to an existing Fernway tool row',
        )

    def handle(self, *args, **options):
        price_val = None
        if options.get('price') is not None:
            try:
                price_val = Decimal(str(options['price']).strip())
                if price_val < 0:
                    raise InvalidOperation()
            except InvalidOperation as exc:
                raise CommandError('Invalid --price value. Provide a non-negative decimal like 15.00.') from exc

        if options.get('active') and options.get('inactive'):
            raise CommandError('Cannot specify both --active and --inactive.')

        tool = Tool.objects.filter(slug='fernway').first()
        if not tool:
            tool = Tool.objects.filter(name__iexact='Fernway Online Banking').first()

        if not tool:
            tool = Tool.objects.create(
                name=options.get('name') or 'Fernway Online Banking',
                slug='fernway',
                description=options.get('description') or (
                    'Create and manage interactive online banking accounts with custom balances, '
                    'physical debit cards, transaction PINs, and transfer approval.'
                ),
                price=price_val if price_val is not None else Decimal('15.00'),
                is_active=False if options.get('inactive') else True,
                is_pinned=True,
                badge=options.get('badge') or 'HOT TOOL',
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created Fernway tool ({tool.id}): {tool.name} @ ${tool.price}/mo (active={tool.is_active})"
                )
            )
            return

        changed = []
        if not tool.slug:
            tool.slug = 'fernway'
            changed.append('slug')

        if options.get('update'):
            if options.get('name'):
                tool.name = options['name']
                changed.append('name')
            if options.get('description') is not None:
                tool.description = options['description']
                changed.append('description')
            if price_val is not None:
                tool.price = price_val
                changed.append('price')
            if options.get('badge') is not None:
                tool.badge = options['badge']
                changed.append('badge')
            if options.get('active'):
                tool.is_active = True
                changed.append('is_active')
            elif options.get('inactive'):
                tool.is_active = False
                changed.append('is_active')

        if changed:
            tool.save(update_fields=changed)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated Fernway tool ({tool.id}) fields={changed}: {tool.name} @ ${tool.price}/mo (active={tool.is_active})"
                )
            )
        else:
            self.stdout.write(
                f"Fernway tool already seeded ({tool.id}): {tool.name} @ ${tool.price}/mo (active={tool.is_active}). "
                "No changes made (pass --update to modify)."
            )
