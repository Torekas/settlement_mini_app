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
import base64
from datetime import datetime

# import matplotlib.pyplot as plt

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'C46ABA8FA628964A54B9D2B7A2E7Fo')
MONGODB_URI = os.getenv(
    'MONGODB_URI',
    "mongodb+srv://janmichalak78:W9DJ4jAcjjVXFYPp@cluster0.gk8zvxq.mongodb.net/settlement_db?retryWrites=true&w=majority"
)

SUPPORTED_CURRENCIES = ["PLN", "EUR", "USD", "NOK", "GBP", "CZK", "CHF", "SEK", "DKK"]

def get_db():
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    db = client['settlement_db']
    return db

def get_transactions_col():
    return get_db()['transactions']

def get_users_col():
    return get_db()['users']

def get_trips_col():
    return get_db()['trips']

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Nazwa użytkownika i hasło są wymagane.', 'danger')
        elif get_users_col().find_one({'username': username}):
            flash('Użytkownik już istnieje.', 'danger')
        else:
            pw_hash = generate_password_hash(password)
            get_users_col().insert_one({
                'username': username,
                'password': pw_hash,
                'main_currency': "PLN",
                'currency_rates': {"PLN": 1.0, "EUR": 4.0, "USD": 3.8}
            })
            flash('Rejestracja zakończona sukcesem! Zaloguj się.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = get_users_col().find_one({'username': username})
        if not user or not check_password_hash(user['password'], password):
            flash('Nieprawidłowe dane logowania.', 'danger')
        else:
            session['user_id'] = str(user['_id'])
            session['username'] = username
            flash(f'Witaj, {username}!', 'success')
            # Zmień na index lub archive_list, jak chcesz!
            return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Wylogowano.', 'info')
    return redirect(url_for('login'))

def get_user():
    return get_users_col().find_one({'_id': ObjectId(session['user_id'])})

def fetch_transactions(user_id):
    docs = list(get_transactions_col().find({'user_id': user_id}))
    txs = []
    for d in docs:
        txs.append({
            'id': str(d['_id']),
            'payer': d.get('payer', ''),
            'amount': d.get('amount', 0),
            'currency': d.get('currency', 'PLN'),
            'beneficiaries': d.get('beneficiaries', []),
            'description': d.get('description', '')
        })
    return txs

def get_gender_verb(name, amount):
    if name.strip().lower().endswith('a') and not name.strip().lower().endswith('ba'):
        return 'zapłaciła'
    else:
        return 'zapłacił'

def convert(amount, from_cur, to_cur, rates):
    if from_cur == to_cur:
        return amount
    if from_cur not in rates or to_cur not in rates:
        raise Exception(f"Brak kursu dla {from_cur} lub {to_cur}")
    return amount * rates[from_cur] / rates[to_cur]

def compute_settlement_matrix(transactions, main_currency, rates):
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
        tx_cur = item.get("currency", main_currency)
        amt_conv = convert(amt, tx_cur, main_currency, rates)
        if not bens:
            continue
        share = amt_conv / len(bens)
        paid[payer] += amt_conv
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

def get_detailed_settlement(transactions, main_currency, rates):
    paid = defaultdict(float)
    owes = defaultdict(float)
    people = set()
    for t in transactions:
        payer = t.get('payer')
        amount = float(t.get('amount', 0))
        tx_cur = t.get('currency', main_currency)
        amount_conv = convert(amount, tx_cur, main_currency, rates)
        bens = t.get('beneficiaries', [])
        people.add(payer)
        for b in bens:
            people.add(b)
        share = amount_conv / len(bens) if bens else 0
        paid[payer] += amount_conv
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

def get_lodging_summary(transactions, main_currency, rates):
    summary = {}
    for tx in transactions:
        if 'nocleg' in tx.get('description', '').lower():
            amount = float(tx.get('amount', 0))
            tx_cur = tx.get('currency', main_currency)
            amount_conv = convert(amount, tx_cur, main_currency, rates)
            bens = tx.get('beneficiaries', [])
            if bens:
                share = amount_conv / len(bens)
                for b in bens:
                    summary[b] = summary.get(b, 0) + share
    return {k: round(v, 2) for k, v in summary.items()}

@app.route('/archive-trip', methods=['POST'])
@login_required
def archive_trip():
    user = get_user()
    if not user:
        flash('Błąd użytkownika.', 'danger')
        return redirect(url_for('index'))

    trip_name = request.form.get('trip_name', '').strip()
    trip_desc = request.form.get('trip_desc', '').strip()
    if not trip_name:
        flash('Nazwa wycieczki jest wymagana!', 'danger')
        return redirect(url_for('index'))

    transactions = fetch_transactions(session['user_id'])
    if not transactions:
        flash('Brak transakcji do archiwizacji.', 'danger')
        return redirect(url_for('index'))

    archive_doc = {
        "user_id": session['user_id'],
        "name": trip_name,
        "description": trip_desc,
        "archived_at": datetime.utcnow(),
        "transactions": transactions,
        "main_currency": user.get('main_currency', 'PLN'),
        "currency_rates": user.get('currency_rates', {"PLN": 1.0}),
    }
    get_trips_col().insert_one(archive_doc)
    get_transactions_col().delete_many({'user_id': session['user_id']})
    flash(f'Wycieczka „{trip_name}” została przeniesiona do archiwum.', 'success')
    return redirect(url_for('archive_list'))

@app.route('/archive')
@login_required
def archive_list():
    user_id = session['user_id']
    trips = list(get_trips_col().find({'user_id': user_id}))
    return render_template('archive.html', trips=trips)

@app.route('/archive/<trip_id>')
@login_required
def archive_view(trip_id):
    trip = get_trips_col().find_one({'_id': ObjectId(trip_id)})
    if not trip or trip['user_id'] != session['user_id']:
        flash('Nie znaleziono wycieczki.', 'danger')
        return redirect(url_for('archive_list'))
    transactions = trip['transactions']
    main_currency = trip.get('main_currency', 'PLN')
    currency_rates = trip.get('currency_rates', {"PLN": 1.0})
    settlement_matrix = compute_settlement_matrix(transactions, main_currency, currency_rates)
    detailed = get_detailed_settlement(transactions, main_currency, currency_rates)
    lodging = get_lodging_summary(transactions, main_currency, currency_rates)
    positives = sorted(p for p, d in detailed.items() if d['net'] > 0)
    negatives = sorted(p for p, d in detailed.items() if d['net'] < 0)
    return render_template('archive_view.html',
        trip=trip,
        transactions=transactions,
        settlement_matrix=settlement_matrix,
        detailed_settlement=detailed,
        lodging_summary=lodging,
        positives=positives,
        negatives=negatives,
        main_currency=main_currency,
        currency_rates=currency_rates,
    )

@app.route('/archive/<trip_id>/delete', methods=['POST'])
@login_required
def archive_delete(trip_id):
    trip = get_trips_col().find_one({'_id': ObjectId(trip_id)})
    if not trip or trip['user_id'] != session['user_id']:
        flash('Brak dostępu.', 'danger')
        return redirect(url_for('archive_list'))
    get_trips_col().delete_one({'_id': ObjectId(trip_id)})
    flash('Wycieczka została usunięta z archiwum.', 'success')
    return redirect(url_for('archive_list'))

@app.route('/archive/<trip_id>/edit', methods=['GET', 'POST'])
@login_required
def archive_edit(trip_id):
    trip = get_trips_col().find_one({'_id': ObjectId(trip_id)})
    if not trip or trip['user_id'] != session['user_id']:
        flash('Brak dostępu.', 'danger')
        return redirect(url_for('archive_list'))
    if request.method == 'POST':
        new_name = request.form.get('trip_name', '').strip()
        new_desc = request.form.get('trip_desc', '').strip()
        if not new_name:
            flash('Nazwa wycieczki jest wymagana!', 'danger')
            return redirect(url_for('archive_edit', trip_id=trip_id))
        get_trips_col().update_one({'_id': ObjectId(trip_id)}, {'$set': {'name': new_name, 'description': new_desc}})
        flash('Zaktualizowano dane wycieczki.', 'success')
        return redirect(url_for('archive_view', trip_id=trip_id))
    return render_template('archive_edit.html', trip=trip)

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    user = get_user()
    if not user:
        flash('Nie znaleziono użytkownika.', 'danger')
        return redirect(url_for('login'))

    edit_id = None
    edit_tx = None

    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    download_currency = main_currency

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            payer = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip()
            bens_str = request.form.get('beneficiaries', '').strip()
            desc = request.form.get('description', '').strip()
            tx_cur = request.form.get('currency', main_currency)
            if not payer or not amount_str or not bens_str or not tx_cur:
                flash('Płatnik, kwota, beneficjenci i waluta są wymagane.', 'danger')
            else:
                try:
                    amount = float(amount_str.replace(',', '.'))
                    bens = [b.strip() for b in bens_str.split(',') if b.strip()]
                    get_transactions_col().insert_one({
                        'user_id': session['user_id'],
                        'payer': payer,
                        'amount': amount,
                        'currency': tx_cur,
                        'beneficiaries': bens,
                        'description': desc
                    })
                    flash('Dodano transakcję!', 'success')
                    return redirect(url_for('index'))
                except Exception:
                    flash('Nieprawidłowa kwota.', 'danger')
        elif action == 'edit':
            edit_id = request.form.get('id')
            if edit_id:
                doc = get_transactions_col().find_one({'_id': ObjectId(edit_id)})
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
                    flash('Nie znaleziono transakcji.', 'danger')
        elif action == 'update':
            edit_id = request.form.get('id')
            payer = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip()
            bens_str = request.form.get('beneficiaries', '').strip()
            desc = request.form.get('description', '').strip()
            tx_cur = request.form.get('currency', main_currency)
            if not edit_id or not payer or not amount_str or not bens_str or not tx_cur:
                flash('Wszystkie pola są wymagane.', 'danger')
            else:
                try:
                    amount = float(amount_str.replace(',', '.'))
                    bens = [b.strip() for b in bens_str.split(',') if b.strip()]
                    get_transactions_col().update_one(
                        {'_id': ObjectId(edit_id)},
                        {'$set': {
                            'payer': payer,
                            'amount': amount,
                            'currency': tx_cur,
                            'beneficiaries': bens,
                            'description': desc
                        }}
                    )
                    flash('Zaktualizowano transakcję.', 'success')
                    return redirect(url_for('index'))
                except Exception:
                    flash('Błąd podczas aktualizacji.', 'danger')
        elif action == 'delete':
            del_id = request.form.get('id')
            if del_id:
                get_transactions_col().delete_one({'_id': ObjectId(del_id)})
                flash('Usunięto transakcję.', 'info')
                return redirect(url_for('index'))
            else:
                flash('Brak id do usunięcia.', 'danger')
        elif action == 'set_main_currency':
            new_main = request.form.get('main_currency')
            if new_main and new_main in currency_rates:
                get_users_col().update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'main_currency': new_main}})
                flash(f'Zmieniono walutę główną na {new_main}.', 'success')
                main_currency = new_main
            else:
                flash("Brak kursu dla tej waluty – najpierw ustaw kurs!", 'danger')
        elif action == 'set_rate':
            cur = request.form.get('currency_code', '').upper()
            val = request.form.get('currency_value', '')
            try:
                val = float(val.replace(',', '.'))
                if cur and val > 0:
                    currency_rates[cur] = val
                    get_users_col().update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'currency_rates': currency_rates}})
                    flash(f'Ustawiono kurs {cur}: {val}', 'success')
                else:
                    flash("Nieprawidłowy kod waluty lub wartość.", 'danger')
            except Exception:
                flash("Nieprawidłowa wartość kursu.", 'danger')
        elif action == 'del_rate':
            cur = request.form.get('del_currency_code', '').upper()
            if cur in currency_rates and cur != main_currency:
                currency_rates.pop(cur)
                get_users_col().update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'currency_rates': currency_rates}})
                flash(f'Usunięto kurs waluty {cur}.', 'info')
            else:
                flash("Nie można usunąć tej waluty lub jest ona walutą główną.", 'danger')
        elif action == 'calculate':
            flash('Obliczono rozliczenie.', 'info')
        elif action == 'reset':
            get_transactions_col().delete_many({'user_id': session['user_id']})
            flash('Wyczyszczono wszystkie transakcje.', 'warning')
            return redirect(url_for('index'))
        elif action == 'import':
            f = request.files.get('import_file')
            if f:
                try:
                    imported = json.loads(f.read().decode('utf-8'))
                    if isinstance(imported, list):
                        get_transactions_col().delete_many({'user_id': session['user_id']})
                        for tx in imported:
                            tx['user_id'] = session['user_id']
                            if 'currency' not in tx:
                                tx['currency'] = main_currency
                            get_transactions_col().insert_one(tx)
                        flash('Zaimportowano transakcje.', 'success')
                        return redirect(url_for('index'))
                    else:
                        flash('Nieprawidłowy format JSON.', 'danger')
                except Exception as e:
                    flash(f'Błąd importu: {e}', 'danger')
            else:
                flash('Brak pliku do importu.', 'danger')
        elif action == 'set_download_currency':
            download_currency = request.form.get('download_currency', main_currency)

    user = get_user()
    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    download_currency = request.form.get('download_currency', main_currency)

    transactions = fetch_transactions(session['user_id'])
    try:
        settlement_matrix = compute_settlement_matrix(transactions, main_currency, currency_rates)
        detailed = get_detailed_settlement(transactions, main_currency, currency_rates)
        lodging = get_lodging_summary(transactions, main_currency, currency_rates)
        positives = sorted(p for p, d in detailed.items() if d['net'] > 0)
        negatives = sorted(p for p, d in detailed.items() if d['net'] < 0)
    except Exception as e:
        flash(str(e), 'danger')
        settlement_matrix = None
        detailed = {}
        lodging = {}
        positives = []
        negatives = []

    available_currencies = list(currency_rates.keys())
    download_currency = download_currency if download_currency in available_currencies else main_currency

    return render_template('index.html',
        transactions=transactions,
        settlement_matrix=settlement_matrix,
        detailed_settlement=detailed,
        lodging_summary=lodging,
        positives=positives,
        negatives=negatives,
        edit_id=edit_id,
        edit_transaction=edit_tx,
        main_currency=main_currency,
        currency_rates=currency_rates,
        supported_currencies=SUPPORTED_CURRENCIES,
        available_currencies=available_currencies,
        download_currency=download_currency
    )

