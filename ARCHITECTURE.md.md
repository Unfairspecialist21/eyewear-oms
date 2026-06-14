# Lumio — Architecture Note

**AI-powered Order Management System for an eyewear brand**
Live: https://eyewear-oms.onrender.com · Repo: github.com/Unfairspecialist21/eyewear-oms

## What it does
Ingests eyewear orders from any source (website, store, marketplace API), automatically decides whether to fulfill from in-house inventory or source from suppliers, tracks every order through 10 lifecycle stages, and uses a machine-learning model to **predict SLA breaches before they happen** — alerting operations via WhatsApp.

## System architecture
```
Customers / Stores / Marketplaces  →  Flask API  →  PostgreSQL (Supabase)
                                            ↓
                                  Random Forest (sklearn)
                                            ↓
                                   Twilio WhatsApp alerts
```

| Layer | Technology | Why |
|---|---|---|
| Backend | Flask 3.0 + SQLAlchemy | Lightweight, fast iteration on ops dashboards |
| Database | PostgreSQL via Supabase | Managed, free tier, IPv4 pooler compatible with Render |
| ML | scikit-learn (Random Forest Classifier) | Best for small tabular data, interpretable, robust |
| Frontend | Jinja2 + vanilla JS, dark/light theme via CSS variables | No JS framework overhead; clean Palantir-style UI |
| Hosting | Render (Singapore) | Free tier, auto-deploys from GitHub |
| Notifications | Twilio WhatsApp API | Real WhatsApp delivery to ops team + customers |
| Scheduler | cron-job.org (hourly) | Refreshes predictions automatically |

## ML model — TAT Prediction
**Algorithm:** Random Forest Classifier (100 trees, max depth 10, class-balanced)
**Why Random Forest:** Our problem is tabular, structured, with non-linear feature interactions and only 450 historical orders. Random Forest handles all of this naturally without feature scaling, captures interactions automatically through tree splits, and outputs calibrated probabilities. Linear regression can't capture interactions (premium lens AND sourced AND stuck creates exponential risk). Neural networks would overfit on this data size. XGBoost is a strong alternative but requires extensive tuning — Random Forest just works.

**Features (11):** lens power, index, premium flag, photochromic flag, material, fulfilment path, current stage, hours since placed, hours in stage, day of week, hour of day.

**Training strategy:** For each delivered order, generate multiple snapshots (entry to each stage + 75% through each stage) — augments dataset 8-10x from 450 orders to ~5,000 training rows. Trained model is serialized with pickle and persisted in Supabase (`ModelStore` table), surviving redeploys.

**Prediction explanations:** When the model returns 70% risk, it also surfaces the top reasons (e.g. "Sourced path + premium 1.74 index + stuck in coating 18h"). This makes alerts actionable — operations knows WHY the order is at risk and can intervene.

**Cross-validation ROC-AUC: ~0.82** on the synthetic dataset. In production with real Eluno data, the same code retrains on actual orders.

## Three modules (matching assignment requirements)
1. **Inventory Management** — Tracks SKUs by power/index/coating/material. Auto-decides "in-stock" (Path A, 5-day SLA) vs "sourced" (Path B, 14-day SLA) at order time. AI-driven restocking suggestions analyze 60-day demand vs current stock.
2. **Status Dashboard** — Horizontal flow chart with live counts per stage. Click any stage to drill into filterable order list (path, store, risk, search). Operations updates status via Kanban-style transitions. Forward moves are role-permitted; backward moves require QC/admin role plus a mandatory reason, and automatically reset SLA + notify the customer via WhatsApp.
3. **Breach Prediction & Alerts** — ML model continuously scores every active order. Three-tier alerts: 30-60% (dashboard amber), 60-80% (operations WhatsApp), 80%+ (escalation). Customers also receive WhatsApp notifications on rollback with revised ETA.

## Honest limitations
- Synthetic training data — production deployment would need 3-6 months of real Eluno orders before model is fully reliable
- WhatsApp sandbox requires opt-in (production needs Meta Business approval)
- No supplier reliability features yet — adding per-supplier track records would improve predictions
- Single-tenant architecture (one organization)

## Why no LLM API
We deliberately did not use Claude/GPT here. Breach prediction is a classification problem — Random Forest does it in 10ms locally with interpretable outputs and zero cost per inference. An LLM would be slower, more expensive, less interpretable, and the wrong tool for the job. LLMs would make sense for parsing unstructured prescription images (future enhancement), but not for predicting SLA breaches.
