import uuid
import logging
import os
from django.db import models
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models.signals import post_delete
from django.dispatch import receiver
from .svg_parser import parse_svg_to_form_fields

logger = logging.getLogger(__name__)
User = get_user_model()

class Tool(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=5.00)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name_plural = "Tools"
        ordering = ['name']
        indexes = [models.Index(fields=['is_active'])]
    
    def __str__(self):
        return self.name

class Template(models.Model):
    TEMPLATE_TYPE_CHOICES = [
        ('tool', 'Tool'),
        ('design', 'Design'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    
    # FIGMA-STYLE STORAGE
    # No 'svg' text field (eliminates 20MB DB bloat).
    # 'svg_file' is the base asset.
    # 'svg_patches' stores all incremental edits.
    svg_file = models.FileField(upload_to='templates/svgs/', blank=True, null=True, help_text="Base SVG file storage")
    svg_patches = models.JSONField(default=list, blank=True, help_text="Incremental edits (Figma-style)")

    banner = models.ImageField(upload_to='template_banners/', blank=True, null=True)
    form_fields = models.JSONField(default=list, blank=True)
    type = models.CharField(max_length=20, choices=TEMPLATE_TYPE_CHOICES)
    tool = models.ForeignKey(Tool, on_delete=models.SET_NULL, null=True, blank=True, related_name='templates')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    hot = models.BooleanField(default=False)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='templates')

    def save(self, *args, **kwargs):
        # 1. Detect SVG Change (either via raw data or direct file update)
        raw_svg = getattr(self, '_raw_svg_data', None)
        svg_file_changed = False
        
        if self.pk:
            try:
                old_instance = Template.objects.get(pk=self.pk)
                if old_instance.svg_file != self.svg_file:
                    svg_file_changed = True
            except Template.DoesNotExist:
                pass
        elif self.svg_file:
            svg_file_changed = True

        # 2. Handle initial ingestion or full overwrite
        if raw_svg or svg_file_changed:
            print(f"[Template.save] New SVG detected for {self.name} (Source: {'raw_data' if raw_svg else 'file_upload'})")
            
            # If we don't have raw_svg but the file changed, read it from the file
            if not raw_svg and self.svg_file:
                try:
                    self.svg_file.seek(0)
                    raw_svg = self.svg_file.read().decode('utf-8')
                except Exception as e:
                    print(f"[Template.save] Error reading new svg_file: {e}")

            if raw_svg:
                # FIX ELEMENT IDs (e.g., depends_ position)
                from .svg_parser import fix_svg_element_ids
                fixed_svg, fixes_made = fix_svg_element_ids(raw_svg)
                if fixes_made > 0:
                    print(f"[Template.save] Fixed {fixes_made} invalid element IDs in new upload.")
                    raw_svg = fixed_svg

                # REGENERATE FORM FIELDS from the NEW SVG
                self.form_fields = parse_svg_to_form_fields(raw_svg)
                print(f"[Template.save] Regenerated {len(self.form_fields)} form fields from new SVG.")

                # CLEAR PATCHES (User requirement: SVG is the state)
                # EXCEPT if Serializer explicitly asked to PRESERVE them (e.g. new base + new patches in same save)
                if self.svg_patches and not getattr(self, '_preserve_patches', False):
                    print(f"[Template.save] Clearing {len(self.svg_patches)} existing patches.")
                    self.svg_patches = []
                elif getattr(self, '_preserve_patches', False):
                    print(f"[Template.save] Preserving {len(self.svg_patches)} patches as requested.")

                # Ensure it's saved as the primary svg_file if provided via raw_svg
                if getattr(self, '_raw_svg_data', None):
                    storage_path = f"templates/svgs/{self.id}.svg"
                    content = ContentFile(raw_svg.encode('utf-8'))
                    if default_storage.exists(storage_path):
                        default_storage.delete(storage_path)
                    default_storage.save(storage_path, content)
                    self.svg_file.name = storage_path
                
                # Clear temporary data to prevent re-triggering
                if hasattr(self, '_raw_svg_data'):
                    delattr(self, '_raw_svg_data')

        # 3. TRIGGER RE-PARSING & BAKING (Manual Admin Button / Force Reparse)
        elif self.pk and self.svg_file and getattr(self, '_force_reparse', False):
            # ... [rest of the force reparse logic remains the same]
            print(f"[Template.save] >>> FORCE REPARSE TRIGGERED for {self.name} (ID: {self.id})")
            try:
                # Step 1: Read base SVG
                print(f"[Template.save] Step 1: Reading base SVG from storage...")
                with self.svg_file.open('rb') as f:
                    base_svg = f.read().decode('utf-8')
                print(f"[Template.save] Base SVG read successfully ({len(base_svg)} chars)")

                # Step 1.5: Fix invalid element IDs (e.g., .upload.grayscale.depends_ → .depends_.upload.grayscale)
                print(f"[Template.save] Step 1.5: Checking and fixing invalid element IDs...")
                from .svg_parser import fix_svg_element_ids
                fixed_svg, fixes_made = fix_svg_element_ids(base_svg)
                if fixes_made > 0:
                    print(f"[Template.save] Fixed {fixes_made} invalid element IDs")
                    base_svg = fixed_svg
                else:
                    print(f"[Template.save] No invalid element IDs found")

                # Step 2: Apply patches
                print(f"[Template.save] Step 2: Applying {len(self.svg_patches or [])} patches...")
                import time
                patch_start = time.time()
                from .svg_utils import apply_svg_patches
                reconstructed_svg = apply_svg_patches(base_svg, self.svg_patches or [])
                patch_duration = time.time() - patch_start
                print(f"[Template.save] Patches applied in {patch_duration:.3f}s - baked SVG is {len(reconstructed_svg)} chars")

                # Validate baked SVG
                if not reconstructed_svg or not reconstructed_svg.strip():
                    print(f"[Template.save] ERROR: Baked SVG is empty after applying patches")
                    raise ValueError("Baked SVG is empty after applying patches")

                # Step 3: Parse form fields from BAKED SVG (after patches — reflects actual field state)
                print(f"[Template.save] Step 3: Parsing form fields from baked SVG...")
                parse_start = time.time()
                parsed_fields = parse_svg_to_form_fields(reconstructed_svg)
                parse_duration = time.time() - parse_start
                print(f"[Template.save] Parse completed in {parse_duration:.3f}s - got {len(parsed_fields) if isinstance(parsed_fields, list) else 'INVALID'} fields")

                # Validate that parsing returned valid data
                if not isinstance(parsed_fields, list):
                    print(f"[Template.save] ERROR: parse_svg_to_form_fields returned {type(parsed_fields)} instead of list")
                    raise ValueError(f"parse_svg_to_form_fields returned invalid data type: {type(parsed_fields)}")

                if len(parsed_fields) == 0:
                    print(f"[Template.save] WARNING: Parse returned 0 fields for {self.name}. Check SVG has valid IDs.")

                self.form_fields = parsed_fields

                # Step 4: Save baked version
                print(f"[Template.save] Step 4: Saving baked SVG to storage...")
                save_start = time.time()
                storage_path = f"templates/svgs/{self.id}.svg"
                content = ContentFile(reconstructed_svg.encode('utf-8'))
                if default_storage.exists(storage_path):
                    default_storage.delete(storage_path)
                default_storage.save(storage_path, content)
                self.svg_file.name = storage_path
                save_duration = time.time() - save_start
                print(f"[Template.save] Baked SVG saved in {save_duration:.3f}s")

                # Step 5: Clear patches
                print(f"[Template.save] Step 5: Clearing {len(self.svg_patches)} patches...")
                self.svg_patches = []

                print(f"[Template.save] >>> FORCE REPARSE COMPLETE for {self.name}. Total fields: {len(self.form_fields)}")
            except Exception as e:
                print(f"[Template.save] Reparse/Bake failed for {self.id}: {e}")
                import traceback
                traceback.print_exc()
                # Re-raise to prevent silent failure - this will be caught by the view
                raise ValueError(f"Reparse failed: {str(e)}") from e

        super().save(*args, **kwargs)

    @property
    def svg_url(self):
        if not self.svg_file:
            return ""
        # Add cache buster timestamp
        url = self.svg_file.url
        buster = f"v={int(self.updated_at.timestamp())}"
        return f"{url}&{buster}" if "?" in url else f"{url}?{buster}"

    def __str__(self):
        return self.name

class PurchasedTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="purchased_templates")
    template = models.ForeignKey("Template", on_delete=models.SET_NULL, null=True, blank=True, related_name="purchases")
    name = models.CharField(max_length=255, blank=True)
    
    # FIGMA-STYLE STORAGE FOR PURCHASES
    # Users store their custom edits as patches too.
    svg_patches = models.JSONField(default=list, blank=True, help_text="Incremental edits made by the user")
    
    # We keep svg_file only as a fallback for bespoke uploads, 
    # but for template purchases, we use the template's base file.
    svg_file = models.FileField(upload_to='purchased_templates/svgs/', blank=True, null=True)
    form_fields = models.JSONField(default=list, blank=True)
    test = models.BooleanField(default=True)
    external_user_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Opaque end-user identifier supplied by the owning API customer",
    )
    tracking_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='purchased_templates')

    class Meta:
        indexes = [
            models.Index(
                fields=["buyer", "external_user_id", "-created_at"],
                name="api_doc_tenant_ext_created_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        # 1. Handle initial SVG ingestion for purchases (bespoke uploads)
        raw_svg = getattr(self, '_raw_svg_data', None)
        if raw_svg:
            storage_path = f"purchased_templates/svgs/{self.id}.svg"
            content = ContentFile(raw_svg.encode('utf-8'))
            if default_storage.exists(storage_path):
                default_storage.delete(storage_path)
            default_storage.save(storage_path, content)
            self.svg_file.name = storage_path
        
        # 2. Inherit basic meta on first save
        if not self.pk and self.template:
            if not self.name:
                self.name = f"My {self.template.name}"
            if not self.svg_patches:
                self.svg_patches = list(self.template.svg_patches)
            if not self.form_fields:
                self.form_fields = list(self.template.form_fields)
            if not self.keywords:
                self.keywords = list(self.template.keywords)
        elif not self.pk and not self.name:
            self.name = "Untitled Document"

        super().save(*args, **kwargs)

        # Handle font inheritance (post-save for M2M)
        if self.template and self.template.fonts.exists() and not self.fonts.exists():
             self.fonts.set(self.template.fonts.all())

    def __str__(self):
        return f"{self.buyer.username} - {self.name}"


class ApiEntitlement(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        REVOKED = "revoked", "Revoked"

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="api_entitlement",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_transaction = models.ForeignKey(
        "wallet.Transaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="api_entitlements",
    )
    activated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} API ({self.status})"


class ApiCustomerSettings(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="api_customer_settings",
    )
    allowed_origins = models.JSONField(default=list, blank=True)
    theme = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} API settings"


class ApiKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=24, unique=True)
    secret_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list, blank=True)
    allowed_origins = models.JSONField(default=list, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "revoked_at"], name="api_key_user_active_idx"),
        ]

    @property
    def is_active(self):
        from django.utils import timezone

        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > timezone.now()
        )

    def __str__(self):
        return f"{self.user.username} - {self.name} ({self.prefix})"


