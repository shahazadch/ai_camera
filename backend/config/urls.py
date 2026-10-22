from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.events.views import DashboardSummaryView, HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("api/v1/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("api/v1/", include("apps.tenancy.urls")),
    path("api/v1/", include("apps.sites.urls")),
    path("api/v1/", include("apps.cameras.urls")),
    path("api/v1/", include("apps.events.urls")),
    path("api/v1/", include("apps.alerts.urls")),
    path("api/v1/", include("apps.audit.urls")),
    path("api/v1/", include("apps.identity.urls")),
    path("api/v1/", include("apps.analytics.urls")),
    path("api/v1/", include("apps.safety.urls")),
    path("api/v1/", include("apps.ops.urls")),
    path("api/v1/", include("apps.rules.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
