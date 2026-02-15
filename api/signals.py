from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from .models import Template
from .cache_utils import invalidate_template_cache
from .compression import compress_image, compress_svg_images
from django.core.files.base import ContentFile
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Template)
def invalidate_cache_on_save(sender, instance, **kwargs):
    """
    Invalidate all template-related caches when a template is saved.
    This ensures that:
    1. The template list is updated (e.g. if a new template is added or hot status changed)
    2. The template detail is updated
    3. The SVG content is updated
    """
    logger.info(f"Signal: Template {instance.id} saved. Invalidating all template caches.")
    invalidate_template_cache()

@receiver(post_delete, sender=Template)
def invalidate_cache_on_delete(sender, instance, **kwargs):
    """
    Invalidate all template-related caches when a template is deleted.
    """
    logger.info(f"Signal: Template {instance.id} deleted. Invalidating all template caches.")
    invalidate_template_cache()

@receiver(pre_save, sender=Template)
def compress_template_images(sender, instance, **kwargs):
    """
    Compress banners before saving to storage.
    """
    if instance.banner:
        # Check if this is a new file or if the file has changed
        try:
            old_instance = Template.objects.get(pk=instance.pk)
            if old_instance.banner != instance.banner:
                compress_image(instance.banner)
        except Template.DoesNotExist:
            # New instance
            compress_image(instance.banner)

@receiver(pre_save, sender=Template)
def compress_svg_embedded_images(sender, instance, **kwargs):
    """
    Compress embedded images inside SVG files before saving.
    This reduces file size dramatically (15MB -> 2MB) for Safari performance.
    """
    if instance.svg_file:
        try:
            # Check if this is a new file or if the file has changed
            old_instance = Template.objects.get(pk=instance.pk)
            if old_instance.svg_file != instance.svg_file:
                # Read SVG content
                instance.svg_file.seek(0)
                svg_content = instance.svg_file.read().decode('utf-8')
                
                # Compress embedded images
                optimized_svg = compress_svg_images(svg_content, quality=60)
                
                # Save back to the file field
                instance.svg_file.seek(0)
                instance.svg_file = ContentFile(optimized_svg.encode('utf-8'), name=instance.svg_file.name)
                
                logger.info(f"Compressed SVG embedded images for Template {instance.id}")
        except Template.DoesNotExist:
            # New instance - compress it
            try:
                instance.svg_file.seek(0)
                svg_content = instance.svg_file.read().decode('utf-8')
                
                optimized_svg = compress_svg_images(svg_content, quality=60)
                
                instance.svg_file.seek(0)
                instance.svg_file = ContentFile(optimized_svg.encode('utf-8'), name=instance.svg_file.name)
                
                logger.info(f"Compressed SVG embedded images for new Template {instance.id}")
            except Exception as e:
                logger.error(f"Error compressing SVG for new template: {e}")
        except Exception as e:
            logger.error(f"Error compressing SVG embedded images: {e}")