class EmbedSession(models.Model):
    class Operation(models.TextChoices):
        CREATE = "create", "Create"
        EDIT = "edit", "Edit"

    class Mode(models.TextChoices):
        TEST = "test", "Test"
        PAID = "paid", "Paid"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    class PreviewMode(models.TextChoices):
        STANDARD = "standard", "Standard"
        PROTECTED = "protected", "Protected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="embed_sessions")
    api_key = models.ForeignKey(
        ApiKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="embed_sessions",
    )
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="embed_sessions")
    document = models.ForeignKey(
        PurchasedTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="embed_sessions",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    external_user_id = models.CharField(max_length=255)
    allowed_origin = models.URLField(max_length=500)
    operation = models.CharField(
        max_length=8,
        choices=Operation.choices,
        default=Operation.CREATE,
    )
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.TEST)
    preview_mode = models.CharField(
        max_length=16,
        choices=PreviewMode.choices,
        default=PreviewMode.STANDARD,
    )
    prefill = models.JSONField(default=dict, blank=True)
    theme = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status", "-created_at"], name="embed_user_status_created_idx"),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.template.name} ({self.status})"


class DocumentRenderJob(models.Model):
    class Format(models.TextChoices):
        PNG = "png", "PNG"
        PDF = "pdf", "PDF"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="document_render_jobs")
    document = models.ForeignKey(
        PurchasedTemplate,
        on_delete=models.CASCADE,
        related_name="render_jobs",
    )
    requested_by_key = models.ForeignKey(
        ApiKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="render_jobs",
    )
    format = models.CharField(max_length=4, choices=Format.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    output_file = models.FileField(upload_to="api/renders/%Y/%m/%d/", null=True, blank=True)
    output_size = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status", "-created_at"], name="render_user_status_created_idx"),
            models.Index(fields=["expires_at"], name="render_expiry_idx"),
        ]

    def __str__(self):
        return f"{self.document_id} {self.format} ({self.status})"


class ApiIdempotencyRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    api_key = models.ForeignKey(ApiKey, on_delete=models.CASCADE, related_name="idempotency_records")
    operation = models.CharField(max_length=64)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    document = models.ForeignKey(
        PurchasedTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idempotency_records",
    )
    render_job = models.ForeignKey(
        DocumentRenderJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idempotency_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["api_key", "operation", "key"],
                name="unique_api_idempotency_key",
            ),
        ]
        indexes = [
            models.Index(fields=["created_at"], name="api_idempotency_created_idx"),
        ]

class Tutorial(models.Model):
    # A tutorial is scoped to exactly one of: a template, a tool, or neither (general).
    template = models.OneToOneField(Template, on_delete=models.CASCADE, related_name='tutorial', null=True, blank=True)
    tool = models.OneToOneField(Tool, on_delete=models.CASCADE, related_name='tutorial', null=True, blank=True)
    url = models.URLField()
    title = models.CharField(max_length=255, blank=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_featured', '-created_at']
        constraints = [
            models.CheckConstraint(
                check=~(models.Q(template__isnull=False) & models.Q(tool__isnull=False)),
                name='tutorial_single_scope',
            ),
        ]

    def __str__(self):
        return self.title or self.url

class Font(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    family = models.CharField(max_length=255, blank=True)
    weight = models.CharField(max_length=50, default="normal")
    style = models.CharField(max_length=50, default="normal")
    font_file = models.FileField(upload_to='fonts/')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def get_font_format(self):
        ext = os.path.splitext(self.font_file.name or "")[1].lower().lstrip(".")
        return {
            "ttf": "truetype",
            "otf": "opentype",
            "woff": "woff",
            "woff2": "woff2",
        }.get(ext, "truetype")

    def __str__(self):
        return self.name

class SiteSettings(models.Model):
    # 1. Contact & Support Configurations
    whatsapp_number = models.CharField(max_length=50, blank=True, default="2349160914217", help_text="Support WhatsApp Number (e.g. 234...)")
    whatsapp_community_link = models.URLField(blank=True, help_text="WhatsApp Community/Group Invite Link")
    support_email = models.EmailField(blank=True, help_text="Platform Support Email")
    telegram_link = models.URLField(blank=True, help_text="Telegram Group/Channel link")
    twitter_link = models.URLField(blank=True, help_text="Twitter/X link")
    instagram_link = models.URLField(blank=True, help_text="Instagram link")
    tiktok_link = models.URLField(blank=True, help_text="TikTok link")
    
    # Hover Button Toggles
    show_whatsapp_on_hover = models.BooleanField(default=True)
    show_community_on_hover = models.BooleanField(default=True)
    show_telegram_on_hover = models.BooleanField(default=True)
    show_instagram_on_hover = models.BooleanField(default=True)
    show_twitter_on_hover = models.BooleanField(default=True)
    show_tiktok_on_hover = models.BooleanField(default=True)

    # 2. Wallet & Financial Constraints
    min_topup_amount = models.DecimalField(max_digits=10, decimal_places=2, default=5.00, help_text="Minimum top-up amount in USD")
    min_withdrawal_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=10.00, help_text="Minimum withdrawal amount for referral balance")
    crypto_address = models.CharField(max_length=255, blank=True, help_text="Fallback / Master BEP20 USDT Address")
    funding_whatsapp_number = models.CharField(max_length=50, blank=True, default="2349160914217", help_text="WhatsApp for manual deposits")
    exchange_rate_override = models.DecimalField(max_digits=10, decimal_places=2, default=1650.00, help_text="Flat Dollar-to-Naira Exchange Rate (e.g. 1650)")

    # 3. Platform Toggles (Kill Switches)
    maintenance_mode = models.BooleanField(default=False, help_text="Block non-admins with a maintenance screen")
    disable_new_signups = models.BooleanField(default=False, help_text="Temporarily block new users from creating accounts")
    disable_deposits = models.BooleanField(default=False, help_text="Lock the wallet top-up functionality temporarily")
    enable_ai_features = models.BooleanField(default=True, help_text="Global kill-switch for AI Chat and assistant features")

    # API platform access. When upgrade is not required, customers still
    # activate a free entitlement so access can be suspended or revoked.
    enable_api_access = models.BooleanField(default=False, help_text="Global API and embed kill-switch")
    require_api_upgrade = models.BooleanField(default=True, help_text="Charge the configured wallet amount before API activation")
    api_upgrade_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    api_tool_discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Global percentage discount applied to paid API documents",
    )
    api_default_rate_limit = models.PositiveIntegerField(default=120, help_text="Requests per API key per minute")
    api_session_ttl_minutes = models.PositiveIntegerField(default=30, help_text="Maximum lifetime of a hosted embed session")

    # 4. Branding Defaults
    global_announcement_text = models.TextField(blank=True, help_text="Text for global dashboard banner")
    global_announcement_link = models.URLField(blank=True, help_text="Optional link for global banner")
    enable_global_announcement = models.BooleanField(default=False, help_text="Show the global banner to all users")
    
    # 5. Referral Program Configs
    enable_referrals = models.BooleanField(default=True, help_text="Enable the referral program")
    referral_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=10.00, help_text="Percentage of every deposit given to both referrer and invitee (e.g. 10.00)")
    min_referral_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=6.00, help_text="Minimum deposit required by the referred user to trigger reward")

    # ---- Deposit Promo ----
    enable_deposit_promo = models.BooleanField(default=False, help_text="Enable the deposit bonus promo")
    deposit_promo_min_amount = models.DecimalField(max_digits=10, decimal_places=2, default=30.00, help_text="Minimum deposit (USD) to qualify for the bonus")
    deposit_promo_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=100.00, help_text="Bonus percentage of the deposit (e.g. 100.00 = 100%)")
    deposit_promo_max_bonus = models.DecimalField(max_digits=10, decimal_places=2, default=50.00, help_text="Maximum bonus (USD) granted per deposit")
    deposit_promo_expiry_days = models.PositiveIntegerField(default=7, help_text="Days until a granted bonus expires (0 = never)")
    deposit_promo_message = models.TextField(blank=True, default="", help_text="Optional promo copy for the popup/banner (blank = auto-generated)")

    # Legacy / Other Fields
    manual_purchase_text = models.TextField(blank=True)
    dev_name_obfuscated = models.TextField(blank=True)
    owner_name_obfuscated = models.TextField(blank=True)
    # Cache Versioning (Signatures)
    template_cache_version = models.BigIntegerField(default=0, help_text="Global version for template cache busting")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(api_tool_discount_percentage__gte=0)
                    & models.Q(api_tool_discount_percentage__lte=100)
                ),
                name="api_tool_discount_between_0_and_100",
            ),
        ]

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

