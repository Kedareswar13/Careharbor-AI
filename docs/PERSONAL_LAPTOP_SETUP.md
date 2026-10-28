# Moving CareHarbor AI to your personal laptop and publishing it on GitHub

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

This guide covers the whole path: zip → copy → unzip → set up → run → verify → push to GitHub → what to do next. Every command is for **Windows PowerShell** on the personal laptop unless the step says otherwise.

**TL;DR**
1. On this laptop: `.\run.ps1 -Stop`, then `.\scripts\make_portable_zip.ps1` (about 10 MB, with no secrets, venvs or databases).
2. On the personal laptop: install Git, uv, Node LTS and Ollama, then pull `qwen2.5:3b` and `qwen2.5:1.5b`.
3. Unzip to `C:\dev\careharbor`, then run `.\setup.ps1`, `.\run.ps1` and the three verify commands.
4. Create an **empty** GitHub repo, run `git init -b main`, complete the pre-push checklist, then commit and push.

---

## 1. On this laptop: create the zip

```powershell
cd C:\Users\KKedareswar\ai-projects\careharbor
.\run.ps1 -Stop                         # stop CareHarbor AI so no file is locked
.\scripts\make_portable_zip.ps1         # -> C:\Users\KKedareswar\ai-projects\careharbor-portable-<timestamp>.zip
```

**What the script leaves out, and why:**

| Excluded | Why |
|---|---|
| `.venv`, `frontend/node_modules`, `frontend/dist` | Machine-specific and large. `setup.ps1` rebuilds them in minutes. |
| `.env` | **Secrets.** `setup.ps1` creates a new `.env` and a new `SESSION_SECRET`. |
| `data/*.db`, `data/index`, `data/traces`, `logs`, `.run` | Runtime state. The personal laptop starts with a fresh demo database, so chats from this laptop do not travel. |
| `.git` | The repository is created on the personal laptop (step 6). |

**Included:** source, docs, evals, the frontend source, `content/` (leaflets, MedlinePlus, policies) and `sources/` (the open Synthea dataset, about 5.4 MB, plus MedlinePlus raw files), so nothing needs downloading again.

**If you zip the folder by hand instead,** first delete or skip `.venv`, `frontend\node_modules`, `frontend\dist`, `data\*.db` and **`.env`**. A zip of hundreds of MB means `node_modules` or `.venv` got in.

Copy the zip to the personal laptop with a transfer method your employer's policy allows.

---

## 2. Personal laptop: install the tools (one time)

All of these install without admin rights, using `--scope user` where possible.

```powershell
winget install Git.Git --scope user
winget install OpenJS.NodeJS.LTS --scope user
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install Ollama.Ollama
```

Close and reopen PowerShell so the new tools are on `PATH`, then:

```powershell
git --version; node --version; uv --version; ollama --version
ollama pull qwen2.5:3b          # answer model (~1.9 GB)
ollama pull qwen2.5:1.5b        # fast classifier / fallback (~1 GB)
ollama list
```

You do **not** need Python installed (uv downloads Python 3.11 into the project) or any embedding model (retrieval is vectorless BM25).

---

## 3. Unzip in the right place

```powershell
mkdir C:\dev -Force
Expand-Archive "$HOME\Downloads\careharbor-portable-*.zip" -DestinationPath C:\dev
cd C:\dev\careharbor
Get-ChildItem -Recurse | Unblock-File   # remove the "downloaded from the internet" flag so the .ps1 scripts can run
```

Keep the project **outside OneDrive or synced folders** and on a short path (`C:\dev\careharbor`). Syncing locks SQLite files and breaks uv's file links.

---

## 4. Set up and run

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` does the following:
1. Checks for uv, Node and npm.
2. Creates the Python 3.11 `.venv` and installs dependencies.
3. Creates `.env` with a random `SESSION_SECRET`.
4. Confirms the open data is present.
5. Seeds the clinic database (only if it is missing).
6. Builds the RAG index and runs `npm install`.

It is safe to run again. Use `-Reseed` to reset the demo data.

Then open `.env` in an editor and change `STAFF_PASSWORD=change-me` to your own password.

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
# Patient chat : http://127.0.0.1:5174
# Staff console: http://127.0.0.1:5174/#/staff   (user: staff, password from .env)
.\run.ps1 -Status
.\run.ps1 -Stop
```

---

## 5. Verify everything works (about 3 minutes)

```powershell
uv run pytest -q                                   # expect: 221 passed
uv run python scripts/run_evals.py --mock          # expect: 10 suites PASS, "ALL THRESHOLDS MET"
uv run python scripts/verify_phases.py             # expect: P0..P8 PASS (P5 needs run.ps1 running)
uv run python scripts/run_evals.py                 # optional, live on Ollama: slower, real-model numbers
```

**Manual checks in the browser:**