@app.route('/download', methods=['GET', 'POST'])
@login_required
def download_json():
    user = get_user()
    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    docs = list(get_transactions_col().find({'user_id': session['user_id']}))
    out = []
    to_cur = request.args.get('currency') or main_currency
    for d in docs:
        amt = float(d.get('amount', 0))
        from_cur = d.get('currency', main_currency)
        try:
            conv_amt = convert(amt, from_cur, to_cur, currency_rates)
        except Exception:
            conv_amt = amt
        out.append({
            'payer': d.get('payer', ''),
            'amount': round(conv_amt, 2),
            'currency': to_cur,
            'beneficiaries': d.get('beneficiaries', []),
            'description': d.get('description', '')
        })
    buf = io.BytesIO(json.dumps(out, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    flash(f'Pobrano dane w walucie {to_cur}.', 'info')
    return send_file(
        buf,
        mimetype='application/json',
        as_attachment=True,
        download_name=f'transactions_{to_cur}.json'
    )

@app.route('/download-report', methods=['GET'])
@login_required
def download_report():
    user = get_user()
    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    transactions = fetch_transactions(session['user_id'])
    matrix = compute_settlement_matrix(transactions, main_currency, currency_rates)
    detailed = get_detailed_settlement(transactions, main_currency, currency_rates)
    lodging = get_lodging_summary(transactions, main_currency, currency_rates)
    positives = sorted(p for p, d in detailed.items() if d['net'] > 0)
    negatives = sorted(p for p, d in detailed.items() if d['net'] < 0)

    for p, data in detailed.items():
        data['verb'] = get_gender_verb(p, data['paid'])

    fig, ax = plt.subplots()
    names = list(detailed.keys())
    nets = [detailed[p]['net'] for p in names]
    bars = ax.bar(names, nets)
    ax.set_title('Saldo netto uczestników')
    ax.set_ylabel(main_currency)
    for bar, net in zip(bars, nets):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(), f'{net:.2f}', ha='center', va='bottom')
    plt.tight_layout()
    buf1 = io.BytesIO()
    fig.savefig(buf1, format='png')
    plt.close(fig)
    buf1.seek(0)
    detailed_chart = base64.b64encode(buf1.read()).decode('utf-8')

    if lodging:
        fig2, ax2 = plt.subplots()
        names2 = list(lodging.keys())
        vals2 = list(lodging.values())
        ax2.pie(vals2, labels=names2, autopct='%1.1f%%')
        ax2.set_title('Udział kosztów noclegów')
        plt.tight_layout()
        buf2 = io.BytesIO()
        fig2.savefig(buf2, format='png')
        plt.close(fig2)
        buf2.seek(0)
        lodging_chart = base64.b64encode(buf2.read()).decode('utf-8')
    else:
        lodging_chart = None

    rendered = render_template(
        'report.html', matrix=matrix, detailed=detailed,
        lodging=lodging, positives=positives,
        negatives=negatives, main_currency=main_currency,
        detailed_chart=detailed_chart, lodging_chart=lodging_chart
    )
    buf = io.BytesIO(rendered.encode('utf-8'))
    buf.seek(0)
    flash('Pobrano raport HTML z podsumowaniami.', 'info')
    return send_file(
        buf, mimetype='text/html', as_attachment=True,
        download_name=f'report_{main_currency}.html'
    )

if __name__ == '__main__':
    app.run(debug=True)
