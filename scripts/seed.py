"""
Lumio seed script — populates the database with realistic synthetic data.
Run once after fresh deployment to give the dashboard, ML model, and demo something to chew on.

Usage:
    python scripts/seed.py
"""
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, date
from app import app, db, User, Lens, Order, StageTransition, STAGES, calculate_promised_date, generate_order_number
from werkzeug.security import generate_password_hash

random.seed(42)  # reproducible

# ─── Lens SKU catalogue ────────────────────────────────────────────────────────
LENS_POWERS    = [round(x * 0.25, 2) for x in range(-32, 33)]  # -8.00 to +8.00 step 0.25
LENS_INDEXES   = [1.50, 1.56, 1.60, 1.67, 1.74]
COATINGS       = ['none', 'AR', 'blue-cut', 'photochromic']
MATERIALS      = ['CR-39', 'poly', 'trivex', 'high-index']
LENS_TYPES     = ['single-vision', 'bifocal', 'progressive']
FRAME_MODELS   = ['Lumio Aria', 'Lumio Steel', 'Lumio Verve', 'Lumio Pulse', 'Lumio Noir', 'Lumio Crest']
STORE_LOCATIONS = ['Bangalore HQ', 'Mumbai', 'Delhi', 'Chennai', 'Hyderabad']
SOURCES        = ['website', 'store', 'marketplace']

# Common SKUs that should be in stock (Pareto: 20% of SKUs handle 80% of demand)
COMMON_POWERS  = [-2.00, -1.50, -1.00, -0.50, +0.50, +1.00, +1.50, +2.00, +2.50, -2.50, -3.00]
COMMON_INDEXES = [1.50, 1.56, 1.60]
COMMON_COATINGS = ['none', 'AR', 'blue-cut']

def seed_lenses():
    """Create ~80 lens SKUs — common ones with stock, rare ones with low/no stock."""
    print("Seeding lens inventory...")
    created = 0
    for power in COMMON_POWERS:
        for idx in COMMON_INDEXES:
            for coat in COMMON_COATINGS:
                lens = Lens(
                    power_sph=power, power_cyl=0.0, index=idx, coating=coat,
                    material='CR-39' if idx <= 1.56 else 'poly' if idx == 1.60 else 'high-index',
                    lens_type='single-vision',
                    stock_qty=random.randint(15, 80),
                    reorder_at=10,
                    supplier=random.choice(['Supplier-A', 'Supplier-B']),
                    lead_time=random.choice([7, 9, 11])
                )
                db.session.add(lens)
                created += 1

    # Add a few rare/premium SKUs (low stock, longer lead time)
    rare_combos = [
        (-4.00, 1.74, 'photochromic'), (-5.50, 1.67, 'AR'),
        (+4.00, 1.67, 'photochromic'), (-6.00, 1.74, 'blue-cut'),
        (-7.00, 1.74, 'AR'), (+5.00, 1.74, 'photochromic'),
    ]
    for power, idx, coat in rare_combos:
        lens = Lens(
            power_sph=power, power_cyl=0.0, index=idx, coating=coat,
            material='high-index', lens_type='single-vision',
            stock_qty=random.randint(0, 5),
            reorder_at=5,
            supplier='Supplier-Premium',
            lead_time=14
        )
        db.session.add(lens)
        created += 1
    db.session.commit()
    print(f"  ✓ {created} lens SKUs created")

# ─── Historical orders ─────────────────────────────────────────────────────────
def realistic_stage_duration(stage, lens_idx, coating, material):
    """Returns realistic hours for a stage, varied by lens characteristics."""
    base = {
        'placed':    0.1, 'verified': 2, 'sourcing': 8,
        'cutting':   3, 'coating': 5, 'qc1': 0.5,
        'fitting':   2, 'qc2': 0.5, 'shipped': 12, 'delivered': 24
    }[stage]

    # Premium lens index = longer coating/cutting
    if stage in ['cutting', 'coating']:
        if lens_idx >= 1.67: base *= 1.6
        if coating == 'photochromic': base *= 1.4
    if material == 'high-index' and stage == 'cutting':
        base *= 1.3

    return base * random.uniform(0.7, 1.5)

