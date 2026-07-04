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
    def debit(self, amount: Decimal, *, description=''):
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("Debit amount must be positive")

        if self.balance < amount:
            raise ValidationError("Insufficient wallet balance")

        self.balance -= amount
        self.save(update_fields=['balance'])

        tx = Transaction.objects.create(
            wallet=self,
            type=Transaction.Type.PAYMENT,
            amount=-amount,
            status=Transaction.Status.COMPLETED,
            description=description
        )

        # Send Payment Email
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
