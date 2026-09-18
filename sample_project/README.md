# Sample target project

Push this directory to its own public GitHub repo and point the agent at it:

    python -m cli.demo deploy --repo https://github.com/<you>/nomeshops-sample --instance i-...

`requirements.txt` fails on Python >= 3.10 (old numpy pin). Swap in a file from `variants/` to demo other paths.
The app must answer `GET /health` on port 8000; the agent starts it with `python -m uvicorn app:app`.
