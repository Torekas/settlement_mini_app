from currex import Currency

import os
import json
import io
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from pymongo import MongoClient
from bson.objectid import ObjectId
from collections import defaultdict
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# If using .env, load it
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'C46ABA8FA628964A54B9D2B7A2E7Fo')

MONGODB_URI = os.getenv(
    'MONGODB_URI',
    "mongodb+srv://janmichalak78:W9DJ4jAcjjVXFYPp@cluster0.gk8zvxq.mongodb.net/settlement_db?retryWrites=true&w=majority"
)

client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = client['settlement_db']
transactions_col = db['transactions']
users_col = db['users']

CURRENCIES = ['PLN', 'EUR', 'USD', 'NOK', 'SEK', 'GBP', 'CHF', 'CZK']

def convert(amount, from_cur, to_cur):
    try:
        if from_cur == to_cur:
            return float(amount)
        money = Currency(from_cur, float(amount))
        converted = money.to(to_cur)
        return float(converted.amount)
    except Exception:
        return float(amount) if from_cur == to_cur else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

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
            flash('Rejestracja zakończona sukcesem. Możesz się zalogować.', 'success')
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
            if 'main_currency' not in session:
                session['main_currency'] = 'PLN'
            flash('Zalogowano pomyślnie.', 'success')
            return redirect(url_for('index'))
    return render_template('login.html', errors=errors)

@app.route('/logout')
def logout():
    session.clear()
    flash('Wylogowano pomyślnie.', 'info')
    return redirect(url_for('login'))

