from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    monthly_budget = models.DecimalField(max_digits=12, decimal_places=2, default=30000.0)
    
    # Salary Configurations
    salary_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    salary_day = models.IntegerField(default=10)
    auto_salary_enabled = models.BooleanField(default=True)
    last_salary_applied = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username}'s Profile"

# Automatically create user profile upon user registration
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, 'profile'):
        instance.profile.save()


class Transaction(models.Model):
    TRANSACTION_TYPES = (
        ('Debit', 'Debit'),
        ('Credit', 'Credit'),
    )
    
    SOURCES = (
        ('manual', 'manual'),
        ('sms', 'sms'),
        ('notification', 'notification'),
        ('csv', 'csv'),
        ('pdf', 'pdf'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    merchant = models.CharField(max_length=255)
    category = models.CharField(max_length=100, default='Other')
    date = models.DateTimeField()
    is_auto_detected = models.BooleanField(default=False)
    original_sms = models.TextField(null=True, blank=True)
    reference = models.CharField(max_length=100, null=True, blank=True)
    source = models.CharField(max_length=20, choices=SOURCES, default='manual')
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.type} - ₹{self.amount} at {self.merchant} by {self.user.username}"


class Conflict(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='conflicts')
    sms_transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='conflicts')
    
    # Conflicting statement transaction details
    imported_amount = models.DecimalField(max_digits=12, decimal_places=2)
    imported_merchant = models.CharField(max_length=255)
    imported_date = models.DateTimeField()
    imported_reference = models.CharField(max_length=100, null=True, blank=True)
    resolved = models.BooleanField(default=False)

    def __str__(self):
        return f"Conflict: SMS ₹{self.sms_transaction.amount if self.sms_transaction else 'N/A'} vs Statement ₹{self.imported_amount} for {self.user.username}"
