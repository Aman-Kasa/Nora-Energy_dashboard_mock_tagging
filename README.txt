Nora Energy Dashboard (Mock - Tagging Split)

What this adds
- Tenant split: Utility kWh vs Generator kWh using the agreed tagging rule:
  If generator main meter increases in an interval => tag tenant delta as GENERATOR, else UTILITY.

Run on PC:
  python -m venv venv
  # activate venv
  pip install -r requirements.txt
  python webapp.py
  open http://127.0.0.1:5000/dashboard

DB:
- ems.db included (sample data)
- Meter IDs: Utility=10, Generator=21, Tenant(Supermarket)=13
