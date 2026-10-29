from rest_framework import serializers
from ..models import Template, Font, Tutorial
from .base import FontSerializer
from api.watermark import WaterMark
from api.utils import get_signed_url
import os
import logging
from lxml import etree
import json
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

class TutorialSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source='template.name', read_only=True)
    template_tool = serializers.CharField(source='template.tool.id', read_only=True, allow_null=True)
    template_tool_name = serializers.CharField(source='template.tool.name', read_only=True, allow_null=True)
    tool_name = serializers.CharField(source='tool.name', read_only=True, allow_null=True)

    class Meta:
        model = Tutorial
        fields = [
            'id', 'template', 'template_name', 'template_tool', 'template_tool_name',
            'tool', 'tool_name', 'url', 'title', 'is_featured', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        template = attrs.get('template', getattr(self.instance, 'template', None))
        tool = attrs.get('tool', getattr(self.instance, 'tool', None))
        if template and tool:
            raise serializers.ValidationError(
                'A tutorial can be linked to a template or a tool, not both.'
            )
        return attrs


class TemplateSerializer(serializers.ModelSerializer):
    tutorial = serializers.SerializerMethodField()
    fonts = FontSerializer(many=True, read_only=True)
    font_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Font.objects.all(),
        source='fonts',
        write_only=True,
        required=False
    )
    svg_url = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    version = serializers.SerializerMethodField()
    
    # Temporary field for initial SVG ingestion or full overwrites
    svg = serializers.CharField(write_only=True, required=False)

    
    class Meta:
        model = Template
        fields = [
            'id', 'name', 'banner', 'svg_file', 'svg_patches', 'form_fields', 
            'type', 'tool', 'country', 'is_active', 'created_at', 'updated_at', 'hot', 
            'keywords', 'fonts', 'font_ids', 'tutorial', 'svg_url', 'tool_price', 
            'version', 'svg'
        ]

    def get_tutorial(self, obj):
        """Resolve which tutorial the template usage page should show.

        Rule: the template's own tutorial wins; if the template has none,
        fall back to its tool's tutorial; if neither exists, return None
        (the frontend hides the "Watch Tutorial" button when this is null).

        NOTE: reverse OneToOne access raises RelatedObjectDoesNotExist when
        no row exists — use getattr(obj, 'tutorial', None) instead of obj.tutorial.
        """
        tutorial = getattr(obj, 'tutorial', None)
        if not tutorial and obj.tool:
            tutorial = getattr(obj.tool, 'tutorial', None)
        if tutorial:
            return TutorialSerializer(tutorial).data
        return None

    def get_version(self, obj):
        return int(obj.updated_at.timestamp())
    
    def get_svg_url(self, obj):
        if obj.svg_file:
            url = get_signed_url(obj.svg_file)
            request = self.context.get('request')
            buster = f"v={int(obj.updated_at.timestamp())}"
            url = f"{url}&{buster}" if "?" in url else f"{url}?{buster}"
            if request and url and url.startswith('/'):
                return request.build_absolute_uri(url)
            return url
        return None

    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        # Extract tutorial data from request data
        request = self.context.get('request')
        tutorial_url = request.data.get('tutorial_url') if request else None
        tutorial_title = request.data.get('tutorial_title') if request else None
        fonts_data = validated_data.pop('fonts', None)
        svg_data = validated_data.pop('svg', None)
        
        # Create the template
        template = Template(**validated_data)
        if svg_data:
            template._raw_svg_data = svg_data
        template.save()
        
        if fonts_data:
            template.fonts.set(fonts_data)

        
        # Create tutorial if URL is provided
        if tutorial_url:
            Tutorial.objects.create(
                template=template,
                url=tutorial_url,
                title=tutorial_title or ''
            )
        
        return template
    
    def update(self, instance, validated_data):
        # Extract tutorial data from request data
        request = self.context.get('request')
        tutorial_url = request.data.get('tutorial_url') if request else None
        tutorial_title = request.data.get('tutorial_title') if request else None
        fonts_data = validated_data.pop('fonts', None)
        svg_data = validated_data.pop('svg', None)
        
        if svg_data:
            instance._raw_svg_data = svg_data
            
        # Update the template
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)

        
        # Update or create tutorial
        if tutorial_url is not None:  # Allow clearing tutorial by sending empty string
            tutorial, created = Tutorial.objects.get_or_create(
                template=instance,
                defaults={'url': tutorial_url, 'title': tutorial_title or ''}
            )
            if not created:
                tutorial.url = tutorial_url
                tutorial.title = tutorial_title or ''
                tutorial.save()
        
        return instance
        
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        if view and view.action == 'list':
            representation.pop('form_fields', None)
        
        # Manually sign banner URL if present
        if instance.banner:
            url = get_signed_url(instance.banner)
            request = self.context.get('request')
            buster = f"v={int(instance.updated_at.timestamp())}"
            url = f"{url}&{buster}" if "?" in url else f"{url}?{buster}"
            if request and url and url.startswith('/'):
                url = request.build_absolute_uri(url)
            representation['banner'] = url
            
        return representation


