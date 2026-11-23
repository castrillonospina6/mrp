from django.contrib import admin

from .models import CustomerEanFormat, EanControl, EanLine, EanLog, EanSheet


class EanLineInline(admin.TabularInline):
    model = EanLine
    extra = 0
    fields = ("ean", "quantity", "description", "position")


@admin.register(EanSheet)
class EanSheetAdmin(admin.ModelAdmin):
    list_display = ("order", "status", "source", "loaded_by", "loaded_at", "updated_at")
    list_filter = ("status", "source")
    search_fields = ("order__number", "order__customer__name", "order__product__sku")
    raw_id_fields = ("order",)


@admin.register(EanLine)
class EanLineAdmin(admin.ModelAdmin):
    list_display = ("order", "ean", "quantity", "description")
    search_fields = ("ean", "order__number", "description")
    raw_id_fields = ("order",)


@admin.register(EanLog)
class EanLogAdmin(admin.ModelAdmin):
    list_display = ("at", "order", "action", "user", "reason")
    search_fields = ("order__number", "detail", "reason")
    raw_id_fields = ("order",)
    readonly_fields = ("order", "user", "action", "detail", "reason", "at")


@admin.register(CustomerEanFormat)
class CustomerEanFormatAdmin(admin.ModelAdmin):
    list_display = ("customer", "ean_column", "qty_column", "desc_column", "updated_at")


@admin.register(EanControl)
class EanControlAdmin(admin.ModelAdmin):
    list_display = ("order", "stage_key", "ean", "enabled", "position", "changed_by", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("ean", "order__number")
