import json
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, send_file
import io

app = Flask(__name__)
app.secret_key = 'tajny_klucz'  # Ustaw swój własny, bezpieczny klucz


def init_session():
    if 'transactions' not in session:
        session['transactions'] = []


def compute_settlement_matrix(transactions):
    """
    Oblicza macierz rozliczeń – porównanie każdego dłużnika (osoba z ujemnym saldem)
    z każdym kredytorem (osoba z dodatnim saldem) według poniższej logiki:
      1. Wyznacz zbiór osób na podstawie płatników i beneficjentów.
      2. Dla każdej transakcji kwota dzielona jest równomiernie między beneficjentów.
      3. Obliczamy dla każdej osoby:
             paid  – suma kwot, które zapłaciła (jako płatnik),
             owes  – suma udziałów wynikających z bycia beneficjentem.
         Net = paid − owes.
      4. Dla osób z dodatnim net (kredytorzy) oraz ujemnym net (dłużnicy) wyznaczamy:
         – całkowitą dodatnią sumę (total_positive).
         – dla każdego dłużnika, kwota do spłaty zostaje rozdzielona proporcjonalnie do wkładu każdego kredytora.

    Zwracamy macierz w postaci słownika:
         { dłużnik: { kredytor: kwota, ... } }
    """
    # Wyznacz zbiór osób
    people = set()
    for item in transactions:
        people.add(item["payer"])
        for b in item["beneficiaries"]:
            people.add(b)
    people = list(people)

    paid = defaultdict(float)
    owes = defaultdict(float)

    for item in transactions:
        try:
            amt = float(item["amount"])
        except ValueError:
            amt = 0.0
        payer = item["payer"]
        beneficiaries = item["beneficiaries"]
        n = len(beneficiaries)
        if n == 0:
            continue
        share = amt / n
        paid[payer] += amt
        for b in beneficiaries:
            owes[b] += share

    net = {}
    for p in people:
        net[p] = paid[p] - owes[p]

    positives = {}
    negatives = {}
    for p in people:
        if net[p] > 1e-6:
            positives[p] = net[p]
        elif net[p] < -1e-6:
            negatives[p] = -net[p]
    total_positive = sum(positives.values())
    matrix = {}
    for neg in negatives:
        matrix[neg] = {}
        for pos in positives:
            if total_positive != 0:
                # Każdy dłużnik rozdziela swoją całkowitą kwotę zadłużenia proporcjonalnie
                matrix[neg][pos] = round(negatives[neg] * (positives[pos] / total_positive), 2)
            else:
                matrix[neg][pos] = 0.0
    return matrix


def get_detailed_settlement(transactions):
    """
    Oblicza szczegółowe podsumowanie kosztów dla każdej osoby:
      - "paid": suma wpłat jako płatnik,
      - "owes": suma udziałów (gdy osoba jest beneficjentem),
      - "net": różnica (paid – owes).
    Zwraca słownik: { osoba: { "paid": X, "owes": Y, "net": Z } }.
    """
    paid = defaultdict(float)
    owes = defaultdict(float)
    people = set()
    for t in transactions:
        payer = t.get('payer')
        try:
            amount = float(t.get('amount', 0))
        except:
            amount = 0.0
        beneficiaries = t.get('beneficiaries', [])
        people.add(payer)
        for b in beneficiaries:
            people.add(b)
        share = amount / len(beneficiaries) if beneficiaries else 0
        paid[payer] += amount
        for b in beneficiaries:
            owes[b] += share
    detailed = {}
    for person in people:
        detailed[person] = {
            "paid": round(paid.get(person, 0), 2),
            "owes": round(owes.get(person, 0), 2),
            "net": round(paid.get(person, 0) - owes.get(person, 0), 2)
        }
    return detailed


def get_lodging_summary(transactions):
    """
    Dla transakcji, których opis zawiera słowo "nocleg" (case-insensitive),
    dzieli kwotę równomiernie między beneficjentów, po czym sumuje, ile każda osoba
    powinna zapłacić za noclegi.
    Zwraca słownik { osoba: suma udziału za noclegi }.
    """
    summary = {}
    for tx in transactions:
        description = tx.get('description', '')
        if "nocleg" in description.lower():
            try:
                amount = float(tx.get('amount', 0))
            except ValueError:
                amount = 0.0
            beneficiaries = tx.get('beneficiaries', [])
            if beneficiaries:
                share = amount / len(beneficiaries)
                for b in beneficiaries:
                    summary[b] = summary.get(b, 0) + share
    for key in summary:
        summary[key] = round(summary[key], 2)
    return summary


