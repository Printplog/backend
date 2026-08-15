from rest_framework import serializers
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from dj_rest_auth.serializers import UserDetailsSerializer
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str

User = get_user_model()


ROLE_CODES = {
    "admin": "ZK7T-93XY",
    "staff": "S9K3-41TV",
    "user": "LQ5D-21VM",
}

class CustomUserDetailsSerializer(UserDetailsSerializer):
    role = serializers.SerializerMethodField()
    wallet_balance = serializers.SerializerMethodField()
    total_purchases = serializers.SerializerMethodField()
    downloads = serializers.SerializerMethodField()
    date_joined = serializers.DateTimeField(read_only=True)

    class Meta(UserDetailsSerializer.Meta):
        fields = UserDetailsSerializer.Meta.fields + (
            "email",
            "role",
            "wallet_balance",
            "total_purchases",
            "downloads",
            "date_joined",
            "is_active",
            "source",
            "medium",
            "campaign",
            "term",
            "content",
            "source_platform",
            "gclid",
            "fbclid",
        )


    def get_role(self, user):
        if user.is_superuser:
            return ROLE_CODES["admin"]
        if user.is_staff:
            return ROLE_CODES["staff"]
        return ROLE_CODES["user"]

    def get_wallet_balance(self, user):
        if hasattr(user, 'wallet'):
            return user.wallet.balance
        return 0

    def get_total_purchases(self, user):
        return user.purchased_templates.count()

    def get_downloads(self, user):
        return user.downloads


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        min_length=6,
        style={"input_type": "password"}
    )
    referred_by = serializers.CharField(required=False, allow_blank=True, write_only=True)
    source = serializers.CharField(required=False, allow_blank=True, write_only=True)
    medium = serializers.CharField(required=False, allow_blank=True, write_only=True)
    campaign = serializers.CharField(required=False, allow_blank=True, write_only=True)
    term = serializers.CharField(required=False, allow_blank=True, write_only=True)
    content = serializers.CharField(required=False, allow_blank=True, write_only=True)
    source_platform = serializers.CharField(required=False, allow_blank=True, write_only=True)
    gclid = serializers.CharField(required=False, allow_blank=True, write_only=True)
    fbclid = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'referred_by', 'source', 'medium', 'campaign', 'term', 'content', 'source_platform', 'gclid', 'fbclid']

    def validate_referred_by(self, value):
        if not value:
            return value
        from api.models import SiteSettings
        settings = SiteSettings.get_settings()
        if settings.enable_referrals and not User.objects.filter(username=value).exists():
            raise serializers.ValidationError("Invalid referral code — no user found with that username.")
        return value

    def create(self, validated_data):
        referrer_username = validated_data.pop('referred_by', None)
        source = validated_data.pop('source', None)
        medium = validated_data.pop('medium', None)
        campaign = validated_data.pop('campaign', None)
        term = validated_data.pop('term', None)
        content = validated_data.pop('content', None)
        source_platform = validated_data.pop('source_platform', None)
        gclid = validated_data.pop('gclid', None)
        fbclid = validated_data.pop('fbclid', None)

        request = self.context.get('request')
        attribution = {}

        if request:
            from analytics.utils import get_attribution_for_request

            attribution = get_attribution_for_request(request)

        if not source:
            source = attribution.get('source')
        if not medium:
            medium = attribution.get('medium')
        if not campaign:
            campaign = attribution.get('campaign')
        if not term:
            term = attribution.get('term')
        if not content:
            content = attribution.get('content')
        if not source_platform:
            source_platform = attribution.get('source_platform')
        if not gclid:
            gclid = attribution.get('gclid')
        if not fbclid:
            fbclid = attribution.get('fbclid')

        # If still no source but we have a referrer, hardcode to 'referral'
        if not source and referrer_username:
            source = 'referral'
        if not medium and source == 'referral':
            medium = 'referral'

        user = User.objects.create_user(**validated_data)

        update_fields = []
        if source:
            user.source = source
            update_fields.append('source')
        elif not user.source:
            user.source = 'Direct'
            update_fields.append('source')

        if medium:
            user.medium = medium
            update_fields.append('medium')
        elif not user.medium:
            user.medium = '(none)'
            update_fields.append('medium')

        if campaign:
            user.campaign = campaign
            update_fields.append('campaign')

        if term:
            user.term = term
            update_fields.append('term')

        if content:
            user.content = content
            update_fields.append('content')

        if source_platform:
            user.source_platform = source_platform
            update_fields.append('source_platform')

        if gclid:
            user.gclid = gclid
            update_fields.append('gclid')

        if fbclid:
            user.fbclid = fbclid
            update_fields.append('fbclid')

        if update_fields:
            user.save(update_fields=update_fields)


        if referrer_username:
            from api.models import SiteSettings
            settings = SiteSettings.get_settings()
            
            if settings.enable_referrals:
                try:
                    referrer = User.objects.get(username=referrer_username)

                    # Anti-fraud: Don't link if same user or same IP
                    user_ip = None
                    if request:
                        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
                        if x_forwarded_for:
                            user_ip = x_forwarded_for.split(',')[0]
                        else:
                            user_ip = request.META.get('REMOTE_ADDR')

                    # We don't have the referrer's last IP here easily, but we can check if they are the same user
                    # More robust IP checks will happen during the reward trigger in the webhook.
                    if referrer != user:
                        user.referred_by = referrer
                        user.save()
                except User.DoesNotExist:
                    pass
        return user


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"}
    )

    def validate(self, data): # type: ignore
        username = data.get("username")
        password = data.get("password")

        if username and password:
            # If username looks like an email, try to find the actual username
            if "@" in username:
                user_obj = User.objects.filter(email=username).first()
                if user_obj:
                    username = user_obj.username

            user = authenticate(request=self.context.get("request"), username=username, password=password)
            if not user:
                raise serializers.ValidationError({"error": ("Invalid credentials")})
        else:
            raise serializers.ValidationError({"error": ("Both username and password are required")})

        data["user"] = user
        return data


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        if not User.objects.filter(email=value).exists():
            raise serializers.ValidationError("This email is not registered, please try again.")
        return value

class ResetPasswordConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, min_length=6)

    def validate(self, attrs):
        try:
            uid = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError):
            raise serializers.ValidationError("Invalid user")

        if not PasswordResetTokenGenerator().check_token(user, attrs["token"]):
            raise serializers.ValidationError("Invalid or expired token")

        attrs["user"] = user
        return attrs

    def save(self):
        user = self.validated_data["user"]
        password = self.validated_data["password"]
        user.set_password(password)
        user.save()
        return user
    
class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True)

    def validate(self, attrs):
        user = self.context['request'].user
        if not user.check_password(attrs['old_password']):
            raise serializers.ValidationError("Old password is incorrect.")
        if attrs['old_password'] == attrs['new_password']:
            raise serializers.ValidationError("New password must be different.")
        return attrs
