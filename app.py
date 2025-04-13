from flask import Flask, render_template, request, redirect, url_for, session, send_file
import json
import io

app = Flask(__name__)
app.secret_key = 'tajny_klucz'  # Ustaw swój własny, bezpieczny klucz


def init_session():
    if 'transactions' not in session:
        session['transactions'] = []


def compute_settlement(transactions):
    """
    Dla każdej transakcji:
      - Dodajemy całą kwotę do "paid" dla płatnika,
      - Każdy beneficjent (lista) zobowiązany jest do zapłaty równego udziału.
    Oblicza saldo netto (wpłacone minus udział) dla każdej osoby oraz generuje listę transakcji rozliczeniowych.
    """
    paid = {}
    owes = {}
    persons = set()

    for t in transactions:
        payer = t.get('payer')
        try:
            amount = float(t.get('amount', 0))
        except:
            amount = 0.0
        beneficiaries = t.get('beneficiaries', [])
        persons.add(payer)
        for b in beneficiaries:
            persons.add(b)
        share = amount / len(beneficiaries) if beneficiaries else 0
        paid[payer] = paid.get(payer, 0) + amount
        for b in beneficiaries:
            owes[b] = owes.get(b, 0) + share

    net = {}
    for person in persons:
        net[person] = round(paid.get(person, 0) - owes.get(person, 0), 2)

    creditors = []
    debtors = []
    for person, balance in net.items():
        if balance > 0:
            creditors.append([person, balance])
        elif balance < 0:
            debtors.append([person, -balance])

    transactions_settlement = []
    for debtor in debtors:
        d_name, d_amount = debtor
        while d_amount > 0 and creditors:
            creditor = creditors[0]
            c_name, c_amount = creditor
            payment = min(d_amount, c_amount)
            transactions_settlement.append((d_name, c_name, round(payment, 2)))
            d_amount -= payment
            c_amount -= payment
            if abs(c_amount) < 1e-2:
                creditors.pop(0)
            else:
                creditor[1] = c_amount
    return {'net': net, 'transactions': transactions_settlement}


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
            # Dodaj nową transakcję
            payer = request.form.get('payer', '').strip()
            amount_str = request.form.get('amount', '').strip()
            beneficiaries_str = request.form.get('beneficiaries', '').strip()
            if not payer or not amount_str or not beneficiaries_str:
                errors = "Wszystkie pola są wymagane przy dodawaniu transakcji."
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
                        'beneficiaries': beneficiaries
                    }
                    transactions = session.get('transactions', [])
                    transactions.append(new_tx)
                    session['transactions'] = transactions
                    return redirect(url_for('index'))

        elif action == 'edit':
            # Wybierz transakcję do edycji – wartość "index" jest przesyłana ukrytym polem
            try:
                edit_index = int(request.form.get('index'))
                transactions = session.get('transactions', [])
                if 0 <= edit_index < len(transactions):
                    edit_transaction = transactions[edit_index]
                else:
                    errors = "Nieprawidłowy indeks transakcji."
            except ValueError:
                errors = "Błąd podczas pobierania indeksu transakcji."

        elif action == 'update':
            # Aktualizuj transakcję o podanym indeksie
            try:
                edit_index = int(request.form.get('index'))
            except ValueError:
                errors = "Błąd indeksu transakcji."
            if not errors:
                payer = request.form.get('payer', '').strip()
                amount_str = request.form.get('amount', '').strip()
                beneficiaries_str = request.form.get('beneficiaries', '').strip()
                if not payer or not amount_str or not beneficiaries_str:
                    errors = "Wszystkie pola są wymagane przy aktualizacji transakcji."
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
                            'beneficiaries': beneficiaries
                        }
                        transactions = session.get('transactions', [])
                        if 0 <= edit_index < len(transactions):
                            transactions[edit_index] = updated_tx
                            session['transactions'] = transactions
                            return redirect(url_for('index'))
                        else:
                            errors = "Nieprawidłowy indeks transakcji przy aktualizacji."

        elif action == 'delete':
            # Usuń transakcję o podanym indeksie
            try:
                del_index = int(request.form.get('index'))
                transactions = session.get('transactions', [])
                if 0 <= del_index < len(transactions):
                    transactions.pop(del_index)
                    session['transactions'] = transactions
                    return redirect(url_for('index'))
                else:
                    errors = "Nieprawidłowy indeks transakcji przy usuwaniu."
            except ValueError:
                errors = "Błąd podczas usuwania transakcji."

        elif action == 'calculate':
            transactions = session.get('transactions', [])
            settlement = compute_settlement(transactions)

        elif action == 'reset':
            session['transactions'] = []
            return redirect(url_for('index'))

        elif action == 'import':
            # Wczytanie pliku z transakcjami (format JSON)
            uploaded_file = request.files.get('import_file')
            if uploaded_file:
                try:
                    content = uploaded_file.read().decode('utf-8')
                    imported_transactions = json.loads(content)
                    if isinstance(imported_transactions, list):
                        session['transactions'] = imported_transactions
                        return redirect(url_for('index'))
                    else:
                        errors = "Format pliku nie jest prawidłowy (oczekiwana lista transakcji)."
                except Exception as e:
                    errors = f"Błąd podczas importowania pliku: {str(e)}"
            else:
                errors = "Nie wybrano pliku do importu."

    transactions = session.get('transactions', [])
    return render_template('index.html', transactions=transactions, settlement=settlement, errors=errors,
                           edit_index=edit_index, edit_transaction=edit_transaction)


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
