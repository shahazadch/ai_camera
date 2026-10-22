import hmac
import mimetypes

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cameras.authentication import EdgeServiceUser
from apps.common.permissions import TenantQuerysetMixin, require_role
from apps.tenancy.models import Role

from . import engine
from .models import AlertSound, Rule
from .serializers import AlertSoundSerializer, RuleSerializer

MANAGE_ROLES = [Role.OWNER, Role.ADMIN]


class RuleListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = RuleSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return Rule.objects.filter(organization_id__in=self.get_user_organizations()).prefetch_related(
            "cameras"
        )

    def perform_create(self, serializer):
        require_role(self.request.user, serializer.validated_data["organization"], MANAGE_ROLES)
        serializer.save()


class RuleDetailView(TenantQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Rule.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_update(self, serializer):
        require_role(self.request.user, serializer.instance.organization, MANAGE_ROLES)
        if "organization" in serializer.validated_data:
            require_role(self.request.user, serializer.validated_data["organization"], MANAGE_ROLES)
        serializer.save()

    def perform_destroy(self, instance):
        require_role(self.request.user, instance.organization, MANAGE_ROLES)
        instance.delete()


class RuleTestView(APIView):
    """Queue a test hit so the laptop player and dashboard pop-up fire once."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        org_ids = request.user.memberships.values_list("organization_id", flat=True)
        rule = get_object_or_404(Rule, id=pk, organization_id__in=list(org_ids))
        require_role(request.user, rule.organization, MANAGE_ROLES)
        return Response(engine.record_test_hit(rule), status=status.HTTP_201_CREATED)


def _alert_key_ok(request) -> bool | None:
    """True/False when an X-Alert-Key was sent, None when it wasn't."""
    alert_key = request.headers.get("X-Alert-Key", "")
    if not alert_key:
        return None
    return hmac.compare_digest(alert_key, settings.ALERT_PLAYER_KEY)


class AlertSoundListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = AlertSoundSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = None

    def get_queryset(self):
        return AlertSound.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_create(self, serializer):
        require_role(self.request.user, serializer.validated_data["organization"], MANAGE_ROLES)
        serializer.save()


class AlertSoundDetailView(TenantQuerysetMixin, generics.RetrieveDestroyAPIView):
    serializer_class = AlertSoundSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AlertSound.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_destroy(self, instance):
        require_role(self.request.user, instance.organization, MANAGE_ROLES)
        # Rules using this tune fall back to the chime instead of going silent.
        Rule.objects.filter(sound=instance.sound_key).update(sound="chime")
        instance.file.delete(save=False)
        instance.delete()


class AlertSoundFileView(APIView):
    """The tune itself, for the dashboard preview and the laptop player (X-Alert-Key)."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        key_ok = _alert_key_ok(request)
        sounds = AlertSound.objects.all()
        if key_ok is False:
            return Response({"detail": "Invalid alert key."}, status=status.HTTP_403_FORBIDDEN)
        if key_ok is None:
            if not (request.user and request.user.is_authenticated) or isinstance(request.user, EdgeServiceUser):
                return Response(
                    {"detail": "Authentication credentials were not provided."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            org_ids = request.user.memberships.values_list("organization_id", flat=True)
            sounds = sounds.filter(organization_id__in=list(org_ids))
        sound = get_object_or_404(sounds, id=pk)
        content_type = mimetypes.guess_type(sound.file.name)[0] or "application/octet-stream"
        return FileResponse(sound.file.open("rb"), content_type=content_type)


class RuleHitsView(APIView):
    """
    Recent rule hits for sound / pop-up clients.

    ``?after=<id>`` returns hits newer than that id; without it, only ``latest``
    is returned so a new client starts from now. Logged-in users see their
    organizations; scripts/alert_player.py uses ``X-Alert-Key`` and sees all.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        key_ok = _alert_key_ok(request)
        if key_ok is False:
            return Response({"detail": "Invalid alert key."}, status=status.HTTP_403_FORBIDDEN)
        if key_ok:
            org_ids = None
        elif request.user and request.user.is_authenticated and not isinstance(
            request.user, EdgeServiceUser
        ):
            org_ids = {
                str(o) for o in request.user.memberships.values_list("organization_id", flat=True)
            }
        else:
            return Response(
                {"detail": "Authentication credentials were not provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            after = int(request.query_params["after"]) if "after" in request.query_params else None
        except ValueError:
            after = None
        latest, hits = engine.hits_after(after, org_ids)
        return Response({"latest": latest, "hits": hits})
