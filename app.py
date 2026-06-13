from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
import os
import threading
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///oms.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Twilio config (re-used pattern)
TWILIO_SID   = os.environ.get('TWILIO_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_TOKEN', '')
TWILIO_FROM  = 'whatsapp:+14155238886'

# ─── Stages config ─────────────────────────────────────────────────────────────
STAGES = ['placed', 'verified', 'sourcing', 'cutting', 'coating', 'qc1', 'fitting', 'qc2', 'shipped', 'delivered']

# Role required to advance from each stage
STAGE_ROLES = {
    'placed':    'system',
    'verified':  'system',
    'sourcing':  'system',
    'cutting':   'lab',
    'coating':   'lab',
    'qc1':       'qc',
    'fitting':   'lab',
    'qc2':       'qc',
    'shipped':   'dispatch',
    'delivered': 'system',
}

# SLA in hours per fulfilment path
SLA_HOURS = {
    'A': 5 * 24,   # 5 days for in-stock
    'B': 14 * 24,  # 14 days for China import
}

# ─── Models ────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    email    = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role     = db.Column(db.String(20), default='ops')  # admin, ops, lab, qc, dispatch
    phone    = db.Column(db.String(20), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)

class Lens(db.Model):
    """Inventory SKU — represents a physical lens type kept in Bangalore."""
    id           = db.Column(db.Integer, primary_key=True)
    power_sph    = db.Column(db.Float, nullable=False)
    power_cyl    = db.Column(db.Float, default=0.0)
    index        = db.Column(db.Float, nullable=False)   # 1.50, 1.56, 1.60, 1.67, 1.74
    coating      = db.Column(db.String(50), default='none')  # none, AR, blue-cut, photochromic
    material     = db.Column(db.String(50), default='CR-39')
    lens_type    = db.Column(db.String(20), default='single-vision')  # single-vision, bifocal, progressive
    stock_qty    = db.Column(db.Integer, default=0)
    reorder_at   = db.Column(db.Integer, default=10)
    supplier     = db.Column(db.String(100), default='China Supplier 1')
    lead_time    = db.Column(db.Integer, default=10)  # days

    @property
    def sku(self):
        return f"{self.power_sph:+.2f}/{self.index}/{self.coating}/{self.material}"

