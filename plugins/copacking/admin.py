from django.contrib import admin

from .models import PackagingMaterial, PackagingSpec


class PackagingMaterialInline(admin.TabularInline):
    model = PackagingMaterial
    extra = 1  # «material» es una lista con solo insumos (Administración → Insumos (catálogo))


@admin.register(PackagingSpec)
class PackagingSpecAdmin(admin.ModelAdmin):
    list_display = ("product", "units_per_box")
    search_fields = ("product__sku", "product__name")
    autocomplete_fields = ("product",)
    inlines = [PackagingMaterialInline]
