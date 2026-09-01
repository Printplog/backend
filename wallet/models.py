from django.db import models, transaction
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from decimal import Decimal
import uuid
from accounts.models import User
# User = get_user_model()

def generate_tx_id():
    return str(uuid.uuid4())

class Transaction(models.Model):
    class Type(models.TextChoices):
        DEPOSIT = 'deposit', 'Deposit'
        PAYMENT = 'payment', 'Payment'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey("Wallet", on_delete=models.CASCADE, related_name='transactions')
    type = models.CharField(max_length=10, choices=Type.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    description = models.CharField(max_length=255, blank=True)
    tx_hash = models.CharField(max_length=255, blank=True, db_index=True)
    tx_id = models.CharField(max_length=36, unique=True, default=generate_tx_id)
    address = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['wallet', 'status']),
        ]

    def __str__(self):
        return f"{self.type.title()} ${abs(self.amount)} ({self.status})"



class Wallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    referral_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    bonus_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.user.username}'s Wallet"

    @property
    def spendable_balance(self):
        return self.balance + self.bonus_balance

    @transaction.atomic
    def credit_bonus(self, amount, *, expires_at=None, source_transaction=None, percentage=None):
        amount = Decimal(amount).quantize(Decimal("0.01"))
        if amount <= 0:
            raise ValueError("Bonus amount must be positive")
        bonus = DepositBonus.objects.create(
            wallet=self,
            source_transaction=source_transaction,
            amount_granted=amount,
            amount_remaining=amount,
            percentage_applied=percentage if percentage is not None else Decimal("0.00"),
            expires_at=expires_at,
            status=DepositBonus.Status.ACTIVE,
        )
        self.bonus_balance = self.bonus_balance + amount
        self.save(update_fields=["bonus_balance"])
        return bonus

    @transaction.atomic
    def credit_referral(self, amount: Decimal):
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("Credit amount must be positive")
        self.referral_balance += amount
        self.save(update_fields=['referral_balance'])

    @transaction.atomic
    def credit(self, amount: Decimal, *, description='Deposit', create_transaction=True):
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("Credit amount must be positive")

        self.balance += amount
        self.save(update_fields=['balance'])

        tx_to_return = None
        if create_transaction:
            tx_to_return = Transaction.objects.create(
                wallet=self,
                type=Transaction.Type.DEPOSIT,
                amount=amount,
                status=Transaction.Status.COMPLETED,
                description=description
            )
            # Use the newly created transaction ID for the email if possible
            final_tx_id = tx_to_return.tx_id
        else:
            final_tx_id = "Auto-Credit"

        # Send Email Notification
        try:
            from api.utils.email_service import EmailService
            EmailService.send_wallet_funded(self.user, amount, self.balance, final_tx_id, description)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send wallet funding email: {e}")

        return tx_to_return

    @transaction.atomic
    def debit(self, amount, *, description=''):
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("Debit amount must be positive")

        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        if wallet.spendable_balance < amount:
            raise ValidationError("Insufficient wallet balance")

        remaining = amount
        bonus_used = Decimal("0.00")
        active_bonuses = (
            wallet.bonuses.select_for_update()
            .filter(status=DepositBonus.Status.ACTIVE, amount_remaining__gt=0)
            .order_by(models.F("expires_at").asc(nulls_last=True), "granted_at")
        )
        for b in active_bonuses:
            if remaining <= 0:
                break
            take = min(b.amount_remaining, remaining)
            b.amount_remaining = b.amount_remaining - take
            if b.amount_remaining <= 0:
                b.status = DepositBonus.Status.SPENT
            b.save(update_fields=["amount_remaining", "status"])
            remaining -= take
            bonus_used += take

        if bonus_used:
            wallet.bonus_balance = wallet.bonus_balance - bonus_used
        if remaining > 0:
            wallet.balance = wallet.balance - remaining
        wallet.save(update_fields=["balance", "bonus_balance"])

        # keep the caller's instance consistent
        self.balance = wallet.balance
        self.bonus_balance = wallet.bonus_balance

        tx = Transaction.objects.create(
            wallet=wallet,
            type=Transaction.Type.PAYMENT,
            amount=-amount,
            status=Transaction.Status.COMPLETED,
            description=description,
        )

        try:
            from api.utils.email_service import EmailService
            EmailService.send_payment_notification(self.user, amount, self.balance, tx.tx_id, description)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send payment receipt email: {e}")

        return tx

class WithdrawalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='withdrawal_requests')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    usdt_address = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.amount} ({self.status})"


