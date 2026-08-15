from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.api_security import DEFAULT_API_KEY_SCOPES, generate_api_key, normalize_origin
from api.models import ApiEntitlement, ApiKey


DEMO_KEY_NAME = "Hosted form demo"


class Command(BaseCommand):
    help = "Issue a development-only API key for the standalone hosted-form demo."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--origin", default="http://127.0.0.1:4188")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo keys can only be issued while DEBUG is enabled.")
        try:
            origin = normalize_origin(options["origin"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        User = get_user_model()
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError(f"User '{options['username']}' does not exist.") from exc

        ApiEntitlement.objects.update_or_create(
            user=user,
            defaults={"status": ApiEntitlement.Status.ACTIVE},
        )
        ApiKey.objects.filter(
            user=user,
            name=DEMO_KEY_NAME,
            revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())

        token, prefix, token_hash = generate_api_key()
        ApiKey.objects.create(
            user=user,
            name=DEMO_KEY_NAME,
            prefix=prefix,
            secret_hash=token_hash,
            scopes=DEFAULT_API_KEY_SCOPES,
            allowed_origins=[origin],
        )

        # Intentionally print only the one-time secret. This makes command
        # substitution possible without saving the key to a file.
        self.stdout.write(token)
