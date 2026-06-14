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
    'sourcing':  'ops',     # warehouse confirms lens pulled (Path A) or supplier arrival (Path B)
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


class ModelStore(db.Model):
    """Persistent model storage — survives redeploys."""
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(50), unique=True, nullable=False)
    blob         = db.Column(db.LargeBinary, nullable=False)
    features     = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

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
    all_orders = Order.query.filter(Order.current_stage != 'delivered').all()
    orders_by_stage = {stage: [] for stage in STAGES}
    for order in all_orders:
        orders_by_stage[order.current_stage].append(order)

    stats = {
        'total_active': len(all_orders),
        'path_a':       sum(1 for o in all_orders if o.fulfilment_path == 'A'),
        'path_b':       sum(1 for o in all_orders if o.fulfilment_path == 'B'),
        'breached':     sum(1 for o in all_orders if o.is_breached),
        'at_risk':      sum(1 for o in all_orders if o.breach_risk >= 60 and not o.is_breached),
    }

    active_stage = request.args.get('stage')
    q            = request.args.get('q', '').strip().lower()
    path_f       = request.args.get('path', '')
    store_f      = request.args.get('store', '')
    risk_f       = request.args.get('risk', '')

    filtered = all_orders
    if active_stage:
        filtered = [o for o in filtered if o.current_stage == active_stage]
    if q:
        filtered = [o for o in filtered if q in o.order_number.lower() or q in (o.customer_name or '').lower()]
    if path_f:
        filtered = [o for o in filtered if o.fulfilment_path == path_f]
    if store_f:
        filtered = [o for o in filtered if o.store_location == store_f]
    if risk_f == 'high':
        filtered = [o for o in filtered if o.breach_risk >= 60]
    elif risk_f == 'med':
        filtered = [o for o in filtered if 30 <= o.breach_risk < 60]
    elif risk_f == 'low':
        filtered = [o for o in filtered if o.breach_risk < 30]

    # Sort by risk desc by default
    filtered = sorted(filtered, key=lambda o: -o.breach_risk)

    return render_template('dashboard.html',
        orders_by_stage=orders_by_stage, stats=stats, stages=STAGES,
        filtered_orders=filtered, active_stage=active_stage
    )


@app.route('/metrics')
@login_required
def metrics():
    """Operational metrics page — daily/weekly view."""
    now = datetime.utcnow()
    today_start = datetime.combine(now.date(), datetime.min.time())
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)

    # Today
    today_orders = Order.query.filter(Order.placed_at >= today_start).count()

    # This week
    week_orders = Order.query.filter(Order.placed_at >= week_start).count()
    week_delivered = Order.query.filter(
        Order.delivered_at >= week_start,
        Order.current_stage == 'delivered'
    ).all()
    week_breached = sum(1 for o in week_delivered if o.delivered_at and o.delivered_at > o.promised_at)
    week_breach_rate = int((week_breached / len(week_delivered) * 100)) if week_delivered else 0

    # Average fulfilment time (last 30 days)
    month_delivered = Order.query.filter(
        Order.delivered_at >= month_start,
        Order.current_stage == 'delivered'
    ).all()
    if month_delivered:
        durations = [(o.delivered_at - o.placed_at).total_seconds() / 3600 for o in month_delivered if o.delivered_at]
        avg_hours = sum(durations) / len(durations) if durations else 0
        avg_days = avg_hours / 24
    else:
        avg_days = 0

    # Current at-risk
    active = Order.query.filter(Order.current_stage != 'delivered').all()
    at_risk_now = sum(1 for o in active if o.breach_risk >= 60)

    # Top SKUs (by demand last 30 days)
    from collections import Counter
    recent = Order.query.filter(Order.placed_at >= month_start).all()
    sku_counter = Counter()
    for o in recent:
        sku = f"{o.power_sph:+.2f} / {o.index} / {o.coating}"
        sku_counter[sku] += 1
    top_skus = sku_counter.most_common(8)

    # Stage distribution (active)
    stage_dist = {s: 0 for s in STAGES if s != 'delivered'}
    for o in active:
        stage_dist[o.current_stage] = stage_dist.get(o.current_stage, 0) + 1

    # Path distribution
    path_a = sum(1 for o in active if o.fulfilment_path == 'A')
    path_b = sum(1 for o in active if o.fulfilment_path == 'B')

    return render_template('metrics.html',
        today_orders=today_orders, week_orders=week_orders,
        week_delivered_count=len(week_delivered), week_breach_rate=week_breach_rate,
        avg_days=round(avg_days, 1), at_risk_now=at_risk_now,
        top_skus=top_skus, stage_dist=stage_dist,
        path_a=path_a, path_b=path_b, total_active=len(active)
    )


