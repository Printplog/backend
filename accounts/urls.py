# urls.py
from django.urls import path, include
from .views import *


urlpatterns = [
    path('csrf/', CsrfTokenView.as_view(), name='csrf-token'),
    path('login/', LoginView.as_view()),
    path('two-factor/setup/', AdminTwoFactorSetupView.as_view(), name='admin-two-factor-setup'),
    path('two-factor/verify/', AdminTwoFactorVerifyView.as_view(), name='admin-two-factor-verify'),
    path('logout/', LogoutView.as_view()),
    path('refresh-token/', RefreshTokenView.as_view(), name='token_refresh'),
    path('register/', RegisterView.as_view()),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot-password"),
    path("reset-password-confirm/", ResetPasswordConfirmView.as_view(), name="reset-password-confirm"),
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('google/', GoogleAuthView.as_view(), name='google-auth'),
    
    path('', include('dj_rest_auth.urls')),
    
]