class TransformVariable(models.Model):
    CATEGORY_CHOICES = [
        ('rotate', 'Rotation'),
        ('scale', 'Scale'),
        ('translateX', 'Position X'),
        ('translateY', 'Position Y'),
    ]
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='rotate')
    value = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['name', 'category']

class AiChatSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_sessions")
    title = models.CharField(max_length=255, default="New Chat")
    template = models.ForeignKey(Template, on_delete=models.SET_NULL, null=True, blank=True)
    purchased_template = models.ForeignKey(PurchasedTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.user.username} - {self.title}"

class AiChatMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('tool', 'Tool'),
        ('system', 'System'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(AiChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True, help_text="Stores tool results, cards, etc.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.session.id} - {self.role}"

class Referral(models.Model):
    referrer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='referral_records')
    referred_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registration_referral')
    is_rewarded = models.BooleanField(default=True)
    reward_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.referrer.username} -> {self.referred_user.username}"

@receiver(post_delete, sender=Template)
def auto_delete_file_on_delete_template(sender, instance, **kwargs):
    """Deletes base SVG file from storage when Template is deleted."""
    if instance.svg_file:
        try:
            if default_storage.exists(instance.svg_file.name):
                default_storage.delete(instance.svg_file.name)
                logger.info("[Signal] Deleted base SVG for Template %s", instance.id)
        except Exception as e:
            logger.error(f"Failed to delete SVG file for template {instance.id}: {e}")

@receiver(post_delete, sender=PurchasedTemplate)
def auto_delete_file_on_delete_purchase(sender, instance, **kwargs):
    """Deletes baked SVG file from storage when purchase is deleted."""
    if instance.svg_file:
        try:
            if default_storage.exists(instance.svg_file.name):
                default_storage.delete(instance.svg_file.name)
                logger.info("[Signal] Deleted baked SVG for Purchase %s", instance.id)
        except Exception as e:
            logger.error(f"Failed to delete SVG file for purchase {instance.id}: {e}")


@receiver(post_delete, sender=DocumentRenderJob)
def auto_delete_render_file(sender, instance, **kwargs):
    """Delete expired/revoked render artifacts from the configured storage."""
    if instance.output_file:
        try:
            instance.output_file.delete(save=False)
        except Exception as exc:
            logger.error("Failed to delete render output %s: %s", instance.id, exc)