def fetch_transactions(main_currency=None):
    docs = list(transactions_col.find())
    txs = []
    for d in docs:
        amount = float(d.get('amount', 0))
        currency = d.get('currency', 'PLN')
        if not main_currency:
            main_currency = session.get('main_currency', 'PLN')
        converted = convert(amount, currency, main_currency)
        beneficiaries = d.get('beneficiaries', [])
        if isinstance(beneficiaries, str):
            beneficiaries = [b.strip() for b in beneficiaries.split(',') if b.strip()]
        txs.append({
            'id': str(d['_id']),
            'payer': d.get('payer', ''),
            'amount': amount,
            'currency': currency,
            'amount_converted': round(converted if converted is not None else 0.0, 2),
            'beneficiaries': ', '.join(beneficiaries),
            'beneficiaries_raw': beneficiaries,
            'description': d.get('description', '')
        })
    return txs

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

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    settlement_matrix = None
    errors = None
    edit_id = None
    edit_tx = None
    if 'main_currency' not in session:
        session['main_currency'] = 'PLN'
    main_currency = session['main_currency']
    currencies = CURRENCIES
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'set_main_currency':
            new_main = request.form.get('main_currency')
            if new_main in currencies:
                session['main_currency'] = new_main
                flash(f'Ustawiono walutę główną na {new_main}.', 'info')
                return redirect(url_for('index'))
        if action == 'add':
            payer      = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip().replace(',', '.')
            currency   = request.form.get('currency', main_currency)
            bens_str   = request.form.get('beneficiaries', '').strip()
            desc       = request.form.get('description', '').strip()
            if not payer or not amount_str or not bens_str or not currency:
                errors = 'Płatnik, kwota, waluta i beneficjenci są wymagane.'
            else:
                try:
                    amount = float(amount_str)
                    bens   = [b.strip() for b in bens_str.split(',') if b.strip()]
                    transactions_col.insert_one({
                        'payer': payer,
                        'amount': amount,
                        'currency': currency,
                        'beneficiaries': bens,
                        'description': desc
                    })
                    flash('Transakcja została dodana.', 'success')
                    return redirect(url_for('index'))
                except ValueError:
                    errors = 'Nieprawidłowa kwota.'
        elif action == 'edit':
            edit_id = request.form.get('id')
            if edit_id:
                doc = transactions_col.find_one({'_id': ObjectId(edit_id)})
                if doc:
                    edit_tx = {
                        'id': edit_id,
                        'payer': doc.get('payer', ''),
                        'amount': doc.get('amount', ''),
                        'currency': doc.get('currency', main_currency),
                        'beneficiaries': ', '.join(doc.get('beneficiaries', [])),
                        'description': doc.get('description', '')
                    }
                else:
                    errors = 'Nie znaleziono transakcji.'
        elif action == 'update':
            edit_id    = request.form.get('id')
            payer      = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip().replace(',', '.')
            currency   = request.form.get('currency', main_currency)
            bens_str   = request.form.get('beneficiaries', '').strip()
            desc       = request.form.get('description', '').strip()
            if not edit_id or not payer or not amount_str or not bens_str or not currency:
                errors = 'Wszystkie pola są wymagane.'
            else:
                try:
                    amount = float(amount_str)
                    bens   = [b.strip() for b in bens_str.split(',') if b.strip()]
                    transactions_col.update_one(
                        {'_id': ObjectId(edit_id)},
                        {'$set': {
                            'payer': payer,
                            'amount': amount,
                            'currency': currency,
                            'beneficiaries': bens,
                            'description': desc
                        }}
                    )
                    flash('Transakcja została zaktualizowana.', 'success')
                    return redirect(url_for('index'))
                except Exception:
                    errors = 'Błąd podczas aktualizacji.'
        elif action == 'delete':
            del_id = request.form.get('id')
            if del_id:
                transactions_col.delete_one({'_id': ObjectId(del_id)})
                flash('Transakcja została usunięta.', 'warning')
                return redirect(url_for('index'))
            else:
                errors = 'Brak id do usunięcia.'
        elif action == 'calculate':
            txs = fetch_transactions(main_currency)
            data_only = []
            for tx in txs:
                data_only.append({
                    'payer': tx['payer'],
                    'amount': tx['amount_converted'],
                    'beneficiaries': tx['beneficiaries_raw'],
                    'description': tx['description']
                })
            settlement_matrix = compute_settlement_matrix(data_only)
            flash('Obliczono rozliczenie.', 'info')
        elif action == 'reset':
            transactions_col.delete_many({})
            flash('Wyczyszczono wszystkie transakcje.', 'danger')
            return redirect(url_for('index'))
        elif action == 'import':
            f = request.files.get('import_file')
            if f:
                try:
                    imported = json.loads(f.read().decode('utf-8'))
                    if isinstance(imported, list):
                        transactions_col.delete_many({})
                        for tx in imported:
                            if 'currency' not in tx or tx['currency'] not in currencies:
                                tx['currency'] = main_currency
                            transactions_col.insert_one(tx)
                        flash('Zaimportowano plik transakcji.', 'success')
                        return redirect(url_for('index'))
                    else:
                        errors = 'Nieprawidłowy format JSON.'
                except Exception as e:
                    errors = f'Błąd importu: {e}'
            else:
                errors = 'Brak pliku do importu.'
    transactions = fetch_transactions(main_currency)
    data_for_calc = []
    for tx in transactions:
        data_for_calc.append({
            'payer': tx['payer'],
            'amount': tx['amount_converted'],
            'beneficiaries': [b.strip() for b in tx['beneficiaries'].split(',') if b.strip()],
            'description': tx['description']
        })
    detailed     = get_detailed_settlement(data_for_calc)
    lodging      = get_lodging_summary(data_for_calc)
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
        edit_transaction=edit_tx,
        currencies=currencies,
        main_currency=main_currency
    )

@app.route('/download', methods=['GET'])
@login_required
def download():
    currencies = CURRENCIES
    download_currency = request.args.get('download_currency') or session.get('main_currency', 'PLN')
    if download_currency not in currencies:
        download_currency = 'PLN'
    txs = fetch_transactions(download_currency)
    out = []
    for tx in txs:
        out.append({
            'payer': tx['payer'],
            'amount': tx['amount_converted'],
            'currency': download_currency,
            'beneficiaries': [b.strip() for b in tx['beneficiaries'].split(',') if b.strip()],
            'description': tx['description']
        })
    filename = f'transactions_{download_currency}.json'
    buf = io.BytesIO(json.dumps(out, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    flash(f'Pobrano plik rozliczenia w walucie {download_currency}.', 'info')
    return send_file(
        buf,
        mimetype='application/json',
        as_attachment=True,
        download_name=filename
    )

if __name__ == '__main__':
    app.run(debug=True)
