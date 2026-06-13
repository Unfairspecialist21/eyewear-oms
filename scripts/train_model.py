"""
Lumio ML model — trains breach prediction on historical order data.
"""
import os, pickle
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
from datetime import datetime, timedelta

MODEL_PATH = os.environ.get('MODEL_PATH', '/tmp/lumio_model.pkl')

INDEX_ENCODE    = {1.50:0, 1.56:1, 1.60:2, 1.67:3, 1.74:4}
COATING_ENCODE  = {'none':0, 'AR':1, 'blue-cut':2, 'photochromic':3}
MATERIAL_ENCODE = {'CR-39':0, 'poly':1, 'trivex':2, 'high-index':3}
LENS_TYPE_ENCODE = {'single-vision':0, 'bifocal':1, 'progressive':2}
PATH_ENCODE     = {'A':0, 'B':1}

def _stages():
    from app import STAGES
    return STAGES

def _sla():
    from app import SLA_HOURS
    return SLA_HOURS

def stage_to_int(s):
    try: return _stages().index(s)
    except ValueError: return -1

def extract_features(order, current_stage=None, hours_in_stage=None, now=None):
    from app import StageTransition
    if now is None: now = datetime.utcnow()
    if current_stage is None: current_stage = order.current_stage
    hours_since_placed = (now - order.placed_at).total_seconds() / 3600
    if hours_in_stage is None:
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

def build_training_data():
    from app import Order, StageTransition
    sla_map = _sla()
    rows = []
    delivered = Order.query.filter_by(current_stage='delivered').all()
    for order in delivered:
        sla_h = sla_map.get(order.fulfilment_path, 120)
        actual = (order.delivered_at - order.placed_at).total_seconds() / 3600 if order.delivered_at else None
        if actual is None: continue
        breached = 1 if actual > sla_h else 0
        transitions = StageTransition.query.filter_by(order_id=order.id).order_by(StageTransition.transitioned_at).all()
        if len(transitions) < 2: continue
        for i, t in enumerate(transitions[:-1]):
            features = extract_features(order, current_stage=t.to_stage, hours_in_stage=0, now=t.transitioned_at)
            features['label'] = breached
            rows.append(features)
            next_t = transitions[i+1]
            mid_time = t.transitioned_at + (next_t.transitioned_at - t.transitioned_at) * 0.75
            hours_in = (mid_time - t.transitioned_at).total_seconds() / 3600
            features_mid = extract_features(order, current_stage=t.to_stage, hours_in_stage=hours_in, now=mid_time)
            features_mid['label'] = breached
            rows.append(features_mid)
    return pd.DataFrame(rows)

def train():
    df = build_training_data()
    if len(df) < 100:
        return None
    X = df.drop('label', axis=1)
    y = df['label']
    model = RandomForestClassifier(n_estimators=100, max_depth=10, min_samples_leaf=5,
        class_weight='balanced', random_state=42, n_jobs=-1)
    model.fit(X, y)
    feature_columns = list(X.columns)
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump({'model': model, 'features': feature_columns}, f)
    return model, feature_columns

def load_model():
    if not os.path.exists(MODEL_PATH):
        return None, None
    with open(MODEL_PATH, 'rb') as f:
        data = pickle.load(f)
    return data['model'], data['features']

def predict_breach_risk(order):
    from app import SLA_HOURS
    model, feature_cols = load_model()
    if model is None:
        return 0, ['Model not trained yet']
    features = extract_features(order)
    X = pd.DataFrame([features])[feature_cols]
    prob = model.predict_proba(X)[0][1]
    risk = int(prob * 100)
    reasons = []
    if features['path'] == 1:
        reasons.append("Sourced fulfilment path (slower)")
    if features['is_premium_lens']:
        reasons.append(f"Premium index ({order.index}) — takes longer")
    if features['is_premium_coat']:
        reasons.append("Photochromic coating adds time")
    if features['hours_in_stage'] > 12:
        reasons.append(f"In {order.current_stage} for {int(features['hours_in_stage'])}h")
    if features['hours_since_placed'] > SLA_HOURS[order.fulfilment_path] * 0.6:
        reasons.append("Past 60% of SLA time")
    if features['day_of_week'] >= 4:
        reasons.append("Friday/weekend placement")
    if not reasons:
        reasons.append("Multiple soft signals")
    return risk, reasons[:3]
