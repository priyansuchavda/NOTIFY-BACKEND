from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from drf_spectacular.utils import extend_schema, extend_schema_view
from .views import (
    UserRegisterView,
    UserProfileView,
    TransactionViewSet,
    BulkInterceptionsView,
    DashboardView,
    InsightsView,
    StatementImportView
)

router = DefaultRouter()
router.register(r'transactions', TransactionViewSet, basename='transaction')

TokenObtainPairTagged = extend_schema_view(
    post=extend_schema(tags=['Auth']),
)(TokenObtainPairView)

TokenRefreshTagged = extend_schema_view(
    post=extend_schema(tags=['Auth']),
)(TokenRefreshView)

urlpatterns = [
    # Auth routing
    path('auth/register/', UserRegisterView.as_view(), name='register'),
    path('auth/token/', TokenObtainPairTagged.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshTagged.as_view(), name='token_refresh'),
    
    # Profile routing
    path('profile/', UserProfileView.as_view(), name='profile'),
    
    # Dashboards & Analytics
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    path('insights/', InsightsView.as_view(), name='insights'),
    
    # Bulk uploads & statement imports
    path('transactions/bulk/', BulkInterceptionsView.as_view(), name='transactions_bulk'),
    path('transactions/import/', StatementImportView.as_view(), name='statement_import'),
    
    # CRUD views router inclusion
    path('', include(router.urls)),
]