@app.route('/', methods=['GET', 'POST'])
def index():
    init_session()
    settlement_matrix = None
    errors = None
    edit_index = None
    edit_transaction = None

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            payer = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip()
            beneficiaries_str = request.form.get('beneficiaries', '').strip()
            description = request.form.get('description', '').strip()
            if not payer or not amount_str or not beneficiaries_str:
                errors = "Pola: płatnik, kwota oraz beneficjenci są wymagane."
            else:
                try:
                    amount = float(amount_str.replace(',', '.'))
                except ValueError:
                    errors = "Nieprawidłowa kwota."
                if not errors:
                    beneficiaries = [b.strip() for b in beneficiaries_str.split(',') if b.strip()]
                    new_tx = {
                        'payer': payer,
                        'amount': amount,
                        'beneficiaries': beneficiaries,
                        'description': description
                    }
                    transactions = session.get('transactions', [])
                    transactions.append(new_tx)
                    session['transactions'] = transactions
                    return redirect(url_for('index'))
        elif action == 'edit':
            try:
                edit_index = int(request.form.get('index'))
                transactions = session.get('transactions', [])
                if 0 <= edit_index < len(transactions):
                    edit_transaction = transactions[edit_index]
                else:
                    errors = "Nieprawidłowy indeks transakcji."
            except ValueError:
                errors = "Błąd podczas pobierania indeksu."
        elif action == 'update':
            try:
                edit_index = int(request.form.get('index'))
            except ValueError:
                errors = "Błąd indeksu transakcji."
            if not errors:
                payer = request.form.get('payer', '').strip()
                amount_str = request.form.get('amount', '').strip()
                beneficiaries_str = request.form.get('beneficiaries', '').strip()
                description = request.form.get('description', '').strip()
                if not payer or not amount_str or not beneficiaries_str:
                    errors = "Wszystkie pola (płatnik, kwota, beneficjenci) są wymagane przy aktualizacji."
                else:
                    try:
                        amount = float(amount_str.replace(',', '.'))
                    except ValueError:
                        errors = "Nieprawidłowa kwota."
                    if not errors:
                        beneficiaries = [b.strip() for b in beneficiaries_str.split(',') if b.strip()]
                        updated_tx = {
                            'payer': payer,
                            'amount': amount,
                            'beneficiaries': beneficiaries,
                            'description': description
                        }
                        transactions = session.get('transactions', [])
                        if 0 <= edit_index < len(transactions):
                            transactions[edit_index] = updated_tx
                            session['transactions'] = transactions
                            return redirect(url_for('index'))
                        else:
                            errors = "Nieprawidłowy indeks przy aktualizacji."
        elif action == 'delete':
            try:
                del_index = int(request.form.get('index'))
                transactions = session.get('transactions', [])
                if 0 <= del_index < len(transactions):
                    transactions.pop(del_index)
                    session['transactions'] = transactions
                    return redirect(url_for('index'))
                else:
                    errors = "Nieprawidłowy indeks przy usuwaniu."
            except ValueError:
                errors = "Błąd podczas usuwania transakcji."
        elif action == 'calculate':
            transactions = session.get('transactions', [])
            # Używamy funkcji compute_settlement_matrix – rozliczenie proporcjonalne
            settlement_matrix = compute_settlement_matrix(transactions)
        elif action == 'reset':
            session['transactions'] = []
            return redirect(url_for('index'))
        elif action == 'import':
            uploaded_file = request.files.get('import_file')
            if uploaded_file:
                try:
                    content = uploaded_file.read().decode('utf-8')
                    imported_transactions = json.loads(content)
                    if isinstance(imported_transactions, list):
                        session['transactions'] = imported_transactions
                        return redirect(url_for('index'))
                    else:
                        errors = "Format pliku nieprawidłowy (oczekiwana lista transakcji)."
                except Exception as e:
                    errors = f"Błąd przy imporcie: {str(e)}"
            else:
                errors = "Nie wybrano pliku do importu."

    transactions = session.get('transactions', [])
    lodging_summary = get_lodging_summary(transactions)
    detailed_settlement = get_detailed_settlement(transactions)
    # Dla wygodnego wyświetlania w macierzy – sortujemy osoby
    positives = sorted([p for p, d in detailed_settlement.items() if d['net'] > 0])
    negatives = sorted([p for p, d in detailed_settlement.items() if d['net'] < 0])

    return render_template('index.html',
                           transactions=transactions,
                           settlement_matrix=settlement_matrix,
                           lodging_summary=lodging_summary,
                           detailed_settlement=detailed_settlement,
                           positives=positives,
                           negatives=negatives,
                           errors=errors,
                           edit_index=edit_index,
                           edit_transaction=edit_transaction)


@app.route('/download', methods=['GET'])
def download():
    init_session()
    transactions = session.get('transactions', [])
    json_data = json.dumps(transactions, ensure_ascii=False, indent=2)
    buf = io.BytesIO(json_data.encode('utf-8'))
    buf.seek(0)
    return send_file(buf, mimetype='application/json', as_attachment=True, download_name='transactions.json')


if __name__ == '__main__':
    app.run(debug=True)
