from django.contrib import admin
from .models import *

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('is_staff', 'is_active', 'date_joined', 'source')
    search_fields = ('username', 'email')
    ordering = ('-date_joined',)


@admin.register(AdminTwoFactorProfile)
class AdminTwoFactorProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'confirmed_at', 'created_at', 'updated_at')
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('user', 'confirmed_at', 'created_at', 'updated_at')
    exclude = ('encrypted_secret', 'recovery_code_hashes', 'last_used_counter')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
