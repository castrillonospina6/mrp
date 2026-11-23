from django.contrib import admin

from .models import Lot, YieldStandard


@admin.register(YieldStandard)
class YieldStandardAdmin(admin.ModelAdmin):
    list_display = ("input_product", "output_product", "expected_percent", "tolerance_percent")
    list_filter = ("input_product",)
    autocomplete_fields = ("input_product", "output_product")


@admin.register(Lot)
class LotAdmin(admin.ModelAdmin):
    list_display = ("code", "product", "direction", "quantity", "order", "supplier_lot", "produced_at", "expires_on")
    list_filter = ("direction", "product")
    search_fields = ("code", "supplier_lot", "order__number")
    readonly_fields = ("produced_at",)
