import math
import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


def generate_auth_token():
    return uuid.uuid4().hex


class FernwayProfile(models.Model):
    SUBSCRIPTION_STATUS_CHOICES = [
        ('test', 'Test Mode'),
        ('active', 'Active'),
        ('expired', 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='fernway_profiles',
    )
    first_name = models.CharField(max_length=100, default='Alexander')
    last_name = models.CharField(max_length=100, default='Hamilton')
    email = models.EmailField(default='alexander@fernwaybank.com')
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone = models.CharField(max_length=50, default='+1 (555) 019-2834', blank=True)
    address = models.CharField(max_length=255, default='742 Evergreen Terrace, New York, NY 10001', blank=True)
    password = models.CharField(max_length=128, default='password123')
    transaction_pin = models.CharField(max_length=4, default='1234')
    theme = models.CharField(max_length=50, default='fernway-ember')
    auth_token = models.CharField(max_length=64, default=generate_auth_token, unique=True)
    is_created = models.BooleanField(default=False)
    is_test = models.BooleanField(default=True)
    subscription_status = models.CharField(
        max_length=20,
        choices=SUBSCRIPTION_STATUS_CHOICES,
        default='test',
    )
    subscription_started_at = models.DateTimeField(null=True, blank=True)
    subscription_expires_at = models.DateTimeField(null=True, blank=True)
    last_renewal_reminder_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fernway Bank Profile"
        verbose_name_plural = "Fernway Bank Profiles"
        ordering = ['-created_at']

    @property
    def effective_subscription_status(self) -> str:
        if not self.is_created or self.is_test:
            return 'test'
        if self.subscription_expires_at and self.subscription_expires_at <= timezone.now():
            return 'expired'
        if self.subscription_status == 'expired':
            return 'expired'
        return 'active'

    @property
    def is_live(self) -> bool:
        return self.effective_subscription_status == 'active'

    @property
    def days_remaining(self) -> int:
        if not self.is_live or not self.subscription_expires_at:
            return 0
        delta_seconds = (self.subscription_expires_at - timezone.now()).total_seconds()
        if delta_seconds <= 0:
            return 0
        return max(1, math.ceil(delta_seconds / 86400))

    @property
    def needs_renewal_reminder(self) -> bool:
        status_val = self.effective_subscription_status
        if status_val == 'expired':
            return True
        return status_val == 'active' and self.days_remaining <= 5

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"


class FernwayAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('checking', 'Checking'),
        ('savings', 'Savings'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(FernwayProfile, on_delete=models.CASCADE, related_name='accounts')
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES)
    name = models.CharField(max_length=100, default='Primary Checking')
    account_number = models.CharField(max_length=30, default='1049283749')
    routing_number = models.CharField(max_length=30, default='021000021')
    balance = models.BigIntegerField(default=25000000)  # In cents ($250,000.00)
    apy_bps = models.IntegerField(default=410)  # 410 = 4.10%
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['account_type']

    def __str__(self):
        return f"{self.name} - ${self.balance / 100:,.2f}"


class FernwayCard(models.Model):
    CARD_KIND_CHOICES = [
        ('physical', 'Physical'),
        ('virtual', 'Virtual'),
    ]
    CARD_BRAND_CHOICES = [
        ('visa', 'Visa'),
        ('mastercard', 'Mastercard'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(FernwayProfile, on_delete=models.CASCADE, related_name='cards')
    account = models.ForeignKey(FernwayAccount, on_delete=models.CASCADE, related_name='cards', null=True, blank=True)
    label = models.CharField(max_length=100, default='Metal Black Card')
    kind = models.CharField(max_length=20, choices=CARD_KIND_CHOICES, default='physical')
    brand = models.CharField(max_length=20, choices=CARD_BRAND_CHOICES, default='visa')
    card_number = models.CharField(max_length=30, default='4532 9182 7364 8892')
    last4 = models.CharField(max_length=4, default='8892')
    expiry = models.CharField(max_length=7, default='12/28')
    cvv = models.CharField(max_length=4, default='721')
    pin = models.CharField(max_length=4, default='1234')
    holder_name = models.CharField(max_length=150, default='Alexander Hamilton')
    monthly_limit = models.BigIntegerField(default=5000000)  # In cents ($50,000.00)
    is_frozen = models.BooleanField(default=False)
    online_payments = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.brand.upper()} ending in {self.last4} ({self.holder_name})"


class FernwayTransaction(models.Model):
    DIRECTION_CHOICES = [
        ('credit', 'Credit'),
        ('debit', 'Debit'),
    ]
    STATUS_CHOICES = [
        ('completed', 'Completed'),
        ('pending', 'Pending'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(FernwayAccount, on_delete=models.CASCADE, related_name='transactions')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    amount = models.BigIntegerField()  # In cents
    fee = models.BigIntegerField(default=0)  # In cents
    counterparty = models.CharField(max_length=255)
    counterparty_detail = models.CharField(max_length=255, blank=True, null=True)
    bank_name = models.CharField(max_length=120, blank=True, default='')
    recipient_account_number = models.CharField(max_length=64, blank=True, default='')
    recipient_routing_number = models.CharField(max_length=64, blank=True, default='')
    narration = models.CharField(max_length=255, blank=True, default='')
    category = models.CharField(max_length=50, default='income')
    channel = models.CharField(max_length=20, default='wire')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='completed')
    reference = models.CharField(max_length=100)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']

    def __str__(self):
        sign = "+" if self.direction == "credit" else "-"
        return f"{sign}${self.amount / 100:,.2f} - {self.counterparty} ({self.occurred_at.strftime('%Y-%m-%d')})"
