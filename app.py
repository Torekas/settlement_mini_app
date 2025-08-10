import os
import json
import io
from functools import wraps
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from pymongo import MongoClient
from bson.objectid import ObjectId
from collections import defaultdict
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import base64

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'C46ABA8FA628964A54B9D2B7A2E7Fo')

MONGODB_URI = os.getenv(
    'MONGODB_URI',
    "mongodb+srv://janmichalak78:W9DJ4jAcjjVXFYPp@cluster0.gk8zvxq.mongodb.net/settlement_db?retryWrites=true&w=majority"
)
client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = client['settlement_db']

# kolekcje
transactions_col = db['transactions']
users_col = db['users']
trips_col = db['trips']  # NEW

SUPPORTED_CURRENCIES = ["PLN", "EUR", "USD", "NOK", "GBP", "CZK", "CHF", "SEK", "DKK"]


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
        elif users_col.find_one({'username': username}):
            flash('Użytkownik już istnieje.', 'danger')
        else:
            pw_hash = generate_password_hash(password)
            users_col.insert_one({
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
        user = users_col.find_one({'username': username})
        if not user or not check_password_hash(user['password'], password):
            flash('Nieprawidłowe dane logowania.', 'danger')
        else:
            session['user_id'] = str(user['_id'])
            session['username'] = username
            ensure_active_trip()  # NEW - ustaw aktywny wyjazd
            flash(f'Witaj, {username}!', 'success')
            return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Wylogowano.', 'info')
    return redirect(url_for('login'))


def get_user():
    return users_col.find_one({'_id': ObjectId(session['user_id'])})


# --- WYJAZDY ---

def ensure_active_trip():
    """zapewnia, że istnieje aktywny wyjazd w sesji; jeśli nie, tworzy domyślny nienarchiwalny"""
    if session.get('active_trip_id'):
        t = trips_col.find_one({'_id': ObjectId(session['active_trip_id']), 'user_id': session['user_id']})
        if t and t.get('archived_at') is None:
            return t

    t = trips_col.find_one({'user_id': session['user_id'], 'archived_at': None}, sort=[('created_at', -1)])
    if not t:
        tid = trips_col.insert_one({
            'user_id': session['user_id'],
            'name': 'Domyślny wyjazd',
            'description': '',
            'archived_at': None,
            'created_at': datetime.utcnow(),
        }).inserted_id
        t = trips_col.find_one({'_id': tid})

    session['active_trip_id'] = str(t['_id'])
    return t


def list_trips(user_id):
    """zwraca (niearchiwalne, archiwalne) z polem _id_str dla wygody w szablonie"""
    trips = list(trips_col.find({'user_id': user_id}).sort('created_at', -1))
    for t in trips:
        t['_id_str'] = str(t['_id'])
    non_arch = [t for t in trips if t.get('archived_at') is None]
    arch = [t for t in trips if t.get('archived_at') is not None]
    return non_arch, arch


# --- TRANSAKCJE / LOGIKA ---

def fetch_transactions(user_id, trip_id=None):
    query = {'user_id': user_id}
    if trip_id:
        query['trip_id'] = trip_id
    docs = list(transactions_col.find(query))
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


@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    user = get_user()
    if not user:
        flash('Nie znaleziono użytkownika.', 'danger')
        return redirect(url_for('login'))

    # wyjazdy
    active_trip = ensure_active_trip()
    active_trip_id = session['active_trip_id']
    non_archived_trips, archived_trips = list_trips(session['user_id'])

    edit_id = None
    edit_tx = None

    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    download_currency = main_currency

    if request.method == 'POST':
        action = request.form.get('action')

        # --- WYJAZDY ---
        if action == 'create_trip':
            name = request.form.get('trip_name', '').strip() or f'Wyjazd {datetime.utcnow().date()}'
            desc = request.form.get('trip_desc', '').strip()
            tid = trips_col.insert_one({
                'user_id': session['user_id'],
                'name': name,
                'description': desc,
                'archived_at': None,
                'created_at': datetime.utcnow(),
            }).inserted_id
            session['active_trip_id'] = str(tid)
            flash(f'Utworzono i ustawiono wyjazd: {name}.', 'success')
            return redirect(url_for('index'))

        elif action == 'switch_trip':
            tid = request.form.get('trip_id')
            if tid and trips_col.find_one({'_id': ObjectId(tid), 'user_id': session['user_id'], 'archived_at': None}):
                session['active_trip_id'] = tid
                flash('Zmieniono aktywny wyjazd.', 'info')
            else:
                flash('Nie można przełączyć na ten wyjazd.', 'danger')
            return redirect(url_for('index'))

        elif action == 'archive_trip':
            tid = request.form.get('trip_id') or active_trip_id
            if tid:
                trips_col.update_one(
                    {'_id': ObjectId(tid), 'user_id': session['user_id']},
                    {'$set': {'archived_at': datetime.utcnow()}}
                )
                if session.get('active_trip_id') == tid:
                    session.pop('active_trip_id', None)
                    ensure_active_trip()
                flash('Wyjazd przeniesiono do archiwum.', 'warning')
            return redirect(url_for('index'))

        elif action == 'restore_trip':
            tid = request.form.get('trip_id')
            if tid:
                trips_col.update_one(
                    {'_id': ObjectId(tid), 'user_id': session['user_id']},
                    {'$set': {'archived_at': None}}
                )
                flash('Przywrócono wyjazd z archiwum.', 'success')
            return redirect(url_for('index'))

        # --- TRANSAKCJE / WALUTY ---
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
                    transactions_col.insert_one({
                        'user_id': session['user_id'],
                        'trip_id': session['active_trip_id'],  # NEW
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
                    transactions_col.update_one(
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
                transactions_col.delete_one({'_id': ObjectId(del_id)})
                flash('Usunięto transakcję.', 'info')
                return redirect(url_for('index'))
            else:
                flash('Brak id do usunięcia.', 'danger')

        elif action == 'set_main_currency':
            new_main = request.form.get('main_currency')
            if new_main and new_main in currency_rates:
                users_col.update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'main_currency': new_main}})
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
                    users_col.update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'currency_rates': currency_rates}})
                    flash(f'Ustawiono kurs {cur}: {val}', 'success')
                else:
                    flash("Nieprawidłowy kod waluty lub wartość.", 'danger')
            except Exception:
                flash("Nieprawidłowa wartość kursu.", 'danger')

        elif action == 'del_rate':
            cur = request.form.get('del_currency_code', '').upper()
            if cur in currency_rates and cur != main_currency:
                currency_rates.pop(cur)
                users_col.update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'currency_rates': currency_rates}})
                flash(f'Usunięto kurs waluty {cur}.', 'info')
            else:
                flash("Nie można usunąć tej waluty lub jest ona walutą główną.", 'danger')

        elif action == 'calculate':
            flash('Obliczono rozliczenie.', 'info')

        elif action == 'reset':
            transactions_col.delete_many({'user_id': session['user_id'], 'trip_id': session['active_trip_id']})
            flash('Wyczyszczono wszystkie transakcje tego wyjazdu.', 'warning')
            return redirect(url_for('index'))

        elif action == 'import':
            f = request.files.get('import_file')
            if f:
                try:
                    imported = json.loads(f.read().decode('utf-8'))
                    if isinstance(imported, list):
                        transactions_col.delete_many({'user_id': session['user_id'], 'trip_id': session['active_trip_id']})
                        for tx in imported:
                            tx['user_id'] = session['user_id']
                            tx['trip_id'] = session['active_trip_id']
                            if 'currency' not in tx:
                                tx['currency'] = main_currency
                            transactions_col.insert_one(tx)
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

    # odśwież dane po POST
    user = get_user()
    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    download_currency = request.form.get('download_currency', main_currency)

    # aktywny wyjazd i transakcje
    active_trip = ensure_active_trip()
    active_trip_id = session['active_trip_id']
    transactions = fetch_transactions(session['user_id'], active_trip_id)

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

    non_archived_trips, archived_trips = list_trips(session['user_id'])

    return render_template(
        'index.html',
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
        download_currency=download_currency,
        # wyjazdy
        non_archived_trips=non_archived_trips,
        archived_trips=archived_trips,
        active_trip=active_trip,
        active_trip_id=active_trip_id
    )


