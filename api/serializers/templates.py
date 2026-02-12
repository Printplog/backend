from rest_framework import serializers
from ..models import Template, Font, Tutorial
from .base import FontSerializer
from api.watermark import WaterMark
from api.utils import get_signed_url
import os
from lxml import etree
import json
from django.core.files.base import ContentFile

class TutorialSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source='template.name', read_only=True)
    template_tool = serializers.CharField(source='template.tool.id', read_only=True, allow_null=True)
    template_tool_name = serializers.CharField(source='template.tool.name', read_only=True, allow_null=True)
    
    class Meta:
        model = Tutorial
        fields = ['id', 'template', 'template_name', 'template_tool', 'template_tool_name', 'url', 'title', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class TemplateSerializer(serializers.ModelSerializer):
    tutorial = TutorialSerializer(read_only=True)
    fonts = FontSerializer(many=True, read_only=True)
    font_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Font.objects.all(),
        source='fonts',
        write_only=True,
        required=False
    )
    svg_url = serializers.SerializerMethodField()
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Template
        fields = '__all__'
    
    def get_svg_url(self, obj):
        if obj.svg_file:
            return get_signed_url(obj.svg_file)
        return None

    def get_banner(self, obj):
        if obj.banner:
            return get_signed_url(obj.banner)
        return None

    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        # Extract tutorial data from request data
        request = self.context.get('request')
        tutorial_url = request.data.get('tutorial_url') if request else None
        tutorial_title = request.data.get('tutorial_title') if request else None
        fonts_data = validated_data.pop('fonts', None)
        
        # Create the template
        template = Template.objects.create(**validated_data)
        
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
        # Get the base representation
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        if view and view.action == 'list':
            # For list view: remove SVG and form_fields, keep banner
            representation.pop('svg', None)
            representation.pop('form_fields', None)
        elif view and view.action == 'retrieve':
            # For detail view: provide SVG URL, keep form_fields
            # User wants to fetch SVG from URL, so remove content
            if instance.svg_file:
                representation.pop('svg', None)
        else:
            # For other actions (create, update): include SVG if present
            if 'svg' in representation and representation['svg']:
                representation['svg'] = WaterMark().add_watermark(representation['svg'])
        
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
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    # Use ListField for structured data. For FormData, this will need parsing in `update`.
    svg_patch = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Template
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'form_fields')

    def get_svg_url(self, obj):
        if obj.svg_file:
            return get_signed_url(obj.svg_file)
        return None

    def get_banner(self, obj):
        if obj.banner:
            return get_signed_url(obj.banner)
        return None
    
    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        fonts_data = validated_data.pop('fonts', None)
        validated_data.pop('svg_patch', None) # Don't use patch on create
        template = Template.objects.create(**validated_data)
        if fonts_data:
            template.fonts.set(fonts_data)
        return template
    
    def update(self, instance, validated_data):
        if 'form_fields' in validated_data:
            validated_data.pop('form_fields', None)
        
        fonts_data = validated_data.pop('fonts', None)
        
        # --- SVG Patch Logic ---
        svg_patch_data = validated_data.pop('svg_patch', None)
        
        # Handle case where patch comes as a JSON string from FormData
        request = self.context.get('request')
        if not svg_patch_data and request and 'svg_patch' in request.data:
            try:
                svg_patch_data = json.loads(request.data.get('svg_patch'))
            except (json.JSONDecodeError, TypeError):
                raise serializers.ValidationError("Invalid JSON format for svg_patch.")

        if svg_patch_data:
            from ..svg_utils import apply_svg_patches, merge_svg_patches
            print(f"[AdminTemplateSerializer] Processing {len(svg_patch_data)} patches using utility")
            try:
                # 1. Read existing SVG content
                if not instance.svg_file:
                    raise serializers.ValidationError("Cannot apply patch: No existing SVG file found.")
                
                if instance.svg:
                    svg_content = instance.svg
                else:
                    instance.svg_file.open('r')
                    svg_content = instance.svg_file.read()
                    instance.svg_file.close()

                if not svg_content:
                    raise serializers.ValidationError("Cannot apply patch: Existing SVG content is empty.")

                # 2. Apply patches using utility
                svg_patch_data = merge_svg_patches(svg_patch_data)
                new_svg_content = apply_svg_patches(svg_content, svg_patch_data)
                
                if new_svg_content != svg_content:
                    validated_data['svg'] = new_svg_content
                    instance.skip_svg_parse = True
                    print(f"[AdminTemplateSerializer] SVG patched successfully. Lines changed.")
                else:
                    print("[AdminTemplateSerializer] No changes detected after patching.")

            except Exception as e:
                raise serializers.ValidationError(f"Failed to apply SVG patch: {str(e)}")


        # Continue with the normal update process for all other fields
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)
        
        return instance
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        # In Admin context, we want to be more generous with data to avoid caching issues
        if view and view.action == 'list':
            representation.pop('svg', None)
            representation.pop('form_fields', None)
        
        # Note: We keep 'svg' in the representation for 'retrieve', 'update', and 'partial_update'
        # so the frontend doesn't have to wait for CDN/S3 propagation or deal with stale caches.
        
        return representation