def seed_historical_orders(count=500):
    """Create completed orders spread over last 90 days, with realistic stage transitions."""
    print(f"Seeding {count} historical orders...")
    lenses = Lens.query.all()
    if not lenses:
        print("  ⚠ no lenses found — run lens seeder first")
        return

    now = datetime.utcnow()
    created = 0
    for i in range(count):
        # Placed time: anywhere in last 90 days
        placed_offset = random.uniform(0, 90 * 24)  # hours
        placed_at     = now - timedelta(hours=placed_offset)

        # Pick lens spec
        lens = random.choice(lenses)
        is_in_stock = lens.stock_qty > 0
        path = 'A' if is_in_stock else 'B'

        order = Order(
            order_number    = f"ORD-HIST-{i+1:05d}",
            source          = random.choice(SOURCES),
            store_location  = random.choice(STORE_LOCATIONS),
            customer_name   = f"Customer {i+1}",
            customer_phone  = f"+9198{random.randint(10000000, 99999999)}",
            customer_email  = f"customer{i+1}@example.com",
            power_sph       = lens.power_sph,
            power_cyl       = lens.power_cyl,
            index           = lens.index,
            coating         = lens.coating,
            material        = lens.material,
            lens_type       = lens.lens_type,
            frame_model     = random.choice(FRAME_MODELS),
            fulfilment_path = path,
            matched_lens_id = lens.id if is_in_stock else None,
            current_stage   = 'delivered',
            placed_at       = placed_at,
            promised_at     = placed_at + timedelta(hours=5*24 if path == 'A' else 14*24),
        )
        db.session.add(order)
        db.session.flush()

        # Simulate stage progression
        stage_time = placed_at
        prev_stage = None
        for stage in STAGES:
            duration = realistic_stage_duration(stage, lens.index, lens.coating, lens.material)
            # Add sourcing time for path B
            if stage == 'sourcing' and path == 'B':
                duration = lens.lead_time * 24 * random.uniform(0.9, 1.3)

            # 5% chance of QC failure at qc1 → loops back, adds delay
            if stage == 'qc1' and random.random() < 0.05:
                duration += random.uniform(24, 72)  # delay from re-work

            transition = StageTransition(
                order_id        = order.id,
                from_stage      = prev_stage,
                to_stage        = stage,
                transitioned_at = stage_time + timedelta(hours=duration),
                notes           = 'Auto-seeded'
            )
            db.session.add(transition)
            stage_time = stage_time + timedelta(hours=duration)
            prev_stage = stage

        order.delivered_at = stage_time
        created += 1

        if created % 100 == 0:
            db.session.commit()
            print(f"  ... {created} done")

    db.session.commit()
    print(f"  ✓ {created} historical orders created")

# ─── Active demo orders ────────────────────────────────────────────────────────
def seed_active_orders(count=25):
    """Create active orders distributed across stages with realistic ages."""
    print(f"Seeding {count} active orders...")
    lenses = Lens.query.all()
    now    = datetime.utcnow()

    # Skew distribution: more in early stages, fewer at end
    stage_weights = {
        'placed':1, 'verified':2, 'sourcing':4, 'cutting':5, 'coating':4,
        'qc1':2, 'fitting':3, 'qc2':2, 'shipped':2, 'delivered':0
    }
    stage_choices = []
    for s, w in stage_weights.items():
        stage_choices.extend([s] * w)

    for i in range(count):
        lens          = random.choice(lenses)
        is_in_stock   = lens.stock_qty > 0
        path          = 'A' if is_in_stock else 'B'
        current_stage = random.choice(stage_choices)

        # Place time depends on stage — later stages should have been placed earlier
        stage_idx = STAGES.index(current_stage)
        days_ago  = random.uniform(stage_idx * 0.3, stage_idx * 0.7 + 1)
        placed_at = now - timedelta(days=days_ago)

        order = Order(
            order_number    = f"ORD-{now.strftime('%Y%m%d')}-{(i+1):04d}",
            source          = random.choice(SOURCES),
            store_location  = random.choice(STORE_LOCATIONS),
            customer_name   = f"Active Customer {i+1}",
            customer_phone  = f"+9198{random.randint(10000000, 99999999)}",
            customer_email  = f"active{i+1}@example.com",
            power_sph       = lens.power_sph,
            power_cyl       = lens.power_cyl,
            index           = lens.index,
            coating         = lens.coating,
            material        = lens.material,
            lens_type       = lens.lens_type,
            frame_model     = random.choice(FRAME_MODELS),
            fulfilment_path = path,
            matched_lens_id = lens.id if is_in_stock else None,
            current_stage   = current_stage,
            placed_at       = placed_at,
            promised_at     = placed_at + timedelta(hours=5*24 if path == 'A' else 14*24),
        )
        db.session.add(order)
        db.session.flush()

        # Add transitions for stages already passed
        stage_time = placed_at
        prev = None
        for s in STAGES[:stage_idx + 1]:
            duration = realistic_stage_duration(s, lens.index, lens.coating, lens.material)
            if s == 'sourcing' and path == 'B':
                duration = lens.lead_time * 24 * random.uniform(0.9, 1.3)
            t = StageTransition(
                order_id        = order.id,
                from_stage      = prev,
                to_stage        = s,
                transitioned_at = stage_time + timedelta(hours=duration),
                notes           = 'Seeded active order'
            )
            db.session.add(t)
            stage_time = stage_time + timedelta(hours=duration)
            prev = s

    db.session.commit()
    print(f"  ✓ {count} active orders created")

# ─── Demo user ─────────────────────────────────────────────────────────────────
def seed_users():
    if User.query.count() > 0:
        print("Users already exist — skipping demo user creation")
        return
    print("Creating demo admin user...")
    admin = User(
        name='Demo Admin',
        email='admin@lumio.app',
        password=generate_password_hash('lumio123'),
        role='admin',
        is_admin=True,
        phone=None
    )
    db.session.add(admin)
    db.session.commit()
    print(f"  ✓ Demo admin: email=admin@lumio.app | password=lumio123")

# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Clean slate option — uncomment if you want a fresh start
        # StageTransition.query.delete()
        # Order.query.delete()
        # Lens.query.delete()
        # db.session.commit()

        seed_users()
        seed_lenses()
        seed_historical_orders(count=500)
        seed_active_orders(count=25)
        print("\n✓ Seed complete.")
        print(f"  Users:        {User.query.count()}")
        print(f"  Lens SKUs:    {Lens.query.count()}")
        print(f"  Orders:       {Order.query.count()}")
        print(f"  Transitions:  {StageTransition.query.count()}")
