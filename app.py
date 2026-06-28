from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import mysql.connector
from datetime import datetime
import time

app = Flask(__name__)
app.secret_key = "change_this_secret"

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "admin123",        # <- CHANGE to your MySQL password (or leave empty)
    "database": "ration_system"
}

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

# ---------- WEB ROUTES ----------
@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        role = request.form.get('role')
        rfid = request.form.get('rfid_uid', '').strip()
        fp = request.form.get('fingerprint_id', '').strip()

        conn = get_db()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM users WHERE rfid_uid=%s AND fingerprint_id=%s AND role=%s", (rfid, fp, role))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user:
            flash(f"Logged in as {user['name']} ({user['role']})", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Login failed", "danger")
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    conn = get_db()
    cur = conn.cursor(dictionary=True)

    # Fetch stock
    cur.execute("SELECT id, item_name, quantity, unit FROM stock ORDER BY item_name")
    stock = cur.fetchall()

    # Fetch latest transactions
    cur.execute(
        "SELECT t.id, u.name AS collector_name, s.item_name, t.quantity, t.transaction_date "
        "FROM transactions t "
        "JOIN users u ON t.user_id=u.id "
        "JOIN stock s ON t.item_name=s.id "
        "ORDER BY t.transaction_date DESC LIMIT 10"
    )
    txns = cur.fetchall()

    # Count roles
    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE role='Distributor'")
    distributors = cur.fetchone()['cnt']
    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE role='Collector'")
    collectors = cur.fetchone()['cnt']

    cur.close()
    conn.close()

    return render_template('dashboard.html', stock=stock, transactions=txns, distributors=distributors, collectors=collectors)


@app.route('/add_stock', methods=['GET', 'POST'])
def add_stock():
    if request.method == 'POST':
        item_name = request.form['item_name'].strip()
        qty = int(request.form['quantity'])
        unit = request.form.get('unit', 'kg')

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM stock WHERE item_name=%s", (item_name,))
        r = cur.fetchone()

        if r:
            cur.execute("UPDATE stock SET quantity = quantity + %s, last_updated = %s WHERE item_name = %s",
                        (qty, datetime.now(), item_name))
        else:
            cur.execute("INSERT INTO stock (item_name, quantity, unit) VALUES (%s, %s, %s)",
                        (item_name, qty, unit))

        conn.commit()
        cur.close()
        conn.close()

        flash("Stock added/updated", "success")
        return redirect(url_for('dashboard'))

    return render_template('add_stock.html')


@app.route('/distribute', methods=['GET', 'POST'])
def distribute():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, name FROM users WHERE role='Collector' ORDER BY name")
    collectors = cur.fetchall()
    cur.execute("SELECT id, item_name, quantity FROM stock ORDER BY item_name")
    stock = cur.fetchall()
    cur.close()
    conn.close()

    if request.method == 'POST':
        item_id = int(request.form['item_id'])
        qty = int(request.form['quantity'])

        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO pending_distributions (item_id, quantity) VALUES (%s, %s)", (item_id, qty))
        conn.commit()

        cur.execute("SELECT LAST_INSERT_ID()")
        pid = cur.fetchone()[0]
        cur.close()
        conn.close()

        flash(f"Distribution request created (ID {pid}). Now place Distributor finger on hardware to start.", "info")
        return redirect(url_for('pending_status', pid=pid))

    return render_template('distribute.html', collectors=collectors, stock=stock)


@app.route('/pending/<int:pid>')
def pending_status(pid):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM pending_distributions WHERE id=%s", (pid,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        flash("Pending request not found", "danger")
        return redirect(url_for('dashboard'))

    return render_template('pending_status.html', pending=row)


@app.route('/transactions')
def transactions():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT t.id, u.name AS collector_name, s.item_name, t.quantity, t.transaction_date " 
        "FROM transactions t " 
        "JOIN users u ON t.user_id=u.id "
        "JOIN stock s ON t.item_name=s.id "
        "ORDER BY t.transaction_date DESC"
    )
    txns = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('transactions.html', transactions=txns)


# ---------- API endpoints used by ESP32 ----------