@app.route('/download', methods=['GET', 'POST'])
@login_required
def download_json():
    """pobranie JSON tylko dla aktywnego wyjazdu"""
    user = get_user()
    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})

    # akceptuj zarówno POST z formularza (download_currency), jak i GET ?currency=
    to_cur = request.values.get('download_currency') or request.args.get('currency') or main_currency

    docs = list(transactions_col.find({'user_id': session['user_id'], 'trip_id': session['active_trip_id']}))
    out = []
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
    """raport HTML tylko dla aktywnego wyjazdu"""
    user = get_user()
    main_currency = user.get('main_currency', 'PLN')
    currency_rates = user.get('currency_rates', {"PLN": 1.0})
    transactions = fetch_transactions(session['user_id'], session['active_trip_id'])
    matrix = compute_settlement_matrix(transactions, main_currency, currency_rates)
    detailed = get_detailed_settlement(transactions, main_currency, currency_rates)
    lodging = get_lodging_summary(transactions, main_currency, currency_rates)
    positives = sorted(p for p, d in detailed.items() if d['net'] > 0)
    negatives = sorted(p for p, d in detailed.items() if d['net'] < 0)

    for p, data in detailed.items():
        data['verb'] = get_gender_verb(p, data['paid'])

    # wykres słupkowy (saldo)
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

    # wykres kołowy (noclegi)
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
