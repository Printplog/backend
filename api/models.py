import uuid
import logging
from django.db import models
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
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
        # 1. Handle initial ingestion or full overwrite
        raw_svg = getattr(self, '_raw_svg_data', None)
        if raw_svg:
            self.form_fields = parse_svg_to_form_fields(raw_svg)
            filename = f"{self.id}.svg"
            self.svg_file.save(filename, ContentFile(raw_svg.encode('utf-8')), save=False)
        
    def save(self, *args, **kwargs):
        # 1. Handle initial ingestion or full overwrite
        raw_svg = getattr(self, '_raw_svg_data', None)
        if raw_svg:
            self.form_fields = parse_svg_to_form_fields(raw_svg)
            filename = f"{self.id}.svg"
            self.svg_file.save(filename, ContentFile(raw_svg.encode('utf-8')), save=False)
        
        # 2. TRIGGER RE-PARSING ONLY IF FORCED (Manual Admin Button)
        # We no longer auto-reparse on patches to ensure maximum speed.
        elif self.pk and self.svg_file and getattr(self, '_force_reparse', False):
            try:
                with self.svg_file.open('rb') as f:
                    base_svg = f.read().decode('utf-8')
                
                from .svg_utils import apply_svg_patches
                reconstructed_svg = apply_svg_patches(base_svg, self.svg_patches or [])
                self.form_fields = parse_svg_to_form_fields(reconstructed_svg)
                print(f"[Template.save] Manual structural re-parse complete.")
            except Exception as e:
                print(f"[Template.save] Reparse skipped/failed: {e}")

        super().save(*args, **kwargs)

    @property
    def svg_url(self):
        return self.svg_file.url if self.svg_file else ""

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
    tracking_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='purchased_templates')

    def save(self, *args, **kwargs):
        # 1. Handle initial SVG ingestion for purchases (bespoke uploads)
        raw_svg = getattr(self, '_raw_svg_data', None)
        if raw_svg:
            filename = f"{self.id}.svg"
            self.svg_file.save(filename, ContentFile(raw_svg.encode('utf-8')), save=False)
        
        # 2. Inherit basic meta on first save
        if not self.pk and self.template:
            if not self.svg_patches:
                self.svg_patches = list(self.template.svg_patches)
            if not self.form_fields:
                self.form_fields = list(self.template.form_fields)
            if not self.keywords:
                self.keywords = list(self.template.keywords)

        # 3. FIGMA-STYLE STRUCTURE SYNC:
        # We ONLY re-parse if explicitly forced.
        if getattr(self, '_force_reparse', False) and not raw_svg:
            svg_source = self.svg_file if self.svg_file else (self.template.svg_file if self.template else None)
            
            if svg_source:
                try:
                    with svg_source.open('rb') as f:
                        base_svg = f.read().decode('utf-8')
                    
                    from .svg_utils import apply_svg_patches
                    reconstructed_svg = apply_svg_patches(base_svg, self.svg_patches or [])
                    
                    new_structure = parse_svg_to_form_fields(reconstructed_svg)
                    current_values = {f['id']: f.get('currentValue') for f in (self.form_fields or []) if 'id' in f}
                    
                    for field in new_structure:
                        fid = field.get('id')
                        if fid in current_values:
                            field['currentValue'] = current_values[fid]
                    
                    self.form_fields = new_structure
                    print(f"[PurchasedTemplate.save] Manual re-parse complete.")
                except Exception as e:
                    print(f"[PurchasedTemplate.save] Reparse skipped: {e}")

        super().save(*args, **kwargs)

        # 4. Handle font inheritance (post-save for M2M)
        if self.template and self.template.fonts.exists() and not self.fonts.exists():
             self.fonts.set(self.template.fonts.all())

    def __str__(self):
        return f"{self.buyer.username} - {self.name}"

class Tutorial(models.Model):
    template = models.OneToOneField(Template, on_delete=models.CASCADE, related_name='tutorial')
    url = models.URLField()
    title = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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

    def __str__(self):
        return self.name

class SiteSettings(models.Model):
    crypto_address = models.CharField(max_length=255, blank=True)
    whatsapp_number = models.CharField(max_length=50, blank=True)
    manual_purchase_text = models.TextField(blank=True)
    dev_name_obfuscated = models.TextField(blank=True)
    owner_name_obfuscated = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

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
