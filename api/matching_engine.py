import re
from datetime import timedelta
from django.utils import timezone
from .models import Transaction, Conflict

def normalize_merchant(name):
    if not name:
        return ""
    # Convert to lowercase, replace symbols and multiple spaces
    clean = re.sub(r'[\*#_@\-\/]+', ' ', name)
    clean = re.sub(r'\b(?:a/c|ac|account|xxx+|ending|via|using|bank|upi|card|imps|neft|wallet|pay)\b', ' ', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\s+', ' ', clean).strip().lower()
    return clean

def levenshtein_similarity(s1, s2):
    s1 = normalize_merchant(s1)
    s2 = normalize_merchant(s2)
    if not s1 or not s2:
        return 0.0
    
    # Calculate Levenshtein distance
    if len(s1) < len(s2):
        return levenshtein_similarity(s2, s1)

    if len(s2) == 0:
        return 0.0

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    distance = previous_row[-1]
    max_len = max(len(s1), len(s2))
    return (1.0 - (distance / max_len)) * 100.0

def match_and_merge_transaction(user, imported_tx):
    """
    imported_tx is a dict: 
    {
       'amount': decimal, 
       'type': 'Debit'|'Credit', 
       'date': datetime, 
       'merchant': string, 
       'reference': string (optional)
    }
    """
    amount = imported_tx['amount']
    tx_type = imported_tx['type']
    date = imported_tx['date']
    merchant = imported_tx['merchant']
    reference = imported_tx.get('reference')

    # Get all transactions of the user matching the same type and amount (within a sensible date window)
    date_start = date - timedelta(days=2)
    date_end = date + timedelta(days=2)
    
    candidates = Transaction.objects.filter(
        user=user,
        type=tx_type,
        amount=amount,
        date__range=(date_start, date_end)
    )

    # Level 1: Strict Match
    # If reference matches directly, it's 100% a duplicate
    if reference:
        l1_matches = candidates.filter(reference=reference)
        if l1_matches.exists():
            return {"status": "duplicate", "transaction": l1_matches.first()}

    # Level 2: Probable Match (Time difference < 5 mins, merchant similarity >= 80%)
    for candidate in candidates:
        time_diff = abs((candidate.date - date).total_seconds())
        similarity = levenshtein_similarity(candidate.merchant, merchant)
        
        if time_diff <= 300 and similarity >= 80.0: # 5 mins
            # Merge sources and details
            if not candidate.reference and reference:
                candidate.reference = reference
            
            # Update source array representation (represented locally as string tags or source fields)
            # If CSV has cleaner merchant, update it
            if len(candidate.merchant) < len(merchant) or candidate.source == 'sms':
                candidate.merchant = merchant
                
            candidate.is_verified = True
            if 'csv' not in candidate.source and 'pdf' not in candidate.source:
                candidate.source = f"{candidate.source},csv"
            
            candidate.save()
            return {"status": "merged", "transaction": candidate}

    # Level 3: Weak Match (Same day, similar merchant, same amount)
    # Could be a duplicate or a distinct transaction, flag as a conflict / weak duplicate
    for candidate in candidates:
        is_same_day = candidate.date.date() == date.date()
        similarity = levenshtein_similarity(candidate.merchant, merchant)
        
        if is_same_day and similarity >= 70.0:
            # Register a conflict in the database
            conflict, created = Conflict.objects.get_or_create(
                user=user,
                sms_transaction=candidate,
                imported_amount=amount,
                imported_merchant=merchant,
                imported_date=date,
                imported_reference=reference
            )
            return {"status": "conflict", "conflict": conflict}

    # No match found: Insert as new verified transaction
    new_tx = Transaction.objects.create(
        user=user,
        amount=amount,
        type=tx_type,
        merchant=merchant,
        category='Other', # Categorization helper will determine category or default
        date=date,
        is_auto_detected=True,
        reference=reference,
        source='csv',
        is_verified=True
    )
    
    # Update balance
    profile = user.profile
    if tx_type == 'Debit':
        profile.current_balance -= amount
    else:
        profile.current_balance += amount
    profile.save()

    return {"status": "created", "transaction": new_tx}
