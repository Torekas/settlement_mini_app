import os
import json
import io
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_file, session
from pymongo import MongoClient
from bson.objectid import ObjectId
from collections import defaultdict
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# Jeśli korzystasz z .env, załaduj go:
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
# URI do MongoDB – najlepiej trzymać w zmiennej środowiskowej
MONGODB_URI = os.getenv(
    'MONGODB_URI'
)

client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
try:
    client.admin.command('ping')
    app.logger.info("✅ Połączono z MongoDB Atlas")
except Exception as e:
    app.logger.error(f"❌ Błąd połączenia z MongoDB: {e}")
db = client['settlement_db']
transactions_col = db['transactions']
users_col = db['users']

# Decorator to protect routes
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# Authentication routes
@app.route('/register', methods=['GET', 'POST'])
def register():
    errors = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            errors = 'Nazwa użytkownika i hasło są wymagane.'
        elif users_col.find_one({'username': username}):
            errors = 'Użytkownik już istnieje.'
        else:
            pw_hash = generate_password_hash(password)
            users_col.insert_one({'username': username, 'password': pw_hash})
            return redirect(url_for('login'))
    return render_template('register.html', errors=errors)

@app.route('/login', methods=['GET', 'POST'])
def login():
    errors = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = users_col.find_one({'username': username})
        if not user or not check_password_hash(user['password'], password):
            errors = 'Nieprawidłowe dane logowania.'
        else:
            session['user_id'] = str(user['_id'])
            session['username'] = username
            return redirect(url_for('index'))
    return render_template('login.html', errors=errors)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# Helper to fetch transactions
def fetch_transactions():
    docs = list(transactions_col.find())
    txs = []
    for d in docs:
        txs.append({
            'id': str(d['_id']),
            'payer': d.get('payer', ''),
            'amount': d.get('amount', 0),
            'beneficiaries': d.get('beneficiaries', []),
            'description': d.get('description', '')
        })
    return txs

# Settlement functions unchanged
# ... [compute_settlement_matrix, get_detailed_settlement, get_lodging_summary] ...

def compute_settlement_matrix(transactions):
    people = set()
    for item in transactions:
        people.add(item["payer"])
        for b in item["beneficiaries"]:
            people.add(b)
    people = list(people)

    paid = defaultdict(float)
    owes = defaultdict(float)
    for item in transactions:
        amt = float(item.get("amount", 0))
        payer = item["payer"]
        bens = item.get("beneficiaries", [])
        if not bens:
            continue
        share = amt / len(bens)
        paid[payer] += amt
        for b in bens:
            owes[b] += share

    net = {p: paid[p] - owes[p] for p in people}
    positives = {p: net[p] for p in people if net[p] > 1e-6}
    negatives = {p: -net[p] for p in people if net[p] < -1e-6}
    total_positive = sum(positives.values())

    matrix = {}
    for deb, owe_amt in negatives.items():
        matrix[deb] = {}
        for cred, cred_amt in positives.items():
            share = (owe_amt * (cred_amt / total_positive)) if total_positive else 0
            matrix[deb][cred] = round(share, 2)
    return matrix


def get_detailed_settlement(transactions):
    paid = defaultdict(float)
    owes = defaultdict(float)
    people = set()
    for t in transactions:
        payer = t.get('payer')
        amount = float(t.get('amount', 0))
        bens = t.get('beneficiaries', [])
        people.add(payer)
        for b in bens:
            people.add(b)
        share = amount / len(bens) if bens else 0
        paid[payer] += amount
        for b in bens:
            owes[b] += share
    detailed = {}
    for p in people:
        detailed[p] = {
            'paid': round(paid[p], 2),
            'owes': round(owes[p], 2),
            'net': round(paid[p] - owes[p], 2)
        }
    return detailed


def get_lodging_summary(transactions):
    summary = {}
    for tx in transactions:
        if 'nocleg' in tx.get('description', '').lower():
            amount = float(tx.get('amount', 0))
            bens = tx.get('beneficiaries', [])
            if bens:
                share = amount / len(bens)
                for b in bens:
                    summary[b] = summary.get(b, 0) + share
    return {k: round(v, 2) for k, v in summary.items()}

