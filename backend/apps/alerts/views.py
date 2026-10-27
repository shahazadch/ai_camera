from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import TenantQuerysetMixin, require_role
from apps.tenancy.models import Role

from .models import Alert, AlertRule, AlertStatus
from .serializers import AlertAcknowledgeSerializer, AlertRuleSerializer, AlertSerializer


class AlertRuleListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = AlertRuleSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("event_type", "is_active")

    def get_queryset(self):
        return AlertRule.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_create(self, serializer):
        org = serializer.validated_data["organization"]
        require_role(self.request.user, org, [Role.OWNER, Role.ADMIN])
        serializer.save()


class AlertListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = AlertSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("status", "severity")
    ordering = ["-created_at"]

    def get_queryset(self):
        return Alert.objects.filter(organization_id__in=self.get_user_organizations()).select_related(
            "event", "event__camera", "event__camera__site"
        )

    def delete(self, request):
        """Body {"ids": [...]} deletes those alerts; no ids deletes every alert."""
        queryset = Alert.objects.filter(organization_id__in=self.get_user_organizations())
        ids = (request.data or {}).get("ids") if isinstance(request.data, dict) else None
        if ids is not None:
            if not isinstance(ids, list):
                return Response({"detail": "ids must be a list."}, status=status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(id__in=[str(i) for i in ids])
        organizations = {alert.organization for alert in queryset.select_related("organization")}
        for organization in organizations:
            require_role(request.user, organization, [Role.OWNER, Role.ADMIN])
        deleted_count = queryset.count()
        queryset.delete()
        return Response({"deleted": deleted_count})


class AlertAcknowledgeView(TenantQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"

    def get_queryset(self):
        return Alert.objects.filter(organization_id__in=self.get_user_organizations())

    def patch(self, request, pk):
        alert = get_object_or_404(self.get_queryset(), pk=pk)
        require_role(request.user, alert.organization, [Role.OWNER, Role.ADMIN, Role.OPERATOR])
        serializer = AlertAcknowledgeSerializer(alert, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data.get("status", AlertStatus.ACKNOWLEDGED)
        alert.status = new_status
        if new_status == AlertStatus.ACKNOWLEDGED:
            alert.acknowledged_by = request.user
            alert.acknowledged_at = timezone.now()
        elif new_status in (AlertStatus.RESOLVED, AlertStatus.DISMISSED):
            alert.resolved_at = timezone.now()
        alert.save()
        return Response(AlertSerializer(alert).data)
