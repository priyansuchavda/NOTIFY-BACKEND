from django.contrib import admin

from .models import UserProfile, Transaction, Conflict


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'current_balance',
        'monthly_budget',
        'salary_amount',
        'salary_day',
        'auto_salary_enabled',
        'last_salary_applied',
    )
    list_filter = ('auto_salary_enabled',)
    search_fields = ('user__username', 'user__email')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'type', 'amount', 'merchant', 'category', 'date', 'source')
    list_filter = ('type', 'source', 'category')
    search_fields = ('merchant', 'user__username', 'reference')
    date_hierarchy = 'date'


@admin.register(Conflict)
class ConflictAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'imported_amount', 'imported_merchant', 'imported_date', 'resolved')
    list_filter = ('resolved',)
    search_fields = ('imported_merchant', 'user__username')