@app.route('/api/pending', methods=['GET'])
def api_pending():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM pending_distributions WHERE status='pending' ORDER BY created_at LIMIT 1")
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({"status": "none"}), 200

    cur.execute("UPDATE pending_distributions SET status='processing' WHERE id=%s", (row['id'],))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok", "id": row['id'], "item_id": row['item_id'], "quantity": row['quantity']}), 200


@app.route('/api/complete', methods=['POST'])
def api_complete():
    data = request.get_json(force=True)
    pid = data.get('pending_id')
    dist_fp = data.get('dist_fp')
    collector_rfid = data.get('collector_rfid')
    collector_fp = data.get('collector_fp')
    item_id = data.get('item_id')
    qty = data.get('quantity')

    if not all([pid, dist_fp is not None, collector_rfid, collector_fp is not None, item_id, qty]):
        return jsonify({"status": "error", "message": "missing fields"}), 400

    conn = get_db()
    cur = conn.cursor(dictionary=True)

    # Verify distributor
    cur.execute("SELECT id, name FROM users WHERE fingerprint_id=%s AND role='Distributor'", (str(dist_fp),))
    distributor = cur.fetchone()
    if not distributor:
        cur.execute("UPDATE pending_distributions SET status='failed', result_message=%s WHERE id=%s",
                    ("Distributor not found", pid))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "failed", "message": "Distributor not found"}), 404

    # Verify collector
    cur.execute("SELECT id, name FROM users WHERE rfid_uid=%s AND fingerprint_id=%s AND role='Collector'",
                (collector_rfid, str(collector_fp)))
    collector = cur.fetchone()
    if not collector:
        cur.execute("UPDATE pending_distributions SET status='failed', result_message=%s WHERE id=%s",
                    ("Collector not found", pid))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "failed", "message": "Collector not found"}), 404

    # Verify stock
    cur.execute("SELECT id, quantity FROM stock WHERE id=%s", (item_id,))
    stock_row = cur.fetchone()
    if not stock_row:
        cur.execute("UPDATE pending_distributions SET status='failed', result_message=%s WHERE id=%s",
                    ("Item not found", pid))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "failed", "message": "Item not found"}), 404

    if stock_row['quantity'] < int(qty):
        cur.execute("UPDATE pending_distributions SET status='failed', result_message=%s WHERE id=%s",
                    ("Insufficient stock", pid))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "failed", "message": "Insufficient stock"}), 400

    # Deduct and record transaction
    new_qty = stock_row['quantity'] - int(qty)
    cur.execute("UPDATE stock SET quantity=%s, last_updated=%s WHERE id=%s",
                (new_qty, datetime.now(), item_id))
    cur.execute("INSERT INTO transactions (user_id, item_name, quantity, transaction_date) VALUES (%s, %s, %s, %s)",
                (collector['id'], item_id, int(qty), datetime.now()))
    cur.execute("UPDATE pending_distributions SET status='done', result_message=%s WHERE id=%s", ("OK", pid))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "success", "message": "Distribution recorded"}), 200


# ---------- ADDITIONAL ENDPOINTS ----------

@app.route('/pending_status_ajax/<int:pid>')
def pending_status_ajax(pid):
    conn = get_db(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM pending_distributions WHERE id=%s", (pid,))
    row = cur.fetchone()
    cur.close(); conn.close()
    
    if not row:
        return {"status":"not_found","status_message":"Pending request not found"}
    
    # Convert DB status to friendly message
    status_msg = {
        "pending": "Waiting for Distributor fingerprint...",
        "processing": "Processing hardware...",
        "done": "Transaction completed successfully!",
        "failed": f"Failed: {row['result_message'] or 'Unknown error'}"
    }.get(row['status'], "Unknown status")
    
    return {"status": row['status'], "status_message": status_msg}


@app.route('/start_distribution/<int:pid>', methods=['POST'])
def start_distribution(pid):
    """
    Trigger ESP32 to process this pending request.
    In practice, the ESP32 is already polling /api/pending, so 
    this endpoint can just mark it ready or do nothing.
    """
    conn = get_db(); cur = conn.cursor()
    # Ensure it is pending
    cur.execute("UPDATE pending_distributions SET status='pending' WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    return {"status":"ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)