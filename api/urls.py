from rest_framework.routers import DefaultRouter
from .views import *
from django.urls import path

router = DefaultRouter()
router.register(r'tools', ToolViewSet, basename='tool')
router.register(r'templates', TemplateViewSet, basename='template')
router.register(r'admin/templates', AdminTemplateViewSet, basename='admin-template')
router.register(r'purchased-templates', PurchasedTemplateViewSet, basename='purchased-template')
router.register(r'tutorials', TutorialViewSet, basename='tutorial')
router.register(r'fonts', FontViewSet, basename='font')
router.register(r'settings', SiteSettingsViewSet, basename='settings')
router.register(r'transform-variables', TransformVariableViewSet, basename='transform-variable')

urlpatterns = [
    path("track/<str:tracking_id>/", PublicTemplateTrackingView.as_view(), name="track-template"),
    path("download-doc/", DownloadDoc.as_view(), name="download-doc"),
    path("remove-background/", RemoveBackgroundView.as_view(), name="remove-background"),

    # Admin views
    path("admin/overview/", AdminOverview.as_view(), name="admin-overview"),
    path("admin/users/", AdminUsers.as_view(), name="admin-users"),
    path("admin/users/<int:user_id>/", AdminUserDetails.as_view(), name="admin-user-details"),
    path("admin/documents/", AdminDocuments.as_view(), name="admin-documents"),
]
urlpatterns += router.urls
