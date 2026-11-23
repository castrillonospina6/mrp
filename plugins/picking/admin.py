from django.contrib import admin

from .models import PickLine


class PickLineInline(admin.TabularInline):
    model = PickLine
    extra = 0
    autocomplete_fields = ("product",)
    verbose_name_plural = "Líneas de picking (plugin Picking)"


@admin.register(PickLine)
class PickLineAdmin(admin.ModelAdmin):
    list_display = ("order", "location", "product", "qty_requested", "qty_picked")
    list_filter = ("order__site",)
    search_fields = ("order__number", "location", "product__sku")
