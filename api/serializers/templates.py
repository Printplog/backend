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
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        if view and view.action == 'list':
            representation.pop('form_fields', None)
        
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
    
    # Temporary field for initial SVG ingestion or full overwrites
    svg = serializers.CharField(write_only=True, required=False)
    
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
        
        # --- Figma-style Patch Logic ---
        svg_patch_data = validated_data.pop('svg_patch', None)
        
        request = self.context.get('request')
        if not svg_patch_data and request and 'svg_patch' in request.data:
            try:
                svg_patch_data = json.loads(request.data.get('svg_patch'))
            except (json.JSONDecodeError, TypeError):
                raise serializers.ValidationError("Invalid JSON format for svg_patch.")

        if svg_patch_data:
            from ..svg_utils import merge_svg_patches
            # 1. Merge new patches with existing ones in the database
            existing_patches = instance.svg_patches or []
            combined_patches = existing_patches + svg_patch_data
            instance.svg_patches = merge_svg_patches(combined_patches)
            
            # 2. FAST SYNC: Update form_fields JSON directly for innerText changes
            if instance.form_fields:
                updated_fields = json.loads(json.dumps(instance.form_fields)) # Deep copy
                modified = False
                
                print(f"[SVG-Sync] Started for template: {instance.name}")
                print(f"[SVG-Sync] Processing {len(svg_patch_data)} patches...")
                
                for patch in svg_patch_data:
                    p_id = patch.get('id')
                    p_attr = patch.get('attribute')
                    p_val = patch.get('value')

                    if p_id and p_attr == 'innerText':
                        p_id_lower = p_id.lower()
                        for field in updated_fields:
                            field_id = field.get('id', '')
                            svg_el_id = field.get('svgElementId', '')
                            
                            # A. Match regular fields (Full match or Base ID match)
                            # Check both case-sensitive and case-insensitive to be safe
                            if p_id == svg_el_id or p_id == field_id or p_id_lower == svg_el_id.lower() or p_id_lower == field_id.lower():
                                print(f"  [Match] Field '{field_id}' (SVG ID: {svg_el_id}) matched patch '{p_id}' -> '{p_val}'")
                                field['defaultValue'] = p_val
                                field['currentValue'] = p_val
                                modified = True
                            
                            # B. Match Select Options (Search inside the options array)
                            if field.get('type') == 'select' and 'options' in field:
                                for opt in field.get('options', []):
                                    opt_id = opt.get('value', '')
                                    opt_svg_id = opt.get('svgElementId', '')
                                    
                                    if p_id == opt_svg_id or p_id == opt_id or p_id_lower == opt_svg_id.lower() or p_id_lower == opt_id.lower():
                                        print(f"  [Match] Select Option '{opt.get('label')}' matched patch '{p_id}' -> '{p_val}'")
                                        opt['displayText'] = p_val
                                        opt['label'] = p_val
                                        modified = True

                if modified:
                    instance.form_fields = updated_fields
                    # Save explicitly here to ensure sync is locked in before return
                    instance.save(update_fields=['form_fields'])
                    print(f"[SVG-Sync] SUCCESS: form_fields updated and saved.")
                else:
                    print(f"[SVG-Sync] NOTICE: No matching form fields found for these patches.")

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
        
        return representation
