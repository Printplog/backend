from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.conf import settings


class UserManager(BaseUserManager):
    def create_user(self, username, email, password=None, **extra_fields):
        if not username:
            raise ValueError("The username is required")
        if not email:
            raise ValueError("The email is required")

        email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(username, email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    
    downloads = models.IntegerField(default=0)
    referred_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals_sent')
    source = models.CharField(max_length=100, default='Direct', null=True, blank=True, help_text="Traffic source (e.g. google, facebook, direct)")
    medium = models.CharField(max_length=100, default='(none)', null=True, blank=True, help_text="Acquisition medium (e.g. organic, referral, cpc)")
    campaign = models.CharField(max_length=150, null=True, blank=True, help_text="Acquisition campaign name")
    term = models.CharField(max_length=150, null=True, blank=True, help_text="Acquisition term")
    content = models.CharField(max_length=150, null=True, blank=True, help_text="Acquisition content")
    source_platform = models.CharField(max_length=100, null=True, blank=True, help_text="Acquisition source platform")
    gclid = models.CharField(max_length=500, null=True, blank=True, help_text="Google Click ID")
    fbclid = models.CharField(max_length=500, null=True, blank=True, help_text="Facebook Click ID")
    google_id = models.CharField(max_length=255, null=True, blank=True, unique=True)


    objects = UserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email']

    def __str__(self):
        return self.username


class AdminTwoFactorProfile(models.Model):
    """Encrypted authenticator-app configuration for privileged accounts."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_two_factor",
    )
    encrypted_secret = models.TextField()
    recovery_code_hashes = models.JSONField(default=list, blank=True)
    confirmed_at = models.DateTimeField()
    last_used_counter = models.BigIntegerField(default=-1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "admin two-factor profile"
        verbose_name_plural = "admin two-factor profiles"

    def __str__(self):
        return f"2FA for {self.user.username}"