class AdminTemplateSerializer(serializers.ModelSerializer):
    """Admin-only serializer that never adds watermarks and handles SVG patching."""
    fonts = FontSerializer(many=True, read_only=True)
    font_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Font.objects.all(),
        source='fonts',
        write_only=True,
        required=False
    )
    svg_url = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    # Temporary field for initial SVG ingestion or full overwrites
    svg = serializers.CharField(write_only=True, required=False)

    # Flexible field to accept both JSON string (FormData) and list (JSON request)
    svg_patch = serializers.JSONField(write_only=True, required=False)

    version = serializers.SerializerMethodField()

    class Meta:
        model = Template
        fields = [
            'id', 'name', 'banner', 'svg_file', 'svg_patches', 'form_fields', 
            'type', 'tool', 'country', 'is_active', 'created_at', 'updated_at', 'hot', 
            'keywords', 'fonts', 'font_ids', 'svg_url', 'tool_price', 
            'version', 'svg', 'svg_patch'
        ]
        read_only_fields = ('id', 'created_at', 'updated_at', 'form_fields', 'tool_price')
    
    def get_version(self, obj):
        return int(obj.updated_at.timestamp())

    def get_svg_url(self, obj):
        if obj.svg_file:
            url = get_signed_url(obj.svg_file)
            request = self.context.get('request')
            buster = f"v={int(obj.updated_at.timestamp())}"
            url = f"{url}&{buster}" if "?" in url else f"{url}?{buster}"
            if request and url and url.startswith('/'):
                return request.build_absolute_uri(url)
            return url
        return None
    
    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None
    
    def create(self, validated_data):
        fonts_data = validated_data.pop('fonts', None)
        svg_data = validated_data.pop('svg', None)
        validated_data.pop('svg_patch', None) # Don't use patch on create
        
        template = Template(**validated_data)
        if svg_data:
            template._raw_svg_data = svg_data
        template.save()

        if fonts_data:
            template.fonts.set(fonts_data)
        return template
    
    def update(self, instance, validated_data):
        if 'form_fields' in validated_data:
            validated_data.pop('form_fields', None)

        fonts_data = validated_data.pop('fonts', None)
        svg_data = validated_data.pop('svg', None)

        if svg_data:
            instance._raw_svg_data = svg_data
            print(f"[Admin-Update] SVG replacement requested for {instance.name}.")
            instance._force_reparse = True
            # _preserve_patches is NOT set here — set conditionally below only when
            # actual patches are being saved alongside the new SVG.

        # --- Figma-style Patch Logic ---
        svg_patch_data = validated_data.pop('svg_patch', None)

        request = self.context.get('request')

        # Handle FormData: svg_patch might be a JSON string
        if svg_patch_data and isinstance(svg_patch_data, str):
            try:
                svg_patch_data = json.loads(svg_patch_data)
            except (json.JSONDecodeError, TypeError) as e:
                raise serializers.ValidationError(f"Invalid JSON format for svg_patch: {str(e)}")

        # Ensure svg_patch_data is a list
        if svg_patch_data and not isinstance(svg_patch_data, list):
            raise serializers.ValidationError("svg_patch must be a list of patch objects")

        # "Start Fresh": new SVG + explicitly empty patch list → clear all stored patches
        if svg_data and isinstance(svg_patch_data, list) and len(svg_patch_data) == 0:
            instance.svg_patches = []
            print(f"[Admin-Update] Patches cleared (new SVG + empty patch list = Start Fresh).")

        if svg_patch_data:
            from ..svg_utils import merge_svg_patches
            from ..svg_sync import sync_form_fields_with_patches
            from ..svg_parser import validate_svg_id

            # 1. Validate Patch IDs against DSL
            for patch in svg_patch_data:
                if patch.get('attribute') == 'id':
                    new_id = patch.get('value')
                    is_valid, error = validate_svg_id(new_id)
                    if not is_valid:
                        print(f"[Admin-Update] REJECTED invalid ID: {new_id} - {error}")
                        raise serializers.ValidationError(f"Invalid SVG ID '{new_id}': {error}")

            # 2. Merge new patches with existing ones in the database
            existing_patches = instance.svg_patches or []
            print(f"[Admin-Update] New Patches: {len(svg_patch_data)}, Existing: {len(existing_patches)}")
            combined_patches = existing_patches + svg_patch_data
            instance.svg_patches = merge_svg_patches(combined_patches)

            # Preserve combined patches when a new SVG is also being uploaded in the same save
            if svg_data:
                instance._preserve_patches = True

            # 3. SYNC: Update form_fields JSON directly (Handles innerText and ID changes)
            updated_fields, modified = sync_form_fields_with_patches(instance, svg_patch_data)
            # Always assign — if not modified, updated_fields == existing fields (no-op); if modified, ensures sync
            instance.form_fields = updated_fields
            if modified:
                logger.info("[SVG-Sync] Synced %d form fields for template %s", len(updated_fields), instance.id)



        # Continue with metadata updates
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)
        
        return instance
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        if view and view.action == 'list':
            representation.pop('form_fields', None)
        
        # Manually sign banner URL if present
        if instance.banner:
            url = get_signed_url(instance.banner)
            request = self.context.get('request')
            buster = f"v={int(instance.updated_at.timestamp())}"
            url = f"{url}&{buster}" if "?" in url else f"{url}?{buster}"
            if request and url and url.startswith('/'):
                url = request.build_absolute_uri(url)
            representation['banner'] = url
            
        return representation
