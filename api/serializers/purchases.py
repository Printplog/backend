from rest_framework import serializers
from ..models import PurchasedTemplate, Template
from .base import FieldUpdateSerializer, FontSerializer
from api.watermark import WaterMark
from api.utils import get_signed_url
from decimal import Decimal
import json

class PurchasedTemplateSerializer(serializers.ModelSerializer):
    field_updates = FieldUpdateSerializer(many=True, write_only=True, required=False)
    fonts = FontSerializer(many=True, read_only=True)
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    svg_url = serializers.SerializerMethodField()
    
    # FIGMA-STYLE OVERWRITES
    svg = serializers.CharField(write_only=True, required=False)
    svg_patch = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = PurchasedTemplate
        fields = '__all__'
        read_only_fields = ('buyer', 'svg_patches')

    def charge_if_test_false(self, instance, validated_data, is_update=False):
        old_test = instance.test if is_update else True
        new_test = validated_data.get("test", old_test)

        if old_test is True and new_test is False:
            user = self.context['request'].user
            charge_amount = instance.template.tool.price if instance.template and instance.template.tool else Decimal('5.00')

            if user.wallet.spendable_balance < charge_amount:
                raise serializers.ValidationError(f"Insufficient funds. Required: {charge_amount}")

            tx = user.wallet.debit(charge_amount, description=f"Watermark removal: {instance.name}")

            # Send Purchase Receipt Email (keeping user engaged)
            try:
                from api.utils.email_service import EmailService
                EmailService.send_purchase_receipt(
                    user, 
                    instance.name or instance.template.name, 
                    charge_amount, 
                    user.wallet.balance, 
                    tx.tx_id
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to send purchase receipt email: {e}")

    def create(self, validated_data):
        field_updates = validated_data.pop("field_updates", None)
        svg_data = validated_data.pop("svg", None)
        validated_data.pop("svg_patch", None)
        
        instance = PurchasedTemplate(**validated_data)
        instance.buyer = self.context['request'].user
        
        # 0. Set default name if missing
        if not instance.name and instance.template:
            instance.name = f"My {instance.template.name}"
        elif not instance.name:
            instance.name = "Untitled Document"
        
        # FIGMA-STYLE INHERITANCE: Force inheritance before we process field_updates
        if instance.template:
            if not instance.svg_patches:
                instance.svg_patches = list(instance.template.svg_patches)
            if not instance.form_fields:
                instance.form_fields = list(instance.template.form_fields)
                
        # If user provided a raw SVG (e.g. tool output), save to storage
        if svg_data:
            instance._raw_svg_data = svg_data
            
        # If user provided initial field values, store in form_fields
        if field_updates:
            # Merging field updates into form_fields logic
            current_fields = instance.form_fields or []
            field_map = {f['id']: f for f in current_fields}
            for update in field_updates:
                if update['id'] in field_map:
                    field_map[update['id']]['currentValue'] = update['value']
                    if update.get('barcodeImage') is not None:
                        field_map[update['id']]['barcodeImage'] = update['barcodeImage']
                else:
                    # If it doesn't match a template field, still add it as a custom field
                    field_map[update['id']] = {
                        'id': update['id'],
                        'currentValue': update['value'],
                        'name': update['id'].replace('_', ' ').capitalize()
                    }
                    if update.get('barcodeImage') is not None:
                        field_map[update['id']]['barcodeImage'] = update['barcodeImage']
            instance.form_fields = list(field_map.values())

        self.charge_if_test_false(instance, validated_data, is_update=False)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        self.charge_if_test_false(instance, validated_data, is_update=True)
        
        # 1. FIGMA-STYLE: Store field values in the JSON field instead of the SVG text
        field_updates = validated_data.pop("field_updates", None)
        if field_updates:
            current_fields = instance.form_fields or []
            field_map = {f['id']: f for f in current_fields}
            for update in field_updates:
                if update['id'] in field_map:
                    field_map[update['id']]['currentValue'] = update['value']
                    if update.get('barcodeImage') is not None:
                        field_map[update['id']]['barcodeImage'] = update['barcodeImage']
                else:
                    # New field (shouldn't happen often but for safety)
                    field_map[update['id']] = {'id': update['id'], 'currentValue': update['value']}
                    if update.get('barcodeImage') is not None:
                        field_map[update['id']]['barcodeImage'] = update['barcodeImage']
            instance.form_fields = list(field_map.values())
            instance.save(update_fields=['form_fields'])

        # 2. Support Figma-style patches for layout edits
        svg_patch_data = validated_data.pop('svg_patch', None)
        request = self.context.get('request')
        if not svg_patch_data and request and 'svg_patch' in request.data:
            try: svg_patch_data = json.loads(request.data.get('svg_patch'))
            except (json.JSONDecodeError, TypeError): pass

        if svg_patch_data:
            from ..svg_utils import merge_svg_patches
            from ..svg_sync import sync_form_fields_with_patches
            existing = instance.svg_patches or []
            instance.svg_patches = merge_svg_patches(existing + svg_patch_data)
            
            # ADVANCED SYNC: Use helper to update form_fields JSON
            updated_fields, modified = sync_form_fields_with_patches(instance, svg_patch_data)
            
            if modified:
                instance.form_fields = updated_fields
                instance.save(update_fields=['form_fields'])
            
            instance.save(update_fields=['svg_patches'])

        # 3. Raw SVG overwrite
        svg_data = validated_data.pop('svg', None)
        if svg_data:
            instance._raw_svg_data = svg_data

        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        request = self.context.get('request')
        
        if view and view.action == 'list':
            representation.pop('form_fields', None)
        else:
            # EMERGENCY SYNC: If data is missing but template has it, inherit now
            if not representation.get('form_fields') and instance.template:
                representation['form_fields'] = instance.template.form_fields
                if not instance.form_fields and representation['form_fields']:
                    instance.form_fields = representation['form_fields']
                    instance.save(update_fields=['form_fields'])
        
        # Keywords should be available in list view for split detection on dashboard
        if not representation.get('keywords') and instance.template:
            representation['keywords'] = instance.template.keywords
            if not instance.keywords and representation['keywords']:
                instance.keywords = representation['keywords']
                instance.save(update_fields=['keywords'])

        # Absolute URL for SVG
        if representation.get('svg_url') and representation['svg_url'].startswith('/') and request:
            representation['svg_url'] = request.build_absolute_uri(representation['svg_url'])
            
        # Absolute URL for Banner
        if representation.get('banner') and representation['banner'].startswith('/') and request:
            representation['banner'] = request.build_absolute_uri(representation['banner'])
            
        return representation
    
    def get_tool_price(self, obj):
        return obj.template.tool.price if obj.template and obj.template.tool else None

    def get_banner(self, obj):
        if obj.template and obj.template.banner:
            return get_signed_url(obj.template.banner)
        return None

    def get_svg_url(self, obj):
        if obj.svg_file:
            return get_signed_url(obj.svg_file)
        # Fallback to base template SVG if purchase has no bespoke file
        if obj.template and obj.template.svg_file:
            return get_signed_url(obj.template.svg_file)
        return None

class PublicTrackingSerializer(serializers.ModelSerializer):
    """
    Restricted serializer for public tracking sites.
    Only returns fields marked with tracking roles and basic status info.
    """
    form_fields = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = PurchasedTemplate
        fields = ['id', 'tracking_id', 'name', 'form_fields', 'test', 'updated_at']

    def get_form_fields(self, obj):
        # Only return fields that have a trackingRole assigned in the SVG
        return [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "trackingRole": f.get("trackingRole"),
                "currentValue": f.get("currentValue") or f.get("defaultValue")
            }
            for f in (obj.form_fields or [])
            if f.get("trackingRole")
        ]
