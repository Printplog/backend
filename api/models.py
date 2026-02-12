from django.db import models
from django.core.files.base import ContentFile
import uuid
import logging
from .tools import Tool
from .fonts import Font

logger = logging.getLogger(__name__)

TEMPLATE_TYPE_CHOICES = [
    ('social_media', 'Social Media'),
    ('print', 'Print'),
    ('web', 'Web'),
    ('document', 'Document'),
]

class Template(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    # The 'svg' field is now used only for initial uploads or small metadata.
    # Large SVG data is stored exclusively in 'svg_file'.
    svg = models.TextField(blank=True) 
    svg_file = models.FileField(upload_to='templates/svgs/', blank=True, null=True, help_text="SVG file storage")

    banner = models.ImageField(upload_to='template_banners/', blank=True, null=True)
    form_fields = models.JSONField(default=dict, blank=True)
    type = models.CharField(max_length=20, choices=TEMPLATE_TYPE_CHOICES)
    tool = models.ForeignKey(Tool, on_delete=models.SET_NULL, null=True, blank=True, related_name='templates')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    hot = models.BooleanField(default=False)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='templates')

    def save(self, *args, **kwargs):
        # Optimization: If raw SVG text is provided (e.g. from an upload or direct edit),
        # convert it to a file and clear the text field to save DB space.
        if self.svg and self.svg.strip().startswith('<svg'):
            skip_parse = getattr(self, 'skip_svg_parse', False)
            
            # Extract form fields if requested
            if not skip_parse:
                try:
                    from .svg_parser import parse_svg_to_form_fields
                    self.form_fields = parse_svg_to_form_fields(self.svg)
                except Exception as e:
                    logger.error(f"Failed to parse SVG fields: {e}")

            # Persist to file and clear text blob
            filename = f"{self.id}.svg"
            self.svg_file.save(filename, ContentFile(self.svg.encode('utf-8')), save=False)
            self.svg = "" # CLEAR THE BLOB from DB

        super().save(*args, **kwargs)

    @property
    def svg_url(self):
        if self.svg_file:
            return self.svg_file.url
        return ""

    def __str__(self):
        return self.name
