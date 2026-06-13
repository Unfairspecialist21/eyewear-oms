"""
Lumio ML model — trains breach prediction on historical order data.
Saves model to /tmp/lumio_model.pkl (writable on Render free tier).
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import LabelEncoder
from datetime import datetime, timedelta

from app import app, db, Order, StageTransition, STAGES, SLA_HOURS

MODEL_PATH = os.environ.get('MODEL_PATH', '/tmp/lumio_model.pkl')

# ─── Feature engineering ───────────────────────────────────────────────────────
INDEX_ENCODE    = {1.50:0, 1.56:1, 1.60:2, 1.67:3, 1.74:4}
COATING_ENCODE  = {'none':0, 'AR':1, 'blue-cut':2, 'photochromic':3}
MATERIAL_ENCODE = {'CR-39':0, 'poly':1, 'trivex':2, 'high-index':3}
LENS_TYPE_ENCODE = {'single-vision':0, 'bifocal':1, 'progressive':2}
PATH_ENCODE     = {'A':0, 'B':1}

def stage_to_int(s):
    try: return STAGES.index(s)
    except ValueError: return -1

def extract_features(order, current_stage=None, hours_in_stage=None, now=None):
    """Extract features for one order. Used both for training and prediction."""
    if now is None: now = datetime.utcnow()
    if current_stage is None: current_stage = order.current_stage
    hours_since_placed = (now - order.placed_at).total_seconds() / 3600
    if hours_in_stage is None:
        # Compute from transitions
        last_t = StageTransition.query.filter_by(order_id=order.id, to_stage=current_stage).order_by(StageTransition.transitioned_at.desc()).first()
        hours_in_stage = (now - last_t.transitioned_at).total_seconds() / 3600 if last_t else 0
    return {
        'power_sph_abs':    abs(order.power_sph),
        'power_cyl_abs':    abs(order.power_cyl or 0),
        'index':            INDEX_ENCODE.get(order.index, 0),
        'coating':          COATING_ENCODE.get(order.coating, 0),
        'material':         MATERIAL_ENCODE.get(order.material, 0),
        'lens_type':        LENS_TYPE_ENCODE.get(order.lens_type, 0),
        'path':             PATH_ENCODE.get(order.fulfilment_path, 0),
        'stage':            stage_to_int(current_stage),
        'hours_since_placed': hours_since_placed,
        'hours_in_stage':   hours_in_stage,
        'day_of_week':      order.placed_at.weekday(),
        'hour_of_day':      order.placed_at.hour,
        'is_premium_lens':  1 if order.index >= 1.67 else 0,
        'is_premium_coat':  1 if order.coating == 'photochromic' else 0,
    }

# ─── Training ──────────────────────────────────────────────────────────────────
def build_training_data():
    """For each historical (delivered) order, generate training rows at each stage."""
    print("Building training dataset from historical orders...")
    rows = []
    delivered_orders = Order.query.filter_by(current_stage='delivered').all()
    print(f"  Found {len(delivered_orders)} historical orders")

    for order in delivered_orders:
        sla_hours = SLA_HOURS[order.fulfilment_path]
        actual_hours = (order.delivered_at - order.placed_at).total_seconds() / 3600 if order.delivered_at else None
        if actual_hours is None: continue
        breached = 1 if actual_hours > sla_hours else 0

        # Generate one training row per stage of this order
        transitions = StageTransition.query.filter_by(order_id=order.id).order_by(StageTransition.transitioned_at).all()
        if len(transitions) < 2: continue

        for i, t in enumerate(transitions[:-1]):
            # Feature snapshot when entering this stage
            now_snapshot = t.transitioned_at
            current_stage = t.to_stage
            # hours_in_stage at this point is 0 (just entered)
            features = extract_features(order, current_stage=current_stage, hours_in_stage=0, now=now_snapshot)
            features['label'] = breached
            rows.append(features)

            # Also a snapshot at mid-stage (75% through)
            next_t = transitions[i+1]
            mid_time = t.transitioned_at + (next_t.transitioned_at - t.transitioned_at) * 0.75
            hours_in = (mid_time - t.transitioned_at).total_seconds() / 3600
            features_mid = extract_features(order, current_stage=current_stage, hours_in_stage=hours_in, now=mid_time)
            features_mid['label'] = breached
            rows.append(features_mid)

    df = pd.DataFrame(rows)
    print(f"  Generated {len(df)} training rows")
    print(f"  Breach distribution: {df['label'].value_counts().to_dict()}")
    return df

def train():
    df = build_training_data()
    if len(df) < 100:
        print(f"⚠ Only {len(df)} training rows — model may be weak")
        return None

    X = df.drop('label', axis=1)
    y = df['label']

    # Handle class imbalance with class_weight
    model = RandomForestClassifier(
        n_estimators=100, max_depth=10, min_samples_leaf=5,
        class_weight='balanced', random_state=42, n_jobs=-1
    )

    # Cross-validation
    scores = cross_val_score(model, X, y, cv=5, scoring='roc_auc')
    print(f"\nCross-validation ROC-AUC: {scores.mean():.3f} (+/- {scores.std():.3f})")

    # Train on full data for final model
    model.fit(X, y)

    # Feature importance
    importance = pd.DataFrame({
        'feature': X.columns,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False)
    print(f"\nTop features driving predictions:")
    print(importance.head(8).to_string(index=False))

    # Save model
    feature_columns = list(X.columns)
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump({'model': model, 'features': feature_columns}, f)
    print(f"\n✓ Model saved to {MODEL_PATH}")
    return model, feature_columns

# ─── Prediction ────────────────────────────────────────────────────────────────
def load_model():
    if not os.path.exists(MODEL_PATH):
        return None, None
    with open(MODEL_PATH, 'rb') as f:
        data = pickle.load(f)
    return data['model'], data['features']

def predict_breach_risk(order):
    """Returns breach probability (0-100) and top reasons for an active order."""
    model, feature_cols = load_model()
    if model is None:
        return 0, ['Model not trained yet']

    features = extract_features(order)
    X = pd.DataFrame([features])[feature_cols]
    prob = model.predict_proba(X)[0][1]
    risk = int(prob * 100)

    # Generate explanation — which features drove this prediction
    reasons = []
    if features['path'] == 1:
        reasons.append("Sourced fulfilment path (slower)")
    if features['is_premium_lens']:
        reasons.append(f"Premium index ({order.index}) — takes longer to coat")
    if features['is_premium_coat']:
        reasons.append("Photochromic coating adds time")
    if features['hours_in_stage'] > 12:
        reasons.append(f"Stuck in {order.current_stage} for {int(features['hours_in_stage'])}h")
    if features['hours_since_placed'] > SLA_HOURS[order.fulfilment_path] * 0.6:
        reasons.append("Past 60% of SLA time")
    if features['day_of_week'] >= 4:
        reasons.append("Friday/weekend placement (slower processing)")

    if not reasons:
        reasons.append("Multiple soft signals from order pattern")

    return risk, reasons[:3]  # top 3 reasons

if __name__ == '__main__':
    with app.app_context():
        train()
