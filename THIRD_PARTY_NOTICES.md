# Third-party notices

The staff console vendors two browser libraries:

- `app/web/static/ag-grid-community.min.js` — AG Grid Community, copyright AG Grid Ltd., MIT License.
- `app/web/static/ag-charts-community.min.js` — AG Charts Community, copyright AG Grid Ltd., MIT License.

Python dependencies are pinned in `requirements.txt` and `requirements-dev.txt`. The optional PayPal Agent Toolkit pin is `paypal-agent-toolkit==1.11.0` in `pyproject.toml`. The demo runs without that extra install.

PayPal JavaScript SDK v6 is loaded from PayPal on the checkout page. It is not included in this repository.