@app.route('/api/v1/orders', methods=['POST'])
def api_create_order():
    """External API for marketplace/storefront integrations.
    Expects JSON: {customer_name, customer_phone, customer_email, source,
                   store_location, power_sph, power_cyl, index, coating,
                   material, lens_type, frame_model}
    Returns: {order_id, order_number, fulfilment_path, promised_at, hours_to_delivery}
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'JSON body required'}), 400

        required = ['customer_name', 'power_sph', 'index']
        missing = [f for f in required if f not in data]
        if missing:
            return jsonify({'error': f'Missing fields: {missing}'}), 400

        power_sph = float(data['power_sph'])
        power_cyl = float(data.get('power_cyl', 0))
        index = float(data['index'])
        coating = data.get('coating', 'none')
        material = data.get('material', 'CR-39')

        matched = find_matching_lens(power_sph, power_cyl, index, coating, material)
        path = 'A' if matched else 'B'

        order = Order(
            order_number    = generate_order_number(),
            source          = data.get('source', 'marketplace'),
            store_location  = data.get('store_location', 'Online'),
            customer_name   = data['customer_name'],
            customer_phone  = data.get('customer_phone', ''),
            customer_email  = data.get('customer_email', ''),
            power_sph       = power_sph,
            power_cyl       = power_cyl,
            index           = index,
            coating         = coating,
            material        = material,
            lens_type       = data.get('lens_type', 'single-vision'),
            frame_model     = data.get('frame_model', ''),
            fulfilment_path = path,
            matched_lens_id = matched.id if matched else None,
            current_stage   = 'sourcing',
            promised_at     = calculate_promised_date(path),
        )
        db.session.add(order)
        db.session.flush()

        log_transition(order, None, 'placed', notes='Via API')
        log_transition(order, 'placed', 'verified', notes='Auto-verified')
        if matched:
            matched.stock_qty -= 1
            log_transition(order, 'verified', 'sourcing', notes=f'In-stock match: SKU {matched.sku}')
        else:
            log_transition(order, 'verified', 'sourcing', notes='Supplier order required')

        db.session.commit()

        return jsonify({
            'order_id':         order.id,
            'order_number':     order.order_number,
            'fulfilment_path':  'in-stock' if path == 'A' else 'sourced',
            'promised_at':      order.promised_at.isoformat(),
            'hours_to_delivery': SLA_HOURS[path],
            'tracking_url':     f'/order/{order.id}'
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

        # Both paths land in 'sourcing' — warehouse confirms next step
        order.current_stage = 'sourcing'
        if path == 'A':
            log_transition(order, 'verified', 'sourcing', notes=f'In-stock lens matched: SKU {matched.sku}. Awaiting warehouse confirmation.')
            matched.stock_qty -= 1
        else:
            log_transition(order, 'verified', 'sourcing', notes='Out of stock — supplier order required.')

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

    old_idx = STAGES.index(order.current_stage)
    new_idx = STAGES.index(new_stage)
    is_backward = new_idx < old_idx

    # Backward = QC or admin only, must have notes
    if is_backward:
        if current_user.role not in ('qc', 'admin') and not current_user.is_admin:
            return jsonify({'error': 'Only QC team or admin can rollback orders'}), 403
        if not notes:
            return jsonify({'error': 'Rollback requires a reason'}), 400
        # Reset SLA — push promised date forward
        order.promised_at = datetime.utcnow() + timedelta(hours=SLA_HOURS[order.fulfilment_path] * 0.5)
        # Notify customer via WhatsApp
        if order.customer_phone:
            msg = (
                f"Hi {order.customer_name}, your Lumio order {order.order_number} has had a minor "
                f"setback in quality control. We've updated your new expected delivery to "
                f"{order.promised_at.strftime('%d %b')}. We apologise for the inconvenience."
            )
            send_whatsapp(order.customer_phone, msg)
    else:
        # Forward — role check
        required_role = STAGE_ROLES.get(order.current_stage)
        if required_role and required_role != 'system':
            if current_user.role != required_role and current_user.role != 'admin' and not current_user.is_admin:
                return jsonify({'error': f'Only {required_role} team can advance from this stage'}), 403

    old_stage = order.current_stage
    order.current_stage = new_stage
    direction = 'backward' if is_backward else 'forward'
    full_notes = f'[{direction}] {notes}' if notes else f'[{direction}]'
    log_transition(order, old_stage, new_stage, user_id=current_user.id, notes=full_notes)

    if new_stage == 'delivered':
        order.delivered_at = datetime.utcnow()

    db.session.commit()
    return jsonify({'success': True, 'direction': direction})

# ─── Inventory ─────────────────────────────────────────────────────────────────

@app.route('/inventory')
@login_required
def inventory():
    q          = request.args.get('q', '').strip().lower()
    f_index    = request.args.get('index', '')
    f_coating  = request.args.get('coating', '')
    f_material = request.args.get('material', '')
    f_status   = request.args.get('status', '')
    sort       = request.args.get('sort', 'power')

    query = Lens.query
    if f_index:    query = query.filter(Lens.index == float(f_index))
    if f_coating:  query = query.filter(Lens.coating == f_coating)
    if f_material: query = query.filter(Lens.material == f_material)

    lenses = query.all()

    if q:
        lenses = [l for l in lenses if q in l.sku.lower()]
    if f_status == 'out':
        lenses = [l for l in lenses if l.stock_qty == 0]
    elif f_status == 'low':
        lenses = [l for l in lenses if 0 < l.stock_qty <= l.reorder_at]
    elif f_status == 'ok':
        lenses = [l for l in lenses if l.stock_qty > l.reorder_at]

    if sort == 'stock_asc':
        lenses = sorted(lenses, key=lambda l: l.stock_qty)
    elif sort == 'stock_desc':
        lenses = sorted(lenses, key=lambda l: -l.stock_qty)
    else:
        lenses = sorted(lenses, key=lambda l: (l.power_sph, l.index))

    low_stock = [l for l in lenses if l.stock_qty <= l.reorder_at]
    return render_template('inventory.html',
        lenses=lenses, low_stock=low_stock,
        q=q, f_index=f_index, f_coating=f_coating, f_material=f_material,
        f_status=f_status, sort=sort
    )

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


# ─── ML model routes ──────────────────────────────────────────────────────────

# ─── ML helpers ───────────────────────────────────────────────────────────────
import pickle
MODEL_PATH = '/tmp/lumio_model.pkl'

def _order_features(order, current_stage=None, hours_in_stage=None, now=None):
    if now is None: now = datetime.utcnow()
    if current_stage is None: current_stage = order.current_stage
    hours_placed = (now - order.placed_at).total_seconds() / 3600
    if hours_in_stage is None:
        last_t = StageTransition.query.filter_by(order_id=order.id, to_stage=current_stage).order_by(StageTransition.transitioned_at.desc()).first()
        hours_in_stage = (now - last_t.transitioned_at).total_seconds() / 3600 if last_t else 0
    return {
        'power_sph_abs':   abs(order.power_sph),
        'index_v':         order.index,
        'is_premium_lens': 1 if order.index >= 1.67 else 0,
        'is_photochromic': 1 if order.coating == 'photochromic' else 0,
        'is_high_index_mat': 1 if order.material == 'high-index' else 0,
        'path_b':          1 if order.fulfilment_path == 'B' else 0,
        'stage_int':       STAGES.index(current_stage) if current_stage in STAGES else 0,
        'hours_placed':    hours_placed,
        'hours_in_stage':  hours_in_stage,
        'day_of_week':     order.placed_at.weekday(),
        'hour_of_day':     order.placed_at.hour,
    }

def _load_model():
    """Load model from DB (persistent across redeploys)."""
    try:
        rec = ModelStore.query.filter_by(name='breach_predictor').first()
        if not rec:
            return None, None
        import json
        model = pickle.loads(rec.blob)
        features = json.loads(rec.features)
        return model, features
    except Exception as e:
        print(f'Model load error: {e}')
        return None, None

def _save_model(model, features, metadata=None):
    """Save model to DB."""
    import json
    blob = pickle.dumps(model)
    rec = ModelStore.query.filter_by(name='breach_predictor').first()
    if rec:
        rec.blob = blob
        rec.features = json.dumps(features)
        rec.metadata_json = json.dumps(metadata or {})
        rec.created_at = datetime.utcnow()
    else:
        rec = ModelStore(
            name='breach_predictor', blob=blob,
            features=json.dumps(features),
            metadata_json=json.dumps(metadata or {})
        )
        db.session.add(rec)
    db.session.commit()

def predict_risk(order):
    import pandas as pd
    model, feats = _load_model()
    if model is None:
        return 0, ['Model not trained']
    X = pd.DataFrame([_order_features(order)])[feats]
    prob = float(model.predict_proba(X)[0][1])
    risk = int(prob * 100)
    f = _order_features(order)
    reasons = []
    if f['path_b']: reasons.append("Sourced from supplier (slower path)")
    if f['is_premium_lens']: reasons.append(f"Premium {order.index} index lens")
    if f['is_photochromic']: reasons.append("Photochromic coating adds time")
    if f['hours_in_stage'] > 12: reasons.append(f"In {order.current_stage} for {int(f['hours_in_stage'])}h")
    if f['hours_placed'] > SLA_HOURS[order.fulfilment_path] * 0.6: reasons.append("Past 60% of SLA")
    if f['day_of_week'] >= 4: reasons.append("Friday/weekend order")
    if not reasons: reasons.append("Multiple soft signals")
    return risk, reasons[:3]

@app.route('/admin/train-model')
@login_required
def train_model_endpoint():
    if not current_user.is_admin:
        return 'Admin only', 403
    try:
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        rows = []
        delivered = Order.query.filter_by(current_stage='delivered').all()
        for order in delivered:
            sla_h = SLA_HOURS.get(order.fulfilment_path, 120)
            actual = (order.delivered_at - order.placed_at).total_seconds() / 3600 if order.delivered_at else None
            if actual is None: continue
            breached = 1 if actual > sla_h else 0
            transitions = StageTransition.query.filter_by(order_id=order.id).order_by(StageTransition.transitioned_at).all()
            if len(transitions) < 2: continue
            for i, t in enumerate(transitions[:-1]):
                feats = _order_features(order, current_stage=t.to_stage, hours_in_stage=0, now=t.transitioned_at)
                feats['label'] = breached
                rows.append(feats)
                next_t = transitions[i+1]
                mid_t = t.transitioned_at + (next_t.transitioned_at - t.transitioned_at) * 0.75
                hrs = (mid_t - t.transitioned_at).total_seconds() / 3600
                feats_mid = _order_features(order, current_stage=t.to_stage, hours_in_stage=hrs, now=mid_t)
                feats_mid['label'] = breached
                rows.append(feats_mid)
        if len(rows) < 100:
            return f'Only {len(rows)} training rows. Need 100+.'
        df = pd.DataFrame(rows)
        X = df.drop('label', axis=1)
        y = df['label']
        model = RandomForestClassifier(n_estimators=100, max_depth=10, min_samples_leaf=5, class_weight='balanced', random_state=42, n_jobs=-1)
        model.fit(X, y)
        _save_model(model, list(X.columns), {'rows': len(rows), 'breach_rate': float(y.mean())})
        return f'✓ Model trained on {len(rows)} rows. Breach rate: {int(y.mean()*100)}%. Saved to DB. Now visit /admin/refresh-predictions.'
    except Exception as e:
        return f'Training error: {type(e).__name__}: {str(e)}'

@app.route('/admin/refresh-predictions')
def refresh_predictions():
    # Allow access via secret token for cron jobs, OR logged-in admin
    token = request.args.get('key', '')
    if token != 'lumio-cron-2026-secret':
        if not current_user.is_authenticated or not current_user.is_admin:
            return 'Unauthorized', 403
    active = Order.query.filter(Order.current_stage != 'delivered').all()
    high_risk = []
    for order in active:
        risk, reasons = predict_risk(order)
        order.breach_risk = risk
        if risk >= 60 and not order.breach_alerted:
            high_risk.append((order, reasons))
            order.breach_alerted = True
    db.session.commit()
    # Fire WhatsApp alerts for newly high-risk
    for order, reasons in high_risk:
        if current_user.phone:
            msg = f"⚠ Lumio Alert: Order {order.order_number} at {order.breach_risk}% breach risk. Reasons: {'; '.join(reasons)}"
            send_whatsapp(current_user.phone, msg)
    return f'✓ Refreshed {len(active)} orders. {len(high_risk)} new high-risk alerts.'



@app.route('/inventory/restocking')
@login_required
def restocking_ui():
    """UI page for restocking suggestions."""
    from collections import Counter
    sixty_days_ago = datetime.utcnow() - timedelta(days=60)
    recent_orders = Order.query.filter(Order.placed_at >= sixty_days_ago).all()
    demand = Counter()
    for o in recent_orders:
        demand[(o.power_sph, o.index, o.coating, o.material)] += 1
    suggestions = []
    for (sph, idx, coat, mat), count in demand.most_common(40):
        lens = Lens.query.filter_by(power_sph=sph, index=idx, coating=coat, material=mat).first()
        monthly = count / 2
        if lens:
            if lens.stock_qty < monthly * 1.5:
                rec = int(max(monthly * 2 - lens.stock_qty, 10))
                suggestions.append({
                    'sku': lens.sku, 'current': lens.stock_qty,
                    'monthly_demand': round(monthly, 1), 'recommend': rec,
                    'reason': f'{count} orders in 60d, only {lens.stock_qty} in stock',
                    'urgency': 'high' if lens.stock_qty == 0 else 'medium'
                })
        else:
            suggestions.append({
                'sku': f"{sph:+.2f}/{idx}/{coat}/{mat}", 'current': 0,
                'monthly_demand': round(monthly, 1), 'recommend': int(max(monthly * 2, 5)),
                'reason': f'{count} orders but SKU not stocked',
                'urgency': 'high'
            })
    return render_template('restocking.html', suggestions=suggestions[:25])

@app.route('/admin/restocking-suggestions')
@login_required
def restocking_suggestions():
    """Analyze last 60 days of demand vs current stock, suggest reorders."""
    from collections import Counter
    if not current_user.is_admin:
        return jsonify({'error': 'Admin only'}), 403
    sixty_days_ago = datetime.utcnow() - timedelta(days=60)
    recent_orders = Order.query.filter(Order.placed_at >= sixty_days_ago).all()
    demand = Counter()
    for o in recent_orders:
        demand[(o.power_sph, o.index, o.coating, o.material)] += 1
    suggestions = []
    for (sph, idx, coat, mat), count in demand.most_common(30):
        lens = Lens.query.filter_by(power_sph=sph, index=idx, coating=coat, material=mat).first()
        if lens:
            # Suggest reorder if avg monthly demand > current stock / 2
            monthly = count / 2
            if lens.stock_qty < monthly * 1.5:
                recommended = int(max(monthly * 2 - lens.stock_qty, 10))
                suggestions.append({
                    'sku': lens.sku, 'current': lens.stock_qty,
                    'monthly_demand': monthly, 'recommend': recommended,
                    'reason': f'{count} orders in last 60 days vs {lens.stock_qty} in stock'
                })
        else:
            suggestions.append({
                'sku': f"{sph:+.2f}/{idx}/{coat}/{mat}", 'current': 0,
                'monthly_demand': count / 2, 'recommend': int(max(count / 2, 5)),
                'reason': f'{count} orders but no SKU exists'
            })
    return jsonify({'suggestions': suggestions[:20]})

@app.route('/order/<int:order_id>/risk')
@login_required
def order_risk(order_id):
    order = Order.query.get_or_404(order_id)
    risk, reasons = predict_risk(order)
    order.breach_risk = risk
    db.session.commit()
    return jsonify({'risk': risk, 'reasons': reasons})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=10000, debug=False)