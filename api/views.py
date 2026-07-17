import csv
import io
import re
from datetime import datetime, date, timedelta, timezone as py_timezone
from decimal import Decimal
from django.utils import timezone
timezone.utc = py_timezone.utc
from django.db import models
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, permissions, status, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
import pdfplumber

from .models import UserProfile, Transaction, Conflict
from .serializers import UserProfileSerializer, UserRegisterSerializer, TransactionSerializer, ConflictSerializer
from .matching_engine import match_and_merge_transaction

# --- Helper to generate JWT tokens for a user ---
def get_tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }


class UserRegisterView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = UserRegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            tokens = get_tokens_for_user(user)
            return Response({
                "status": "success",
                "tokens": tokens,
                "user": {
                    "username": user.username,
                    "email": user.email
                }
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer

    def get_object(self):
        # Return the profile linked to the request user
        return self.request.user.profile

    def update(self, request, *args, **kwargs):
        # Override to recalculate/checkpoint balance if it was directly changed
        profile = self.get_object()
        old_balance = profile.current_balance
        
        response = super().update(request, *args, **kwargs)
        
        # Log balance updates
        profile.refresh_from_db()
        if profile.current_balance != old_balance:
            print(f"User updated balance checkpoint from {old_balance} to {profile.current_balance}")
            
        return response


from rest_framework.pagination import PageNumberPagination

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class TransactionViewSet(viewsets.ModelViewSet):
    serializer_class = TransactionSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        # Check and apply automated salary credits on the 10th if needed before querying
        self._apply_automated_salary()
        return Transaction.objects.filter(user=self.request.user).order_by('-date', '-id')

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        # Get old transaction values to recalculate balance difference
        instance = self.get_object()
        old_amount = instance.amount
        old_type = instance.type
        
        transaction = serializer.save()
        
        # Calculate balance delta adjustment
        profile = self.request.user.profile
        # Reverse old
        if old_type == 'Debit':
            profile.current_balance += old_amount
        else:
            profile.current_balance -= old_amount
            
        # Add new
        if transaction.type == 'Debit':
            profile.current_balance -= transaction.amount
        else:
            profile.current_balance += transaction.amount
        profile.save()

    def perform_destroy(self, instance):
        # Reverse balance update before deleting
        profile = self.request.user.profile
        if instance.type == 'Debit':
            profile.current_balance += instance.amount
        else:
            profile.current_balance -= instance.amount
        profile.save()
        
        instance.delete()

    def _apply_automated_salary(self):
        # Apply salary automated credits if enabled and due
        user = self.request.user
        if not hasattr(user, 'profile'):
            return
            
        profile = user.profile
        if not profile.auto_salary_enabled or profile.salary_amount <= 0:
            return

        today = date.today()
        salary_day = profile.salary_day
        
        # Check if we are at or past the salary day for the current month
        # and it hasn't been applied for this month yet
        if today.day >= salary_day:
            last_applied = profile.last_salary_applied
            needs_apply = False
            
            if not last_applied:
                needs_apply = True
            else:
                # Needs apply if last applied was in a previous month
                if last_applied.year < today.year or (last_applied.year == today.year and last_applied.month < today.month):
                    needs_apply = True
            
            if needs_apply:
                # Apply salary transaction
                salary_date = datetime(today.year, today.month, salary_day, 9, 0, 0, tzinfo=timezone.utc)
                Transaction.objects.create(
                    user=user,
                    amount=profile.salary_amount,
                    type='Credit',
                    merchant='Salary Credit (Auto)',
                    category='Salary',
                    date=salary_date,
                    is_auto_detected=True,
                    source='manual',
                    is_verified=True
                )
                
                # Update profile
                profile.current_balance += profile.salary_amount
                profile.last_salary_applied = today
                profile.save()
                print(f"Applied automated salary credit of ₹{profile.salary_amount} for user {user.username}")


class BulkInterceptionsView(APIView):
    def post(self, request):
        """
        Receives intercepted alert lists from Flutter:
        [
          { "sender": "AD-HDFCBK", "body": "...", "timestamp": 1720932000, "type": "sms" }
        ]
        """
        alerts = request.data
        if not isinstance(alerts, list):
            alerts = [alerts]
            
        inserted_count = 0
        parsed_results = []
        user = request.user

        for alert in alerts:
            body = alert.get('body', '')
            sender = alert.get('sender', '')
            timestamp_val = alert.get('timestamp')
            source_type = alert.get('type', 'sms')
            
            dt = datetime.fromtimestamp(timestamp_val / 1000.0, tz=timezone.utc) if timestamp_val else timezone.now()
            
            # Simple server-side regex extraction fallback to double check parsing matches
            # Regex to extract amount
            amount_match = re.search(r'(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{2})?)', body, re.IGNORECASE)
            if not amount_match:
                continue
                
            amount = Decimal(amount_match.group(1).replace(',', ''))
            
            # Determine type
            is_credit = re.search(r'(credited|received|added|deposited|refunded|refund)', body, re.IGNORECASE)
            is_debit = re.search(r'(debited|spent|paid|sent|withdrawn|charged)', body, re.IGNORECASE)
            
            tx_type = 'Debit'
            if is_credit and not is_debit:
                tx_type = 'Credit'
                
            # Clean merchant name
            merchant = sender
            merchant_match = re.search(r'(?:to|at|paid to)\s+([^;]+?)(?:\s+via|\s+from|\s+on|\s+ref|\s+a/c|\.|$)', body, re.IGNORECASE)
            if merchant_match:
                merchant = merchant_match.group(1).strip()
                # Clean reference keys
                merchant = re.sub(r'\b(?:a/c|ac|account|xxx+|ending|via|using|bank|upi|card)\b', '', merchant, flags=re.IGNORECASE).strip()

            # Deduplication: Check if there's already a transaction with same amount, date (within 2 mins)
            # and similarity
            exists = Transaction.objects.filter(
                user=user,
                amount=amount,
                type=tx_type,
                date__range=(dt - timedelta(minutes=2), dt + timedelta(minutes=2))
            ).exists()
            
            if not exists:
                tx = Transaction.objects.create(
                    user=user,
                    amount=amount,
                    type=tx_type,
                    merchant=merchant or "UPI Payment",
                    category='Other',
                    date=dt,
                    is_auto_detected=True,
                    original_sms=body,
                    source=source_type,
                    is_verified=False
                )
                # Update balance
                profile = user.profile
                if tx_type == 'Debit':
                    profile.current_balance -= amount
                else:
                    profile.current_balance += amount
                profile.save()
                
                inserted_count += 1
                parsed_results.append({
                    "id": tx.id,
                    "amount": float(tx.amount),
                    "type": tx.type,
                    "merchant": tx.merchant,
                    "date": tx.date.isoformat()
                })

        return Response({
            "status": "success",
            "inserted": inserted_count,
            "parsed": parsed_results
        }, status=status.HTTP_200_OK)


class DashboardView(APIView):
    def get(self, request):
        user = request.user
        profile = user.profile
        
        # Fetch current limits
        total_budget = profile.monthly_budget
        
        # Debits in current month
        today = date.today()
        first_of_month = datetime(today.year, today.month, 1, 0, 0, 0, tzinfo=timezone.utc)
        
        total_debits = Transaction.objects.filter(
            user=user,
            type='Debit',
            date__gte=first_of_month
        ).aggregate(sum=models.Sum('amount'))['sum'] or Decimal(0.0)

        total_credits = Transaction.objects.filter(
            user=user,
            type='Credit',
            date__gte=first_of_month
        ).aggregate(sum=models.Sum('amount'))['sum'] or Decimal(0.0)

        remaining_budget = total_budget - total_debits
        
        recent_txs = Transaction.objects.filter(user=user).order_by('-date', '-id')[:5]
        serializer = TransactionSerializer(recent_txs, many=True)

        return Response({
            "balance": float(profile.current_balance),
            "budget": {
                "total": float(total_budget),
                "spent": float(total_debits),
                "remaining": float(remaining_budget),
                "usage_percentage": float((total_debits / total_budget) * 100) if total_budget > 0 else 0.0
            },
            "cashflow_summary": {
                "income": float(total_credits),
                "expenses": float(total_debits),
                "saved": float(total_credits - total_debits)
            },
            "recent_transactions": serializer.data
        }, status=status.HTTP_200_OK)


class InsightsView(APIView):
    def get(self, request):
        user = request.user
        profile = user.profile
        
        # Calculate categories breakdown
        today = date.today()
        first_of_month = datetime(today.year, today.month, 1, 0, 0, 0, tzinfo=timezone.utc)
        
        debits = Transaction.objects.filter(
            user=user,
            type='Debit',
            date__gte=first_of_month
        )
        
        breakdown = {}
        for tx in debits:
            cat = tx.category
            breakdown[cat] = breakdown.get(cat, 0.0) + float(tx.amount)
            
        # Calculate salary cycle savings
        # Cycles run from 10th of month to 9th of next month (based on profile salary_day)
        salary_day = profile.salary_day
        
        # Get start dates for current and previous cycles
        if today.day >= salary_day:
            curr_cycle_start = datetime(today.year, today.month, salary_day, 0, 0, 0, tzinfo=timezone.utc)
            # Prev cycle start is one month earlier
            prev_year = today.year if today.month > 1 else today.year - 1
            prev_month = today.month - 1 if today.month > 1 else 12
            prev_cycle_start = datetime(prev_year, prev_month, salary_day, 0, 0, 0, tzinfo=timezone.utc)
        else:
            prev_year = today.year if today.month > 1 else today.year - 1
            prev_month = today.month - 1 if today.month > 1 else 12
            curr_cycle_start = datetime(prev_year, prev_month, salary_day, 0, 0, 0, tzinfo=timezone.utc)
            
            prev_year_2 = prev_year if prev_month > 1 else prev_year - 1
            prev_month_2 = prev_month - 1 if prev_month > 1 else 12
            prev_cycle_start = datetime(prev_year_2, prev_month_2, salary_day, 0, 0, 0, tzinfo=timezone.utc)

        # Query totals for cycles
        def get_cycle_stats(start_date, end_date):
            txs = Transaction.objects.filter(user=user, date__range=(start_date, end_date))
            income = txs.filter(type='Credit').aggregate(sum=models.Sum('amount'))['sum'] or Decimal(0.0)
            expenses = txs.filter(type='Debit').aggregate(sum=models.Sum('amount'))['sum'] or Decimal(0.0)
            saved = income - expenses
            saved_pct = (saved / income) * 100 if income > 0 else 0.0
            return float(income), float(expenses), float(saved), float(saved_pct)

        curr_income, curr_expenses, curr_saved, curr_saved_pct = get_cycle_stats(curr_cycle_start, timezone.now())
        
        # For previous cycle, end date is the start of current cycle
        prev_income, prev_expenses, prev_saved, prev_saved_pct = get_cycle_stats(prev_cycle_start, curr_cycle_start)

        diff = curr_saved - prev_saved
        diff_pct = (diff / prev_saved) * 100 if prev_saved > 0 else 0.0
        
        insight_text = "Not enough data yet."
        if prev_saved > 0:
            if diff >= 0:
                insight_text = f"You saved ₹{diff:.0f} more (+{diff_pct:.0f}%) than the previous salary cycle!"
            else:
                insight_text = f"You saved ₹{abs(diff):.0f} less ({diff_pct:.0f}%) than the previous salary cycle."

        return Response({
            "category_breakdown": breakdown,
            "savings_cycle": {
                "previous_cycle": {
                    "income": prev_income,
                    "expenses": prev_expenses,
                    "saved_amount": prev_saved,
                    "savings_percentage": prev_saved_pct
                },
                "current_cycle": {
                    "income": curr_income,
                    "expenses": curr_expenses,
                    "saved_amount": curr_saved,
                    "savings_percentage": curr_saved_pct
                },
                "comparison": {
                    "saved_more_amount": diff,
                    "improvement_percentage": diff_pct,
                    "insight_text": insight_text
                }
            }
        }, status=status.HTTP_200_OK)


def parse_canara_statement(text, user):
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    parsed_transactions = []
    
    # Matches DD-MM-YYYY Date at start of line, followed by optional particulars, amount, balance
    date_regex = re.compile(r'^(\d{2}-\d{2}-\d{4})\s*(.*?)\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$')
    
    # Find all line indices matching the date regex
    date_indices = []
    for idx, line in enumerate(lines):
        if date_regex.match(line):
            date_indices.append(idx)
            
    if not date_indices:
        return []
        
    for k, date_idx in enumerate(date_indices):
        date_line = lines[date_idx]
        match = date_regex.match(date_line)
        date_str = match.group(1)
        middle_desc = match.group(2)
        amount_str = match.group(3)
        balance_str = match.group(4)
        
        # Particulars before the date line: from the previous date line index + 1 to date_idx - 1
        start_idx = date_indices[k-1] + 1 if k > 0 else 0
        particulars_before = lines[start_idx:date_idx]
        
        # Particulars after the date line: from date_idx + 1 to the next date line index - 1
        end_idx = date_indices[k+1] if k < len(date_indices) - 1 else len(lines)
        particulars_after = lines[date_idx+1:end_idx]
        
        # Combine all particulars lines
        all_particulars = particulars_before + [middle_desc] + particulars_after
        particulars = " ".join([p.strip() for p in all_particulars if p.strip()])
        
        # Scan particulars_after for a transaction time (HH:MM:SS)
        tx_time_str = "00:00:00"
        time_match = re.search(r'\b(\d{2}:\d{2}:\d{2})\b', " ".join(particulars_after))
        if time_match:
            tx_time_str = time_match.group(1)
            
        try:
            # Reconstruct datetime object incorporating the precise timestamp
            tx_datetime = datetime.strptime(f"{date_str} {tx_time_str}", '%d-%m-%Y %H:%M:%S')
            dt = timezone.make_aware(tx_datetime)
        except ValueError:
            try:
                tx_date = datetime.strptime(date_str, '%d-%m-%Y')
                dt = timezone.make_aware(tx_date)
            except ValueError:
                dt = timezone.now()
                
        amount = Decimal(amount_str.replace(',', ''))
        balance = Decimal(balance_str.replace(',', ''))
        
        # Clean merchant name
        merchant = "Canara Transaction"
        upi_match = re.search(r'UPI/(?:DR|CR)/[^/]+/([^/]+)', particulars, re.IGNORECASE)
        if upi_match:
            merchant = upi_match.group(1).strip()
        else:
            if particulars_before:
                merchant = " ".join(particulars_before[:2])
            else:
                words = particulars.split()
                if words:
                    merchant = " ".join(words[:2])
                    
        # Remove reference indicators
        merchant = re.sub(r'\b(?:a/c|ac|account|xxx+|ending|via|using|bank|upi|card|ref|chq)\b', '', merchant, flags=re.IGNORECASE).strip()
        
        # Reference extraction
        ref = None
        ref_match = re.search(r'Chq:\s*(\w+)', particulars, re.IGNORECASE)
        if ref_match:
            ref = ref_match.group(1)
        else:
            upi_ref = re.search(r'UPI/(?:DR|CR)/(\d{12})', particulars, re.IGNORECASE)
            if upi_ref:
                ref = upi_ref.group(1)
                
        parsed_transactions.append({
            "amount": amount,
            "balance": balance,
            "type": 'Debit', # will be corrected in balance difference pass
            "date": dt,
            "merchant": merchant or "Canara Bank Transfer",
            "reference": ref,
            "particulars": particulars
        })
        
    # Reconstruct Debit/Credit using running balance
    for idx, tx in enumerate(parsed_transactions):
        tx_type = 'Debit'
        if idx == 0:
            if 'UPI/CR' in tx['particulars'] or 'CR' in tx['particulars']:
                tx_type = 'Credit'
            else:
                tx_type = 'Debit'
        else:
            prev_balance = parsed_transactions[idx-1]['balance']
            if tx['balance'] > prev_balance:
                tx_type = 'Credit'
            else:
                tx_type = 'Debit'
        tx['type'] = tx_type
        
    return parsed_transactions


class StatementImportView(APIView):
    def post(self, request):
        user = request.user
        file_obj = request.FILES.get('file')
        file_type = request.data.get('type') # 'csv' or 'pdf'

        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)

        parsed_transactions = []

        try:
            if file_type == 'csv' or file_obj.name.endswith('.csv'):
                # Read CSV
                csv_data = file_obj.read().decode('utf-8')
                reader = csv.reader(io.StringIO(csv_data))
                
                # Simple header detection and columns extraction
                headers = next(reader, None)
                # Fallback row loop
                for row in reader:
                    if len(row) < 3:
                        continue
                    try:
                        # Simple heuristics to map Date, Description, Amount
                        raw_date = row[0]
                        narration = row[1]
                        raw_amount = row[2]
                        raw_type = row[3] if len(row) > 3 else 'Debit'
                        
                        dt = timezone.make_aware(datetime.strptime(raw_date, "%Y-%m-%d"))
                        amount = Decimal(raw_amount.replace(',', ''))
                        
                        parsed_transactions.append({
                            "amount": amount,
                            "type": 'Credit' if 'credit' in raw_type.lower() else 'Debit',
                            "date": dt,
                            "merchant": narration,
                            "reference": row[4] if len(row) > 4 else None,
                        })
                    except Exception as row_err:
                        continue
                        
            elif file_type == 'pdf' or file_obj.name.endswith('.pdf'):
                # Read PDF text
                with pdfplumber.open(file_obj) as pdf:
                    text = ""
                    for page in pdf.pages:
                        text += page.extract_text() or ""
                        
                # Check for Canara Bank format
                if "Statement for A/c" in text and ("CNRB" in text or "Canara" in text):
                    parsed_transactions = parse_canara_statement(text, user)
                else:
                    # Fallback general parser
                    lines = text.split('\n')
                    for line in lines:
                        date_match = re.search(r'(\d{2}[-/]\d{2}[-/]\d{4}|\d{2}-\w{3}-\d{2}|\d{4}-\d{2}-\d{2})', line)
                        amount_match = re.search(r'(?:rs\.?|inr|₹)?\s*([\d,]+\.\d{2})', line, re.IGNORECASE)
                        
                        if date_match and amount_match:
                            raw_date = date_match.group(1)
                            amount = Decimal(amount_match.group(1).replace(',', ''))
                            narration = line.replace(raw_date, '').replace(amount_match.group(0), '').strip()
                            
                            try:
                                for fmt in ("%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
                                    try:
                                        parsed_date = datetime.strptime(raw_date, fmt)
                                        dt = timezone.make_aware(parsed_date)
                                        break
                                    except ValueError:
                                        continue
                                else:
                                    dt = timezone.now()
                                    
                                parsed_transactions.append({
                                    "amount": amount,
                                    "type": 'Credit' if 'credit' in line.lower() else 'Debit',
                                    "date": dt,
                                    "merchant": narration or "Bank Transfer",
                                    "reference": None
                                })
                            except Exception:
                                continue
            else:
                return Response({"error": "Unsupported file format. Please upload CSV or PDF."}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as file_err:
            return Response({"error": f"Failed to parse statement: {str(file_err)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Run through deduplication matching engine
        engine_stats = {
            "duplicate": 0,
            "merged": 0,
            "conflict": 0,
            "created": 0
        }
        
        results = []

        for p_tx in parsed_transactions:
            match_res = match_and_merge_transaction(user, p_tx)
            status_val = match_res['status']
            engine_stats[status_val] += 1
            
            if status_val in ['merged', 'created']:
                tx = match_res['transaction']
                results.append({
                    "id": tx.id,
                    "amount": float(tx.amount),
                    "merchant": tx.merchant,
                    "status": "Imported"
                })
            elif status_val == 'conflict':
                conflict = match_res['conflict']
                results.append({
                    "id": conflict.id,
                    "amount": float(conflict.imported_amount),
                    "merchant": conflict.imported_merchant,
                    "status": "Conflict"
                })

        return Response({
            "status": "success",
            "stats": engine_stats,
            "imported_count": len(results),
            "results": results
        }, status=status.HTTP_200_OK)
