import json
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, send_file
import io

app = Flask(__name__)
app.secret_key = 'tajny_klucz'  # Ustaw swój własny bezpieczny klucz


def init_session():
    if 'transactions' not in session:
        session['transactions'] = []


def compute_settlement(transactions):
    """
    Nowa logika obliczania rozliczenia:
     - Wyznacza zbiór wszystkich osób na podstawie płatników oraz beneficjentów.
     - Dla każdej transakcji kwota dzielona jest równo między beneficjentów.
     - Oblicza się dla każdej osoby:
          paid (suma wpłat jako płatnik) oraz owes (suma udziałów, gdy osoba występuje jako beneficjent).
     - Net = paid - owes.
     - Następnie, osoby z nadpłatą (net > 0) oraz te, które mają niedopłatę (net < 0)
       są sortowane, a potem dopasowywane do stworzenia listy rozliczeń (kto komu ile oddaje).

    Zwraca słownik z dwoma kluczami:
       "net" - podsumowanie netto (dla każdej osoby),
       "transactions" - lista rozliczeniowa, np. { "from": X, "to": Y, "amount": Z }.
    """
    # Wyznacz zbiór osób
    people = set()
    for item in transactions:
        people.add(item["payer"])
        for b in item["beneficiaries"]:
            people.add(b)
    people = list(people)

    # Oblicz sumy wpłat i udziałów
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
            # edge case: jeśli brak beneficjentów, pomijamy transakcję
            continue
        share = amt / n
        paid[payer] += amt
        for b in beneficiaries:
            owes[b] += share

    # Oblicz saldo netto dla każdej osoby
    net = {}
    for p in people:
        net[p] = paid[p] - owes[p]

    # Podziel osoby na pozytywne (należy im się pieniądz) i negatywne (muszą zapłacić)
    positive = []
    negative = []
    for p in people:
        if abs(net[p]) < 0.000001:
            net[p] = 0.0
        if net[p] > 0:
            positive.append((p, net[p]))
        elif net[p] < 0:
            negative.append((p, -net[p]))  # przechowujemy jako wartość dodatnią ile ktoś musi zapłacić

    positive.sort(key=lambda x: x[1], reverse=True)
    negative.sort(key=lambda x: x[1], reverse=True)

    # Dopasuj dłużników do kredytorów, tworząc listę rozliczeń
    settlements = []
    i, j = 0, 0
    while i < len(positive) and j < len(negative):
        pos_name, pos_amt = positive[i]
        neg_name, neg_amt = negative[j]
        settle_amt = min(pos_amt, neg_amt)
        settlements.append((neg_name, pos_name, settle_amt))
        pos_amt -= settle_amt
        neg_amt -= settle_amt
        positive[i] = (pos_name, pos_amt)
        negative[j] = (neg_name, neg_amt)
        if abs(pos_amt) < 0.000001:
            i += 1
        if abs(neg_amt) < 0.000001:
            j += 1

    settlement_list = []
    for payer, payee, amount in settlements:
        settlement_list.append({
            "from": payer,
            "to": payee,
            "amount": round(amount, 2)
        })

    net_summary = {p: round(net[p], 2) for p in sorted(people)}

    return {"net": net_summary, "transactions": settlement_list}


def get_detailed_settlement(transactions):
    """
    Oblicza dla każdej osoby:
      - "paid": suma wpłat jako płatnik,
      - "owes": suma udziałów (gdy osoba jest beneficjentem),
      - "net": różnica (paid - owes).
    Zwraca słownik: { osoba: { "paid": X, "owes": Y, "net": Z } }.
    """
    from collections import defaultdict
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
    kwota jest dzielona równo między beneficjentów.
    Sumuje udział dla każdej osoby za noclegi.
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
    settlement = None
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
            settlement = compute_settlement(transactions)
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
    return render_template('index.html',
                           transactions=transactions,
                           settlement=settlement,
                           lodging_summary=lodging_summary,
                           detailed_settlement=detailed_settlement,
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
