from rest_framework import serializers
from django.contrib.auth.models import User
from .models import UserProfile, Transaction, Conflict

class UserProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    
    class Meta:
        model = UserProfile
        fields = ('username', 'email', 'current_balance', 'monthly_budget', 'salary_amount', 'salary_day', 'auto_salary_enabled')


class UserRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    current_balance = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, write_only=True)
    salary_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, write_only=True)
    salary_day = serializers.IntegerField(required=False, write_only=True)
    
    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'current_balance', 'salary_amount', 'salary_day')

    def create(self, validated_data):
        username = validated_data['username']
        email = validated_data.get('email', '')
        password = validated_data['password']
        
        # Extract initial profile setup data
        current_balance = validated_data.pop('current_balance', 0.0)
        salary_amount = validated_data.pop('salary_amount', 0.0)
        salary_day = validated_data.pop('salary_day', 10)

        # Create user
        user = User.objects.create_user(username=username, email=email, password=password)
        
        # Update user profile details created by signals
        profile = user.profile
        profile.current_balance = current_balance
        profile.salary_amount = salary_amount
        profile.salary_day = salary_day
        profile.auto_salary_enabled = salary_amount > 0
        profile.save()
        
        return user


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = (
            'id', 
            'amount', 
            'type', 
            'merchant', 
            'category', 
            'date', 
            'is_auto_detected', 
            'original_sms', 
            'reference', 
            'source', 
            'is_verified'
        )
        read_only_fields = ('id', 'is_verified')

    def create(self, validated_data):
        # Bind transaction to the request user
        validated_data['user'] = self.context['request'].user
        
        transaction = Transaction.objects.create(**validated_data)
        
        # Update user profile balance
        profile = transaction.user.profile
        if transaction.type == 'Debit':
            profile.current_balance -= transaction.amount
        else:
            profile.current_balance += transaction.amount
        profile.save()
        
        return transaction


class ConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conflict
        fields = '__all__'
