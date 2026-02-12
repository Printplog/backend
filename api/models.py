from django.db import models
from django.core.files.base import ContentFile
import threading
import uuid
import logging
from .tools import Tool
from .fonts import Font, DEFAULT_FONTS

logger = logging.getLogger(__name__)

TEMPLATE_TYPE_CHOICES = [
    ('social_media', 'Social Media'),
    ('print', 'Print'),
    ('web', 'Web'),
    ('document', 'Document'),
]

# Helper function for background upload
def background_upload(instance, filename, content):
    try:
        instance.svg_file.save(filename, content, save=False)
        # We need to save the instance again to update the file path/url in DB,
        # but we must avoid triggering the whole save logic recursively.
        # However, Django's file.save() already updates the instance's file attribute.
        # We just need to persist that change to the DB without re-triggering our custom save method.
        Template.objects.filter(pk=instance.pk).update(svg_file=instance.svg_file.name)
        logger.info(f"Background upload complete for {instance.id}")
    except Exception as e:
        logger.error(f"Background upload failed for {instance.id}: {e}")

class Template(models.Model):
    # ... (existing fields)
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
    updated_at = models.DateTimeField(auto_now=True)
    hot = models.BooleanField(default=False)
    keywords = models.JSONField(default=list, blank=True)
    fonts = models.ManyToManyField('Font', blank=True, related_name='templates', help_text="Font files used in this template")
    

    def save(self, *args, **kwargs):
        # Allow bypassing SVG parsing for admin edits (performance optimization)
        skip_parse = getattr(self, 'skip_svg_parse', False)
        
        # Optimization: Only parse SVG if it has changed
        svg_changed = False
        if self.svg:
            if self.pk:
                try:
                    old_instance = Template.objects.only('svg').get(pk=self.pk)
                    if old_instance.svg != self.svg:
                        svg_changed = True
                except Template.DoesNotExist:
                    svg_changed = True
            else:
                svg_changed = True

        if svg_changed and self.svg:
            skip_parse = getattr(self, 'skip_svg_parse', False)
            print(f"[Template.save] SVG changed. skip_parse={skip_parse}")
            
            # Parse SVG only if not skipped
            if not skip_parse:
                from .svg_parser import parse_svg_to_form_fields # Import strictly inside to avoid circular import
                print("[Template.save] Parsing SVG to form fields...")
                self.form_fields = parse_svg_to_form_fields(self.svg)
            
            # FILE UPLOAD STRATEGY
            filename = f"{self.id}.svg"
            content = ContentFile(self.svg.encode('utf-8'))
            
            # If this is an admin edit (indicated by skip_parse=True), uses background upload
            # to make the UI response instant.
            if skip_parse:
                 print("[Template.save] Starting BACKGROUND upload for SVG file...")
                 # We DON'T save the file here synchronously.
                 # Instead, we start a thread.
                 thread = threading.Thread(
                     target=background_upload,
                     args=(self, filename, content)
                 )
                 thread.start()
            else:
                # Normal save (synchronous) for safety during initial creation or other flows
                print("[Template.save] Uploading SVG file synchronously...")
                self.svg_file.save(filename, content, save=False)

        super().save(*args, **kwargs)

    @property
    def svg_url(self):
        if self.svg_file:
            return self.svg_file.url
        return ""

    def __str__(self):
        return self.name

