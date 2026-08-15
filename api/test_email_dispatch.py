from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import transaction
from django.test import TestCase

from api.utils.email_service import EmailService
from wallet.models import Wallet

User = get_user_model()


class EmailDispatchTests(TestCase):
    """
    Mail must leave the request path. SMTP costs >1s per message, and it used to
    be charged to whichever request triggered it.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="mailer", email="mailer@example.com", password="pw"
        )
        mail.outbox = []

    def test_send_does_not_transmit_inline(self):
        # No commit happens inside a TestCase, so nothing should go out.
        EmailService.send_welcome_email(self.user)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_transmits_once_the_transaction_commits(self):
        with self.captureOnCommitCallbacks(execute=True):
            EmailService.send_welcome_email(self.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["mailer@example.com"])

    def test_rolled_back_work_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    EmailService.send_welcome_email(self.user)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_recipient_is_dropped(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertFalse(
                EmailService._send_email("Subject", "emails/auth/welcome.html", {}, [""])
            )
            self.assertFalse(
                EmailService._send_email("Subject", "emails/auth/welcome.html", {}, [])
            )
        self.assertEqual(len(mail.outbox), 0)

    def test_queue_payload_is_json_serialisable(self):
        """A Decimal or model instance in the context would break the broker."""
        import json

        captured = {}

        def fake_delay(subject, template_name, context, recipients):
            captured["payload"] = json.dumps(
                [subject, template_name, context, recipients]
            )

        with patch("api.tasks.send_email.delay", side_effect=fake_delay):
            with self.captureOnCommitCallbacks(execute=True):
                EmailService.send_wallet_funded(
                    self.user, Decimal("25.50"), Decimal("100.00"), "tx-1", "Deposit"
                )
        self.assertIn("payload", captured)

    def test_broker_failure_falls_back_to_inline_send(self):
        with patch("api.tasks.send_email.delay", side_effect=OSError("broker down")):
            with self.captureOnCommitCallbacks(execute=True):
                EmailService.send_welcome_email(self.user)
        # A dead broker must not silently swallow a password reset.
        self.assertEqual(len(mail.outbox), 1)


class WalletEmailTransactionTests(TestCase):
    """
    credit() and debit() are @transaction.atomic. The send used to sit inside
    that block, holding the wallet row lock open across an SMTP round-trip and
    mailing "wallet funded" even when the transaction later rolled back.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="walletmail", email="walletmail@example.com", password="pw"
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        mail.outbox = []

    def test_credit_mails_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.wallet.credit(Decimal("50.00"), description="Deposit")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Wallet Funded", mail.outbox[0].subject)

    def test_rolled_back_credit_mails_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.wallet.credit(Decimal("50.00"), description="Deposit")
                    raise RuntimeError("payment reversed")
            except RuntimeError:
                pass
        self.assertEqual(len(mail.outbox), 0)
