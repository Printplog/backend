"""
URL configuration for serverConfig project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings

from analytics.views import LogVisitView
from serverConfig.views import api_docs_redirect

urlpatterns = [
    path('api/v1/docs', api_docs_redirect, name='v1-api-docs'),
    path('admin/', admin.site.urls),
    path('api/accounts/',include("accounts.urls")),
    path('api/',include("api.urls")),
    path('api/',include("wallet.urls")),
    path('api/analytics/',include("analytics.urls")),
    # Innocuous-named alias of /api/analytics/log-visit/ — bypasses ad-blocker
    # filter lists that match URLs containing "analytics" or "log".
    path('api/u/p/', LogVisitView.as_view(), name='analytics-track-page'),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns += [
        path('__debug__/', include(debug_toolbar.urls)),
    ]

from django.urls import re_path
from django.views.static import serve

# Unified Media Serving (Cloudflare acts as the real CDN)
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