# Main route
@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    settlement_matrix = None
    errors = None
    edit_id = None
    edit_tx = None

    if request.method == 'POST':
        action = request.form.get('action')

        # 1) Dodaj
        if action == 'add':
            payer      = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip()
            bens_str   = request.form.get('beneficiaries', '').strip()
            desc       = request.form.get('description', '').strip()

            if not payer or not amount_str or not bens_str:
                errors = 'Płatnik, kwota i beneficjenci są wymagane.'
            else:
                try:
                    amount = float(amount_str.replace(',', '.'))
                    bens   = [b.strip() for b in bens_str.split(',') if b.strip()]
                    transactions_col.insert_one({
                        'payer': payer,
                        'amount': amount,
                        'beneficiaries': bens,
                        'description': desc
                    })
                    return redirect(url_for('index'))
                except ValueError:
                    errors = 'Nieprawidłowa kwota.'

        # 2) Wczytaj do edycji
        elif action == 'edit':
            edit_id = request.form.get('id')
            if edit_id:
                doc = transactions_col.find_one({'_id': ObjectId(edit_id)})
                if doc:
                    edit_tx = {
                        'id': edit_id,
                        'payer': doc.get('payer', ''),
                        'amount': doc.get('amount', ''),
                        'beneficiaries': ', '.join(doc.get('beneficiaries', [])),
                        'description': doc.get('description', '')
                    }
                else:
                    errors = 'Nie znaleziono transakcji.'

        # 3) Zapisz zmiany
        elif action == 'update':
            edit_id    = request.form.get('id')
            payer      = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip()
            bens_str   = request.form.get('beneficiaries', '').strip()
            desc       = request.form.get('description', '').strip()

            if not edit_id or not payer or not amount_str or not bens_str:
                errors = 'Wszystkie pola są wymagane.'
            else:
                try:
                    amount = float(amount_str.replace(',', '.'))
                    bens   = [b.strip() for b in bens_str.split(',') if b.strip()]
                    transactions_col.update_one(
                        {'_id': ObjectId(edit_id)},
                        {'$set': {
                            'payer': payer,
                            'amount': amount,
                            'beneficiaries': bens,
                            'description': desc
                        }}
                    )
                    return redirect(url_for('index'))
                except Exception:
                    errors = 'Błąd podczas aktualizacji.'

        # 4) Usuń
        elif action == 'delete':
            del_id = request.form.get('id')
            if del_id:
                transactions_col.delete_one({'_id': ObjectId(del_id)})
                return redirect(url_for('index'))
            else:
                errors = 'Brak id do usunięcia.'

        # 5) Oblicz
        elif action == 'calculate':
            data = fetch_transactions()
            data_only = [{k: v for k, v in tx.items() if k != 'id'} for tx in data]
            settlement_matrix = compute_settlement_matrix(data_only)

        # 6) Resetuj
        elif action == 'reset':
            transactions_col.delete_many({})
            return redirect(url_for('index'))

        # 7) Import
        elif action == 'import':
            f = request.files.get('import_file')
            if f:
                try:
                    imported = json.loads(f.read().decode('utf-8'))
                    if isinstance(imported, list):
                        transactions_col.delete_many({})
                        for tx in imported:
                            transactions_col.insert_one(tx)
                        return redirect(url_for('index'))
                    else:
                        errors = 'Nieprawidłowy format JSON.'
                except Exception as e:
                    errors = f'Błąd importu: {e}'
            else:
                errors = 'Brak pliku do importu.'

    # pobierz transakcje i renderuj
    transactions = fetch_transactions()
    detailed     = get_detailed_settlement(transactions)
    lodging      = get_lodging_summary(transactions)
    positives    = sorted(p for p,d in detailed.items() if d['net']>0)
    negatives    = sorted(p for p,d in detailed.items() if d['net']<0)

    return render_template('index.html',
        transactions=transactions,
        settlement_matrix=settlement_matrix,
        detailed_settlement=detailed,
        lodging_summary=lodging,
        positives=positives,
        negatives=negatives,
        errors=errors,
        edit_id=edit_id,
        edit_transaction=edit_tx
    )

@app.route('/download', methods=['GET'])
@login_required
def download():
    docs = list(transactions_col.find())
    out = []
    for d in docs:
        out.append({
            'payer': d.get('payer', ''),
            'amount': d.get('amount', 0),
            'beneficiaries': d.get('beneficiaries', []),
            'description': d.get('description', '')
        })
    buf = io.BytesIO(json.dumps(out, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/json',
        as_attachment=True,
        download_name='transactions.json'
    )

if __name__ == '__main__':
    app.run(debug=True)
