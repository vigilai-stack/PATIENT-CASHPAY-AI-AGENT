.PHONY: up down demo test lint reset smoke media notebook
up:        ; docker compose up --build
down:      ; docker compose down
reset:     ; docker compose down -v
demo:      ; python scripts/demo.py
test:      ; pytest -q --cov=app
lint:      ; ruff check . && ruff format --check . && python scripts/doc_lint.py
smoke:     ; python scripts/sandbox_smoke.py
media:     ; python scripts/media/screens.py
notebook:  ; jupyter nbconvert --to notebook --execute --inplace notebooks/walkthrough.ipynb
