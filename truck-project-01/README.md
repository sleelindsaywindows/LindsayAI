# Lindsay Windows — Truck Route Optimizer

Assigns customer window orders to delivery trucks and sequences stops. Configurable per plant — the depot address and truck fleet are set in config.yaml, so the same app runs for any Lindsay Windows location. Initial pilot is the Georgia plant.

---

## Setup

1. Install dependencies

```
pip install -r requirements.txt
```

2. Set up your API key (see section below)

3. Run the app

```
streamlit run app.py
```

---

## API Key Setup

The app uses two files for the API key. They look similar but serve different purposes — read this before pasting anything.

**.env.example**
This is a template. It is committed to git so everyone on the team can see the required format. It contains a placeholder, not a real key. Do not paste your real key here.

**.env**
This is where your real key goes. It is never committed to git (it is listed in .gitignore). This file stays on your machine only.

**Steps to set it up:**

1. Copy the template to create your local file
```
cp .env.example .env
```

2. Open the .env file
```
open -e .env
```

3. Replace `sk-ant-...` with your real Anthropic API key, then save and close

The app will pick it up automatically on next launch. If the key is missing or incorrect, the natural language parsing feature will be disabled but CSV upload and route optimization will still work.

---

## What the App Does

Drop in a CSV of customer orders and it assigns them to trucks, sequences the delivery stops, and generates a load plan showing the order windows go onto the truck (LIFO — last delivery loads first).

CSV format required: order_id, customer_name, address, capacity_units, priority, notes

---

## Fleet and Depot Config

Truck names, capacities, and the depot address are all editable in the sidebar without touching any code. Changes save to config.yaml automatically.

To set up a new plant, update the depot address in the sidebar to that location's warehouse address. The truck fleet can be adjusted to match whatever that plant is running. No code changes needed.

---

## For Questions

See CLAUDE.md for full architecture documentation, data schema, and the development roadmap.
