"""Bandeja de avisos de cada usuario."""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import Notification


@login_required
def inbox(request):
    qs = request.user.notifications.all()
    view = request.GET.get("ver", "pendientes")
    if view == "pendientes":
        qs = qs.filter(read_at__isnull=True)
    kind = request.GET.get("tipo", "")
    if kind in Notification.Kind.values:
        qs = qs.filter(kind=kind)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "mes_core/inbox.html", {
        "page": page, "view": view, "kind": kind, "kinds": Notification.Kind.choices,
        "n_unread": request.user.notifications.filter(read_at__isnull=True).count(),
    })


@login_required
def inbox_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    if n.read_at is None:
        Notification.objects.filter(pk=n.pk).update(read_at=timezone.now())
    if n.url and url_has_allowed_host_and_scheme(n.url, allowed_hosts={request.get_host()}):
        return redirect(n.url)
    return redirect("mes_core:inbox")


@login_required
@require_POST
def inbox_read_all(request):
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect("mes_core:inbox")