class Order(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    order_number       = db.Column(db.String(20), unique=True, nullable=False)
    source             = db.Column(db.String(20), default='website')  # website, store, marketplace
    store_location     = db.Column(db.String(100), default='Bangalore HQ')
    customer_name      = db.Column(db.String(100), nullable=False)
    customer_phone     = db.Column(db.String(20))
    customer_email     = db.Column(db.String(150))
    # Lens spec requested
    power_sph          = db.Column(db.Float, nullable=False)
    power_cyl          = db.Column(db.Float, default=0.0)
    index              = db.Column(db.Float, nullable=False)
    coating            = db.Column(db.String(50), default='none')
    material           = db.Column(db.String(50), default='CR-39')
    lens_type          = db.Column(db.String(20), default='single-vision')
    frame_model        = db.Column(db.String(100))
    # Fulfilment
    fulfilment_path    = db.Column(db.String(1))   # A or B
    matched_lens_id    = db.Column(db.Integer, db.ForeignKey('lens.id'), nullable=True)
    current_stage      = db.Column(db.String(20), default='placed')
    placed_at          = db.Column(db.DateTime, default=datetime.utcnow)
    promised_at        = db.Column(db.DateTime)
    delivered_at       = db.Column(db.DateTime, nullable=True)
    breach_risk        = db.Column(db.Integer, default=0)   # 0-100
    breach_alerted     = db.Column(db.Boolean, default=False)

    @property
    def is_breached(self):
        if self.current_stage == 'delivered':
            return self.delivered_at and self.delivered_at > self.promised_at
        return self.promised_at and datetime.utcnow() > self.promised_at

    @property
    def hours_remaining(self):
        if not self.promised_at:
            return None
        delta = self.promised_at - datetime.utcnow()
        return int(delta.total_seconds() / 3600)

class StageTransition(db.Model):
    """Log of every stage transition — feeds the AI model."""
    id          = db.Column(db.Integer, primary_key=True)
    order_id    = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    from_stage  = db.Column(db.String(20))
    to_stage    = db.Column(db.String(20), nullable=False)
    transitioned_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    notes       = db.Column(db.Text, nullable=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ─── Helpers ───────────────────────────────────────────────────────────────────

def generate_order_number():
    today  = date.today().strftime('%Y%m%d')
    count  = Order.query.filter(Order.placed_at >= datetime.combine(date.today(), datetime.min.time())).count()
    return f"ORD-{today}-{count + 1:04d}"

def find_matching_lens(power_sph, power_cyl, index, coating, material):
    """Find an exact lens match in inventory."""
    return Lens.query.filter_by(
        power_sph=power_sph, power_cyl=power_cyl, index=index,
        coating=coating, material=material
    ).filter(Lens.stock_qty > 0).first()

def calculate_promised_date(path):
    hours = SLA_HOURS[path]
    return datetime.utcnow() + timedelta(hours=hours)

def log_transition(order, from_stage, to_stage, user_id=None, notes=None):
    transition = StageTransition(
        order_id=order.id, from_stage=from_stage, to_stage=to_stage,
        user_id=user_id, notes=notes
    )
    db.session.add(transition)

def send_whatsapp(to_number, message):
    if not TWILIO_SID or not TWILIO_TOKEN or not to_number:
        return
    def _send():
        try:
            requests.post(
                f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json',
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data={'From': TWILIO_FROM, 'To': f'whatsapp:{to_number}', 'Body': message}
            )
        except Exception as e:
            print(f'WhatsApp error: {e}')
    threading.Thread(target=_send, daemon=True).start()

# ─── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if current_user.is_authenticated else url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name     = request.form['name'].strip()
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        role     = request.form.get('role', 'ops')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        user = User(
            name=name, email=email,
            password=generate_password_hash(password),
            role=role,
            is_admin=(User.query.count() == 0)
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        user     = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials.', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    orders_by_stage = {stage: [] for stage in STAGES}
    all_orders = Order.query.filter(Order.current_stage != 'delivered').all()
    for order in all_orders:
        orders_by_stage[order.current_stage].append(order)

    stats = {
        'total_active': len(all_orders),
        'path_a':       sum(1 for o in all_orders if o.fulfilment_path == 'A'),
        'path_b':       sum(1 for o in all_orders if o.fulfilment_path == 'B'),
        'breached':     sum(1 for o in all_orders if o.is_breached),
        'at_risk':      sum(1 for o in all_orders if o.breach_risk >= 60 and not o.is_breached),
    }
    return render_template('dashboard.html', orders_by_stage=orders_by_stage, stats=stats, stages=STAGES)

# ─── Order intake (public form) ────────────────────────────────────────────────

@app.route('/order/new', methods=['GET', 'POST'])
def new_order():
    if request.method == 'POST':
        power_sph = float(request.form['power_sph'])
        power_cyl = float(request.form.get('power_cyl', 0))
        index     = float(request.form['index'])
        coating   = request.form.get('coating', 'none')
        material  = request.form.get('material', 'CR-39')

        # AUTO-VERIFY: check inventory
        matched = find_matching_lens(power_sph, power_cyl, index, coating, material)
        path    = 'A' if matched else 'B'

        order = Order(
            order_number    = generate_order_number(),
            source          = request.form.get('source', 'website'),
            store_location  = request.form.get('store_location', 'Bangalore HQ'),
            customer_name   = request.form['customer_name'].strip(),
            customer_phone  = request.form.get('customer_phone', '').strip(),
            customer_email  = request.form.get('customer_email', '').strip(),
            power_sph       = power_sph,
            power_cyl       = power_cyl,
            index           = index,
            coating         = coating,
            material        = material,
            lens_type       = request.form.get('lens_type', 'single-vision'),
            frame_model     = request.form.get('frame_model', ''),
            fulfilment_path = path,
            matched_lens_id = matched.id if matched else None,
            current_stage   = 'verified',  # auto-verified
            promised_at     = calculate_promised_date(path),
        )
        db.session.add(order)
        db.session.flush()  # get order.id

        # Log auto-transitions
        log_transition(order, None, 'placed')
        log_transition(order, 'placed', 'verified', notes='Auto-verified by system')

        # Auto-decide sourcing path
        if path == 'A':
            order.current_stage = 'sourcing'
            log_transition(order, 'verified', 'sourcing', notes=f'In-stock lens matched: SKU {matched.sku}')
            matched.stock_qty -= 1
            # In-stock → can move to cutting immediately
            order.current_stage = 'cutting'
            log_transition(order, 'sourcing', 'cutting', notes='In-stock lens available')
        else:
            order.current_stage = 'sourcing'
            log_transition(order, 'verified', 'sourcing', notes='Out of stock — ordering from China')

        db.session.commit()
        flash(f'Order {order.order_number} placed! Path {path}, promised by {order.promised_at.strftime("%d %b")}.', 'success')
        return redirect(url_for('view_order', order_id=order.id))
    return render_template('new_order.html')

# ─── Order view & status update ────────────────────────────────────────────────

@app.route('/order/<int:order_id>')
@login_required
def view_order(order_id):
    order       = Order.query.get_or_404(order_id)
    transitions = StageTransition.query.filter_by(order_id=order.id).order_by(StageTransition.transitioned_at).all()
    return render_template('view_order.html', order=order, transitions=transitions, stages=STAGES)

@app.route('/order/<int:order_id>/advance', methods=['POST'])
@login_required
def advance_stage(order_id):
    order = Order.query.get_or_404(order_id)
    data  = request.get_json() or {}
    new_stage = data.get('to_stage') or request.form.get('to_stage')
    notes     = data.get('notes') or request.form.get('notes', '')

    if new_stage not in STAGES:
        return jsonify({'error': 'Invalid stage'}), 400

    # Role check
    required_role = STAGE_ROLES.get(order.current_stage)
    if required_role and required_role != 'system':
        if current_user.role != required_role and current_user.role != 'admin' and not current_user.is_admin:
            return jsonify({'error': f'Only {required_role} team can advance from this stage'}), 403

    old_stage = order.current_stage
    order.current_stage = new_stage
    log_transition(order, old_stage, new_stage, user_id=current_user.id, notes=notes)

    if new_stage == 'delivered':
        order.delivered_at = datetime.utcnow()

    db.session.commit()
    return jsonify({'success': True})

# ─── Inventory ─────────────────────────────────────────────────────────────────

@app.route('/inventory')
@login_required
def inventory():
    lenses = Lens.query.order_by(Lens.power_sph, Lens.index).all()
    low_stock = [l for l in lenses if l.stock_qty <= l.reorder_at]
    return render_template('inventory.html', lenses=lenses, low_stock=low_stock)

@app.route('/inventory/new', methods=['POST'])
@login_required
def new_lens():
    if not current_user.is_admin and current_user.role != 'admin':
        return redirect(url_for('inventory'))
    lens = Lens(
        power_sph  = float(request.form['power_sph']),
        power_cyl  = float(request.form.get('power_cyl', 0)),
        index      = float(request.form['index']),
        coating    = request.form.get('coating', 'none'),
        material   = request.form.get('material', 'CR-39'),
        stock_qty  = int(request.form.get('stock_qty', 0)),
        reorder_at = int(request.form.get('reorder_at', 10)),
    )
    db.session.add(lens)
    db.session.commit()
    flash(f'Added lens {lens.sku}', 'success')
    return redirect(url_for('inventory'))

# ─── Init ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=10000, debug=False)