class CPayDepositRoute(models.Model):
    """One freshly generated CPay client wallet for a deposit."""

    class RouteType(models.TextChoices):
        DIRECT = "cpay", "Legacy CPay direct"
        CRYPTAPI_BRIDGE = "cryptapi_cpay", "CryptAPI to CPay bridge"

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        FORWARDED = "forwarded", "Forwarded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.CASCADE,
        related_name="cpay_route",
    )
    route_type = models.CharField(
        max_length=24,
        choices=RouteType.choices,
        default=RouteType.CRYPTAPI_BRIDGE,
    )
    client_reference = models.CharField(max_length=64, unique=True)
    cpay_wallet_id = models.CharField(max_length=128, unique=True)
    cpay_address = models.CharField(max_length=128, unique=True)
    encrypted_passphrase = models.TextField()
    encrypted_callback_nonce = models.TextField(blank=True, default="")
    # Encrypted because the URL contains the per-payment callback nonce.
    cryptapi_callback_url = models.TextField(blank=True, default="")
    cryptapi_address_in = models.CharField(max_length=128, blank=True)
    cryptapi_callback_id = models.CharField(max_length=128, blank=True)
    cryptapi_txid_in = models.CharField(max_length=255, blank=True)
    cryptapi_txid_out = models.CharField(max_length=255, blank=True)
    cpay_transaction_id = models.CharField(max_length=128, blank=True)
    cpay_tx_hash = models.CharField(max_length=255, blank=True)
    forwarded_amount = models.DecimalField(max_digits=18, decimal_places=6, default=Decimal("0"))
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CREATED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["cryptapi_callback_id"],
                condition=~models.Q(cryptapi_callback_id=""),
                name="unique_nonempty_cryptapi_callback_id",
            ),
            models.UniqueConstraint(
                fields=["cryptapi_txid_in"],
                condition=~models.Q(cryptapi_txid_in=""),
                name="unique_nonempty_cryptapi_txid_in",
            ),
        ]

    def __str__(self):
        return f"{self.transaction_id} -> {self.cpay_address}"


class CryptAPIWebhookEvent(models.Model):
    """Immutable idempotency receipt for each confirmed CryptAPI callback."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    route = models.ForeignKey(CPayDepositRoute, on_delete=models.PROTECT, related_name="webhook_events")
    callback_id = models.CharField(max_length=128, unique=True)
    txid_in = models.CharField(max_length=255, unique=True)
    txid_out = models.CharField(max_length=255, blank=True)
    amount_received = models.DecimalField(max_digits=18, decimal_places=6)
    amount_forwarded = models.DecimalField(max_digits=18, decimal_places=6)
    cost_absorbed = models.DecimalField(max_digits=18, decimal_places=6)
    credited_transaction = models.OneToOneField(
        Transaction,
        on_delete=models.PROTECT,
        related_name="cryptapi_event",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.callback_id}: {self.amount_received} received"


class CPayWebhookEvent(models.Model):
    """Idempotency receipt for a provider-verified CPay direct deposit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    route = models.ForeignKey(CPayDepositRoute, on_delete=models.PROTECT, related_name="cpay_webhook_events")
    provider_transaction_id = models.CharField(max_length=128, unique=True)
    tx_hash = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=6)
    credited_transaction = models.OneToOneField(
        Transaction,
        on_delete=models.PROTECT,
        related_name="cpay_event",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tx_hash"],
                condition=~models.Q(tx_hash=""),
                name="unique_nonempty_cpay_tx_hash",
            ),
        ]

    def __str__(self):
        return f"{self.provider_transaction_id}: {self.amount}"


class RevenueDistributionConfig(models.Model):
    """Singleton operational policy; provider credentials remain in env."""

    enabled = models.BooleanField(default=False)
    threshold_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("100.00"))
    last_available_balance = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    last_balance_checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(threshold_amount__gt=0),
                name="revenue_distribution_threshold_positive",
            ),
        ]

    @classmethod
    def get_config(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Revenue distribution (${self.threshold_amount})"


class RevenueShareRecipient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    bep20_address = models.CharField(max_length=42)
    percentage = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(percentage__gt=0) & models.Q(percentage__lte=100),
                name="revenue_recipient_percentage_valid",
            ),
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(is_active=True),
                name="unique_active_revenue_recipient_email",
            ),
            models.UniqueConstraint(
                fields=["bep20_address"],
                condition=models.Q(is_active=True),
                name="unique_active_revenue_recipient_address",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.percentage}%)"


class RevenueDistributionBatch(models.Model):
    class Status(models.TextChoices):
        PREPARING = "preparing", "Preparing"
        SENDING = "sending", "Sending"
        SUBMITTED = "submitted", "Submitted"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.DecimalField(max_digits=18, decimal_places=6)
    threshold_amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=18, decimal_places=6)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PREPARING)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"${self.amount} ({self.status})"


class RevenueDistributionPayout(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUBMITTED = "submitted", "Submitted"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        RevenueDistributionBatch,
        on_delete=models.CASCADE,
        related_name="payouts",
    )
    recipient = models.ForeignKey(
        RevenueShareRecipient,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts",
    )
    recipient_name = models.CharField(max_length=120)
    recipient_email = models.EmailField()
    bep20_address = models.CharField(max_length=42)
    percentage = models.DecimalField(max_digits=5, decimal_places=2)
    amount = models.DecimalField(max_digits=18, decimal_places=6)
    idempotency_key = models.CharField(max_length=128, unique=True)
    provider_transaction_id = models.CharField(max_length=128, blank=True)
    transaction_hash = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_transaction_id"],
                condition=~models.Q(provider_transaction_id=""),
                name="unique_nonempty_cpay_payout_transaction",
            ),
        ]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"{self.recipient_email}: {self.amount} USDT ({self.status})"


class DepositBonus(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SPENT = "spent", "Spent"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey("Wallet", on_delete=models.CASCADE, related_name="bonuses")
    source_transaction = models.ForeignKey(
        "Transaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="deposit_bonuses"
    )
    amount_granted = models.DecimalField(max_digits=12, decimal_places=2)
    amount_remaining = models.DecimalField(max_digits=12, decimal_places=2)
    percentage_applied = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["expires_at", "granted_at"]
        indexes = [
            models.Index(fields=["wallet", "status"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"Bonus {self.amount_remaining}/{self.amount_granted} ({self.status})"
