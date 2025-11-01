from django.contrib import admin
from django.db import models 

from django.utils.html import format_html
from django.urls import reverse
from django.contrib.auth.hashers import make_password
from django import forms
from mptt.admin import MPTTModelAdmin

from ..app.models import App

from ..payment.models import Transaction

from ..order.models import Payment

from ..invoice.models import Order


from ..product.models import Channel, Product,Category, ProductVariantChannelListing
from ..account.models import StaffNotificationRecipient, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email','first_name','last_name')
    can_delete = False
    exclude = ['last_login','is_superuser','private_metadata','metadata','addresses','note','last_password_reset_request',
             'default_shipping_address','default_billing_address','jwt_token_key','language_code','search_document',
             'uuid','user_permissions']

class CategoryAdminForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "slug", "parent", "background_image"]  # Only required fields

class CategoryAdmin(MPTTModelAdmin):
    form = CategoryAdminForm
    list_display = ["name", "slug", "parent"]
    search_fields = ["name", "slug"]
    autocomplete_fields = ["parent"]  # Enables search in parent dropdown

    fieldsets = (
        (None, {
            "fields": ("name", "slug", "parent", "background_image"),
        }),
    )


class StaffNotificationRecipientForm(forms.ModelForm):
    class Meta:
        model = StaffNotificationRecipient
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Restrict user choices to staff users (optional)
        self.fields["user"].queryset = User.objects.filter(is_active=True, is_staff=True)

        # Define how each user should appear in the select list
        # label_from_instance is used by ModelChoiceField to show the option label
        def label(obj):
            # try first+last, fall back to username/email if empty
            name_part = f"{obj.last_name} {obj.first_name}".strip()
            if name_part:
                return f"{name_part} ({obj.email})"
            return f"{obj.get_username()} ({obj.email})"

        self.fields["user"].label_from_instance = label

@admin.register(StaffNotificationRecipient)
class StaffNotificationRecipientAdmin(admin.ModelAdmin):
    form = StaffNotificationRecipientForm

    list_display = ("get_email", "user", "staff_email", "active")
    list_filter = ("active",)
    search_fields = ("user__email", "user__first_name", "user__last_name", "staff_email")
    ordering = ("staff_email",)


admin.site.register(App)
admin.site.register(Channel)
admin.site.register(Order)
admin.site.register(Payment)
admin.site.register(Transaction)
admin.site.register(Category, CategoryAdmin)
