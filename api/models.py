# models.py
import uuid
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.contrib.postgres.fields import JSONField  # Use `models.JSONField` if Django 3.1+
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from .svg_parser import parse_svg_to_form_fields
from django.core.files.base import ContentFile

# SVG minification removed - SVGs from Photoshop are already optimized and minification can break designs

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
        indexes = [
            models.Index(fields=['is_active']),
        ]
    
    def __str__(self):
        return self.name


class Template(models.Model):
    TEMPLATE_TYPE_CHOICES = [
        ('tool', 'Tool'),
        ('design', 'Design'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    svg = models.TextField()
    svg_file = models.FileField(upload_to='templates/svgs/', blank=True, null=True, help_text="SVG file storage")

    banner = models.ImageField(upload_to='template_banners/', blank=True, null=True, help_text="Banner image for the template")
    form_fields = models.JSONField(default=dict, blank=True)
    type = models.CharField(max_length=20, choices=TEMPLATE_TYPE_CHOICES)
    tool = models.ForeignKey(Tool, on_delete=models.SET_NULL, null=True, blank=True, related_name='templates')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    hot = models.BooleanField(default=False)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='templates', help_text="Font files used in this template")
    

    def save(self, *args, **kwargs):
        # Allow bypassing SVG parsing for admin edits (performance optimization)
        skip_parse = getattr(self, 'skip_svg_parse', False)
        
        # Optimization: Only parse SVG if it has changed AND we're not skipping
        should_parse = False
        if self.svg and not skip_parse:
            if self.pk:
                try:
                    old_instance = Template.objects.only('svg').get(pk=self.pk)
                    if old_instance.svg != self.svg:
                        should_parse = True
                except Template.DoesNotExist:
                    should_parse = True
            else:
                should_parse = True

        if should_parse and self.svg:
            # Parse SVG to generate form fields
            self.form_fields = parse_svg_to_form_fields(self.svg)
            
            # Save SVG to file
            filename = f"{self.id}.svg"
            self.svg_file.save(filename, ContentFile(self.svg.encode('utf-8')), save=False)

        # Ensure keywords is always a list
        if not isinstance(self.keywords, list):
            if self.keywords in (None, '', []):
                self.keywords = []
            else:
                self.keywords = [str(self.keywords)]
        
        return super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['type']),
            models.Index(fields=['hot']),
            models.Index(fields=['created_at']),
            models.Index(fields=['tool']),
            models.Index(fields=['is_active']),
        ]

    def get_purchased_count(self):
        """Get the number of purchased templates for this template"""
        return self.purchases.count()
    
    def has_purchases(self):
        """Check if this template has any purchased templates"""
        return self.purchases.exists()

    def __str__(self):
        return self.name


class PurchasedTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="purchased_templates")
    template = models.ForeignKey("Template", on_delete=models.SET_NULL, null=True, blank=True, related_name="purchases")
    
    name = models.CharField(max_length=255, blank=True)

    svg = models.TextField()
    svg_file = models.FileField(upload_to='purchased_templates/svgs/', blank=True, null=True, help_text="SVG file storage")
    form_fields = models.JSONField(default=dict, blank=True)
    test = models.BooleanField(default=True)

    tracking_id = models.CharField(max_length=100, blank=True, null=True, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='purchased_templates', help_text="Fonts copied from template at purchase time")

    def save(self, *args, **kwargs):
        # Auto-generate name if not provided
        if not self.name:
            if self.template:
                count = PurchasedTemplate.objects.filter(buyer=self.buyer, template=self.template).count() + 1
                self.name = f"{self.template.name} #{count}"
            else:
                # Handle orphaned purchased templates
                count = PurchasedTemplate.objects.filter(buyer=self.buyer, template__isnull=True).count() + 1
                self.name = f"Orphaned Template #{count}"

        # Optimization: Only parse SVG if it has changed
        should_parse = False
        if self.svg:
            if self.pk:
                try:
                    old_instance = PurchasedTemplate.objects.only('svg').get(pk=self.pk)
                    if old_instance.svg != self.svg:
                        should_parse = True
                except PurchasedTemplate.DoesNotExist:
                    should_parse = True
            else:
                should_parse = True

        if should_parse and self.svg:
            # Always parse SVG to generate form fields from the latest SVG content
            self.form_fields = parse_svg_to_form_fields(self.svg)
            
            # Save SVG to file
            filename = f"{self.id}.svg"
            self.svg_file.save(filename, ContentFile(self.svg.encode('utf-8')), save=False)

        # Inherit keywords from template if not provided
        if not self.keywords and self.template and self.template.keywords:
            self.keywords = list(self.template.keywords)
        elif not isinstance(self.keywords, list):
            if self.keywords in (None, '', []):
                self.keywords = []
            else:
                self.keywords = [str(self.keywords)]

        super().save(*args, **kwargs)

        # Copy fonts from parent template if this is a new purchase and has no fonts yet
        if self.template and self.template.fonts.exists() and not self.fonts.exists():
             self.fonts.set(self.template.fonts.all())

    class Meta:
        indexes = [
            models.Index(fields=['buyer']),
            models.Index(fields=['template']),
            models.Index(fields=['tracking_id']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        template_name = self.template.name if self.template else "Orphaned Template"
        return f"{self.buyer.username} - {template_name} ({'test' if self.test else 'paid'})"


class Tutorial(models.Model):
    template = models.OneToOneField(Template, on_delete=models.CASCADE, related_name='tutorial')
    url = models.URLField(help_text="Tutorial video URL")
    title = models.CharField(max_length=255, blank=True, help_text="Optional tutorial title")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.template.name} - Tutorial"


class Font(models.Model):
    """Font files for SVG templates"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Font family name (e.g., 'OCR B', 'Arial')")
    family = models.CharField(max_length=255, blank=True, help_text="CSS Font Family (e.g., 'Roboto'). Groups variants.")
    weight = models.CharField(max_length=50, default="normal", help_text="CSS Font Weight (e.g., 'normal', 'bold', '400', '700')")
    style = models.CharField(max_length=50, default="normal", help_text="CSS Font Style (e.g., 'normal', 'italic')")
    font_file = models.FileField(upload_to='fonts/', help_text="Font file (TTF, OTF, WOFF, WOFF2)")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
        ]
    
    def __str__(self):
        return self.name
    
    def get_font_format(self):
        """Detect font format from file extension"""
        ext = self.font_file.name.split('.')[-1].lower()
        format_map = {
            'ttf': 'truetype',
            'otf': 'opentype',
            'woff': 'woff',
            'woff2': 'woff2',
        }
        return format_map.get(ext, 'truetype')

class SiteSettings(models.Model):
    """
    Singleton model for site-wide settings like payment details and security questions.
    """
    crypto_address = models.CharField(max_length=255, blank=True, help_text="Crypto address for payments")
    whatsapp_number = models.CharField(max_length=50, blank=True, help_text="WhatsApp number for manual payments")
    manual_purchase_text = models.TextField(blank=True, help_text="Instructions for manual purchases")
    
    # Obfuscated security answers
    dev_name_obfuscated = models.TextField(blank=True, help_text="Answer to: Second name of developer")
    owner_name_obfuscated = models.TextField(blank=True, help_text="Answer to: Second name of owner")
    
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Ensure only one instance exists
        if not self.pk and SiteSettings.objects.exists():
            return  # Prevent creation of new records via save()
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls):
        """Helper to get OR create the singleton instance"""
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return "Site Settings"

class TransformVariable(models.Model):
    """
    Model for storing reusable SVG transform values (Rotate, Scale, Position).
    Can be categorized by transform type.
    """
    CATEGORY_CHOICES = [
        ('rotate', 'Rotation'),
        ('scale', 'Scale'),
        ('translateX', 'Position X'),
        ('translateY', 'Position Y'),
    ]
    
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='rotate')
    value = models.FloatField(default=0.0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"

    class Meta:
        verbose_name = "Transform Variable"
        verbose_name_plural = "Transform Variables"
        ordering = ['category', 'name']
        unique_together = ['name', 'category']
