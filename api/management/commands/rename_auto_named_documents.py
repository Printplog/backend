from django.core.management.base import BaseCommand

from api.document_naming import default_document_name
from api.models import PurchasedTemplate


class Command(BaseCommand):
    help = (
        'Rename documents that still have the automatic "My <Template>" name after the person '
        'on them, e.g. "Jane Doe - USA Passport". Dry run unless --apply is given; names users '
        'chose themselves are never touched.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Save the new names (default: only report).')
        parser.add_argument('--samples', type=int, default=15, help='How many example renames to print.')

    def handle(self, *args, apply=False, samples=15, **options):
        docs = (
            PurchasedTemplate.objects.select_related('template')
            .filter(template__isnull=False)
            .only('id', 'name', 'form_fields', 'template__id', 'template__name')
        )
        checked = 0
        pending = []
        for doc in docs.iterator(chunk_size=500):
            checked += 1
            automatic = {f'My {doc.template.name}', '', 'Untitled Document'}
            if (doc.name or '') not in automatic:
                continue
            new_name = default_document_name(doc.template, doc.form_fields or [])
            if new_name != doc.name:
                pending.append((doc, new_name))

        for doc, new_name in pending[:samples]:
            self.stdout.write(f'  "{doc.name}" -> "{new_name}"')
        if len(pending) > samples:
            self.stdout.write(f'  ... and {len(pending) - samples} more')

        if not apply:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {len(pending)} of {checked} documents would be renamed. Run with --apply to save.'
            ))
            return

        for doc, new_name in pending:
            doc.name = new_name
        # bulk_update writes only the name, without running PurchasedTemplate.save()
        PurchasedTemplate.objects.bulk_update([doc for doc, _ in pending], ['name'], batch_size=500)
        self.stdout.write(self.style.SUCCESS(f'Renamed {len(pending)} of {checked} documents.'))