| # | Do this | Expect |
|---|---|---|
| 1 | Ask "Do you accept Star Health insurance?" | A grounded answer with a source, and the right panel showing safety checks and the evidence map |
| 2 | **Refresh the page** | The same conversation is still there (saved on the server) and listed under *Conversations* |
| 3 | Ask "show my prescriptions" and verify with `9876500001 14/05/1990`, then the code | "Thanks, Aarav - you're verified" and the prescription card |
| 4 | Click **Sign out on this device** | The list is cleared and the status shows *Not verified* |
| 5 | Start a new chat and verify again as Aarav | **Your earlier conversations** lists the old chats; click one to re-open it |
| 6 | Type "chest pain and sweating" | An emergency card appears immediately (rules, no AI) and an urgent ticket shows in the staff console |
| 7 | Book with Dr. Rao, then Confirm | "Booked." appears and the slot shows in the staff console's appointments |

**If something fails:**

| Problem | Fix |
|---|---|
| `running scripts is disabled` | Use `powershell -ExecutionPolicy Bypass -File .\setup.ps1`, or run `Unblock-File` (step 3) |
| `uv venv failed`, or setup stops at the venv step | Run `.\setup.ps1` again (it repairs a broken `.venv`). Check the folder is not inside OneDrive. |
| The UI shows *Offline* | Check `.\run.ps1 -Status` and `logs\api.log`. Check that port 8010 is free: `Get-NetTCPConnection -LocalPort 8010` |
| Answers say the model is unavailable | Run `ollama list` (both models should be listed) and `ollama serve` if Ollama isn't running |
| Ports 8010 or 5174 are already used | `.\run.ps1 -ApiPort 8020 -UiPort 5180`, and set `API_PORT` / `UI_PORT` in `.env` to match |

---

## 6. Push to GitHub

### 6.1 Create the repository

On github.com, go to **New repository**. Name it `careharbor` and make it Public. Do **not** add a README, .gitignore or license, because the project already has them and an empty repo avoids merge conflicts.

### 6.2 First commit

```powershell
cd C:\dev\careharbor
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"     # the email on your GitHub account
git init -b main
git add .
```

### 6.3 Pre-push checklist (run it, don't skip it)

```powershell
git status --short | Select-String '\.env$'                                  # must print NOTHING
git ls-files | Select-String '\.db$|node_modules|\.venv/|frontend/dist'       # must print NOTHING
git ls-files sources | Select-Object -First 5                                 # MUST list the Synthea zip + MedlinePlus files
git grep -nE "gsk_[A-Za-z0-9]{20}|hf_[A-Za-z0-9]{20}|sk-[A-Za-z0-9]{20}|SESSION_SECRET=[A-Za-z0-9]{20}"   # must print NOTHING
```

If any check fails, fix `.gitignore`, run `git rm --cached <file>`, and run the checks again.

### 6.4 Commit and push

```powershell
git commit -m "feat: CareHarbor AI - AI front desk for a primary-care clinic"
git remote add origin https://github.com/<your-user>/careharbor.git
git push -u origin main          # Git Credential Manager opens a browser to sign in the first time
```

The included GitHub Actions workflow (`.github/workflows/evals.yml`) then runs the tests, the mock eval gate and the UI build on every push. Check the **Actions** tab shows a green tick.

### 6.5 Make the repository presentable

- **About:** "AI front desk for a primary-care clinic: LangGraph agents, MCP tools, vectorless + advanced RAG, guardrails, evals". Add topics such as `langgraph`, `rag`, `mcp`, `llm-agents`, `healthcare-ai`, `fastapi`, `react`.
- **Screenshots:** take one each of the patient workspace, the evidence panel and the staff console. Put them in `docs/screenshots/` and add them near the top of `README.md`.
- **Pin** the repository on your GitHub profile.

---

## 7. After development is done: the finish line

1. **Tag the release:** run `git tag v1.0.0`, then `git push --tags`.
2. **Deploy a public demo:** follow [DEPLOYMENT.md](DEPLOYMENT.md). An Oracle Cloud Always Free VM keeps chats persistent. Put the URL in the README and the repo *About*.
3. **Record a 2-minute demo video** showing:
   - an emergency message;
   - a grounded FAQ answer with its evidence panel;
   - verification and a booking with Confirm;
   - a refresh that keeps the chat;
   - the staff console.
4. **Prepare to explain it:** read [PROJECT_EXPLAINED.md](PROJECT_EXPLAINED.md), then [ADVANCED_RAG_CONCEPTS.md](ADVANCED_RAG_CONCEPTS.md) (the 12 interview questions), [ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md), and work through [LEARNING_ROADMAP.md](LEARNING_ROADMAP.md) module by module.
5. **Keep it green:** after any change, run `uv run pytest -q` and `uv run python scripts/run_evals.py --mock` before pushing. CI runs them too.
6. **Day-to-day loop:** edit, test, then `git add -A`, `git commit -m "fix: ..."` and `git push`. On the server, run `git pull && docker compose up -d --build`.
