from django.urls import path

from api.views.api_platform import (
    PublicEmbedFinalizeView,
    PublicEmbedPreviewAssetView,
    PublicEmbedSessionView,
    PublicRenderDownloadView,
    V1DocumentDetailView,
    V1DocumentListCreateView,
    V1DocumentUpgradeView,
    V1DocumentRenderCreateView,
    V1EmbedSessionCreateView,
    V1EmbedSessionEditView,
    V1EmbedSessionRevokeView,
    V1TemplateListView,
    V1RenderDetailView,
    V1RenderWatchView,
)


urlpatterns = [
    path("templates", V1TemplateListView.as_view(), name="v1-template-list"),
    path("documents", V1DocumentListCreateView.as_view(), name="v1-document-list-create"),
    path("documents/<uuid:document_id>", V1DocumentDetailView.as_view(), name="v1-document-detail"),
    path("documents/<uuid:document_id>/session", V1EmbedSessionEditView.as_view(), name="v1-document-edit-session"),
    path("documents/<uuid:document_id>/upgrade", V1DocumentUpgradeView.as_view(), name="v1-document-upgrade"),
    path("documents/<uuid:document_id>/render", V1DocumentRenderCreateView.as_view(), name="v1-document-render"),
    path("renders/<uuid:job_id>", V1RenderDetailView.as_view(), name="v1-render-detail"),
    path("renders/<uuid:job_id>/watch", V1RenderWatchView.as_view(), name="v1-render-watch"),
    path("render-download/<uuid:job_id>", PublicRenderDownloadView.as_view(), name="public-render-download"),
    path("embed-sessions", V1EmbedSessionCreateView.as_view(), name="v1-embed-session-create"),
    path("embed-sessions/<uuid:session_id>", V1EmbedSessionRevokeView.as_view(), name="v1-embed-session-revoke"),
    path("embed/session", PublicEmbedSessionView.as_view(), name="public-embed-session"),
    path("embed/preview-assets/<str:asset_id>", PublicEmbedPreviewAssetView.as_view(), name="public-embed-preview-asset"),
    path("embed/finalize", PublicEmbedFinalizeView.as_view(), name="public-embed-finalize"),
]
