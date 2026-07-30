# util system (migration in progress)

Skeleton + HTTP daily task. Fill `config/cfg_items.json` ids and `.env` before use.

## setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp local/names.example.json local/names.json
```

## env

| key | meaning |
|-----|---------|
| CFG_A1 | smtp user |
| CFG_A2 | smtp secret |
| CFG_A3 | smtp to |
| CFG_B1 | http base |
| CFG_B2 | tenant path segment |

## run

```bash
python scripts/scan_daily.py
python -m pytest tests/ -v
```

## notes

- `config/cfg_items.json`: codes `v01`..`v12` + numeric ids (placeholders `0` until filled)
- `local/names.json`: local-only label map (gitignored)
- state under `data/` (gitignored)
