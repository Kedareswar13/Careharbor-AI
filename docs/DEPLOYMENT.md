# Deploying CareHarbor AI

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

This guide takes CareHarbor AI from your laptop to a public HTTPS URL using free tiers only.

**TL;DR**
- **Recommended:** one Oracle Cloud *Always Free* VM running `docker compose up -d --build`. That starts CareHarbor AI, Ollama (the same local models) and Caddy (automatic HTTPS). Chats, bookings and memory live on a persistent volume, so they survive restarts and redeploys.
- **Quickest:** Hugging Face Spaces (Docker) with Groq as the LLM. It is easy, but free Spaces have no persistent disk, so saved chats reset whenever the Space restarts.
- **Not tested on the build laptop:** the Docker image and compose stack (no Docker engine there). Everything inside them was tested locally: `seed.py --if-missing`, uvicorn, `/data` paths and the health check. The GitHub Actions job `docker-image` also builds the image on every push, starts it, chats, restarts it and checks the history survived. A green tick on your first push proves the image works.

| Option | Cost | Persistent chats | LLM | Effort |
|---|---|---|---|---|
| A. Local laptop (`run.ps1`) | free | yes (`data/`) | Ollama on your laptop | done |
| **B. Oracle Cloud VM + Docker Compose** | free (Always Free) | **yes** (Docker volume) | Ollama in a container, or Groq | ~45 min |
| C. Hugging Face Spaces + Groq | free | no, unless you add paid persistent storage | Groq (free tier) | ~20 min |

### Can I really deploy this for free? Yes, with these catches

| Path | Card needed? | Catches you should know |
|---|---|---|
| **Oracle Cloud Always Free (B)** | Yes, for identity verification. Always Free resources are not charged. | - Arm (A1) capacity is often "out of host capacity" in popular regions: retry later, try another availability domain, or start with 2 OCPU / 12 GB.<br>- Oracle may **reclaim idle** Always Free VMs (very low CPU, network and memory use over 7 days). A demo that gets visits and keeps Ollama loaded is normally fine. |
| **Google Cloud e2-micro (B with Groq)** | Yes | The always-free e2-micro has 1 GB RAM: too small for Ollama, fine for CareHarbor AI + Groq. Add 2 GB swap. Use the same compose file with `LLM_BASE_URL_DOCKER` set to Groq and the `ollama` service removed or stopped. 30 GB of persistent disk is included. |
| **Hugging Face Spaces + Groq (C)** | **No** | Chats reset on restart (ephemeral disk). The free CPU Space sleeps after inactivity, so the first visit takes about 30-60 s. Groq free-tier rate limits are enough for a demo. |
| Render / Koyeb free tiers | No | Small RAM, sleeping instances, no free persistent disk. Same trade-off as Hugging Face, with less RAM. |

**Recommendation:**
- If you can add a card for verification, use **Oracle (B)**. It is the only free option that runs the *same* local models and keeps chats.
- If not, use **Hugging Face + Groq (C)** for the public link, and say in the README that saved chats reset on restarts of the demo host. The persistence feature is still proven by the tests and CI (the container restart check).
- Never put a paid API key in the repo. Every key goes in `.env` or the host's secret settings.

---

## 0. Before any public deployment (do not skip)

| Setting (`.env`) | Why | How |
|---|---|---|
| `APP_ENV=prod` | Enables production behaviour. Staff login is **disabled** while the password is still `change-me`. | edit `.env` |
| `SESSION_SECRET` | Signs chat session ids, the staff cookie, OTP hashes and memory keys. If it changes or is missing, every saved chat link stops working and everyone is signed out. | `openssl rand -hex 32` (Linux) or `python -c "import secrets;print(secrets.token_hex(32))"` |
| `STAFF_PASSWORD` | Protects the staff console, the audit log and the `/mcp` tool server. | a long random password |
| `DEMO_MODE=true` | There is no SMS gateway, so the one-time code is shown on screen. Keep it on for a public demo, and say so. | default |
| Data | Synthetic and open data only. Never put real patient data into this demo. | — |

How the app is served: the API serves the built React UI from `frontend/dist` on the same port. That means one origin and one container: no Vercel and no CORS setup.

---

## A. Local (as built)

```powershell
.\setup.ps1        # keeps existing data; use -Reseed to reset the demo (deletes bookings and saved chats)
.\run.ps1          # UI http://127.0.0.1:5174, API http://127.0.0.1:8010
.\run.ps1 -Stop
```

---

## B. Oracle Cloud Always Free VM (recommended)

The Always Free tier includes Arm (Ampere A1) capacity of up to 4 OCPU and 24 GB RAM, which is enough to run `qwen2.5:3b` on CPU. Free-tier rules change, so check Oracle's current Always Free page when you sign up.

### B1. Create the VM

1. Sign up at cloud.oracle.com and choose your home region. A card is needed for identity checks; Always Free resources are not charged.
2. Go to **Compute → Instances → Create instance**:
   - **Image:** Canonical Ubuntu 22.04 or 24.04.
   - **Shape:** Ampere `VM.Standard.A1.Flex` with **4 OCPU / 24 GB**. Use 2 OCPU / 12 GB if capacity is short.
   - **Networking:** keep the default VCN and tick *Assign a public IPv4 address*.
   - **SSH keys:** upload your public key, or download the generated one.
   - **Boot volume:** 50 GB or more. Models are about 3 GB and the image about 2 GB.
3. Note the **public IP**, for example `140.238.1.2`.
4. Open ports 80 and 443 in the cloud firewall: **Networking → Virtual Cloud Networks → your VCN → Security Lists → Default → Add Ingress Rules**. Add two rules with source `0.0.0.0/0`, protocol TCP, and destination ports `80` and `443`.

### B2. Prepare the server

```bash
ssh -i ~/.ssh/your_key ubuntu@140.238.1.2

# Oracle's Ubuntu images also block ports in iptables - open 80/443 there too:
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# Docker + compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version
```

### B3. Get the code and configure

```bash
git clone https://github.com/<your-user>/careharbor.git
cd careharbor
cp .env.example .env
nano .env
```

Set at least these values in `.env`:

```ini
APP_ENV=prod
SESSION_SECRET=<paste: openssl rand -hex 32>
STAFF_PASSWORD=<long random password>
CAREHARBOR_DOMAIN=140-238-1-2.sslip.io      # your IP with dashes; sslip.io resolves it - no domain purchase needed
LLM_MODEL=qwen2.5:3b
LLM_FAST_MODEL=qwen2.5:1.5b
LLM_FALLBACK_MODEL=qwen2.5:1.5b
```

`LLM_BASE_URL` is overridden in `docker-compose.yml` to point at the bundled Ollama container (`http://ollama:11434/v1`). To use Groq instead (faster on a small VM), set `LLM_BASE_URL_DOCKER=https://api.groq.com/openai/v1`, `LLM_API_KEY=<groq key>`, `LLM_MODEL=llama-3.1-8b-instant` and `LLM_FAST_MODEL=llama-3.1-8b-instant`.

### B4. Start the stack

```bash
docker compose up -d --build                     # first build: ~5-10 min on the Arm VM
docker compose exec ollama ollama pull qwen2.5:3b
docker compose exec ollama ollama pull qwen2.5:1.5b
docker compose ps                                # careharbor should become "healthy"
curl -s http://127.0.0.1:7860/api/health          # {"status":"ok", ...}
```

Open `https://140-238-1-2.sslip.io`. Caddy obtains a Let's Encrypt certificate on the first request, which takes a few seconds.

### B5. Verify that persistence works

1. Ask a question in the patient chat.
2. Run `docker compose restart careharbor`.
3. Refresh the browser. The conversation should still be there.
4. Verify as a demo patient, click **Sign out on this device**, start a new chat and verify again. **Your earlier conversations** should list the old chat.

### B6. Day-2 operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f careharbor` |
| Update after a `git push` | `git pull && docker compose up -d --build` (the `/data` volume is kept) |
| Back up chats and bookings | `docker compose exec careharbor python -c "import sqlite3;s=sqlite3.connect('/data/careharbor.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"` then `docker compose cp careharbor:/data/backup.db ./careharbor-$(date +%F).db` |
| Reset demo data (deletes chats) | `docker compose exec careharbor python scripts/seed.py` |
| Run the evals on the server | `docker compose exec careharbor python scripts/run_evals.py --mock` |
| Stop everything | `docker compose down` (volumes kept); `docker compose down -v` also **deletes all data** |

Appointment slots are topped up automatically for the next 14 days on every start (`ensure_future_slots`), so a long-running demo stays bookable. Demo prescriptions are dated relative to the seed day; reseed occasionally if you want them to look current.

---

## C. Hugging Face Spaces + Groq (quick public demo)

1. **Groq key:** go to console.groq.com → API Keys → create a key (free tier).
2. **Create the Space:** go to huggingface.co → New Space → SDK **Docker** → Blank → Public.
3. **Add the code:** push this repo to the Space's git remote. The Space's `README.md` must start with this header. Add it in the Space copy only, not in your GitHub README:
   ```yaml
   ---
   title: CareHarbor AI
   sdk: docker
   app_port: 7860
   ---
   ```
4. **Settings → Variables and secrets:** add `SESSION_SECRET`, `STAFF_PASSWORD`, `LLM_API_KEY` (as secrets), plus `LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_MODEL=llama-3.1-8b-instant`, `LLM_FAST_MODEL=llama-3.1-8b-instant`, `LLM_FALLBACK_MODEL=llama-3.1-8b-instant` and `APP_ENV=prod`.
5. **Build:** the Space builds the `Dockerfile` automatically. Open the Space URL when the status shows *Running*.

**Persistence caveat:** free Spaces use an ephemeral disk. Chats, bookings and memory reset when the Space restarts or sleeps. The image already keeps all state in `/data`, so attaching HF persistent storage (a paid add-on mounted at `/data`) fixes this with no code change.

**Client IP on Spaces:** the image trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`). Behind the HF proxy, every visitor then shares one IP, so the per-IP limit (30 messages/min) becomes shared. Per-session limits and the per-phone identity lockout still apply. For a busier demo you can set the variable `FORWARDED_ALLOW_IPS=*`. That accepts the proxy's header, but a client could spoof it, so only the IP rate limit is weakened, not identity protection.

**Privacy note:** with Groq, prompts leave your machine. They contain only synthetic data and are PII-masked, but a real clinic would need a provider agreement and a data-protection review before doing this.

---

## What is stored where (the same layout in every option)

| File (`data/` locally, `/data` in Docker) | Contents |
|---|---|
| `careharbor.db` | Clinic data (patients, doctors, slots, appointments, prescriptions). Also identity sessions with hashed OTPs, staff tickets, the audit log, feedback, and **chat history** (`conversations`, `chat_messages`: PII-masked user text, no OTPs) |
| `checkpoints.db` | LangGraph per-conversation state, so a paused booking confirmation survives a restart |
| `checkpoints-memory.db` | Long-term preferences keyed by an HMAC memory key, never by the patient id |
| `traces/*.jsonl` | One line per turn: stages, latency, tokens and tool calls (for the staff console) |

## Security checklist before going public

- [ ] `APP_ENV=prod`, plus a strong `STAFF_PASSWORD` and `SESSION_SECRET` (and no `.env` in git: `git status` must not list it).
- [ ] Only ports 80 and 443 are open. The API (7860) is bound to `127.0.0.1` and Ollama is not published.
- [ ] HTTPS works. The staff cookie is `Secure` automatically behind Caddy (uvicorn `--proxy-headers`).
- [ ] The demo banner is visible: synthetic data, not medical advice, 112 / 108 for emergencies.
- [ ] Rate limits are on (30 messages per minute per session or IP, 5 staff logins per minute).
- [ ] Backups are scheduled if you care about the demo data.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser shows a certificate error or Caddy logs `challenge failed` | Ports 80 and 443 must be open in **both** the Oracle security list and iptables (B1 step 4 and B2), and `CAREHARBOR_DOMAIN` must match the IP. |
| `careharbor` is unhealthy and the logs say `connection refused ... ollama` | Models are not pulled yet: run the `ollama pull` commands in B4. |
| Staff login returns 503 | `STAFF_PASSWORD` is still `change-me` while `APP_ENV=prod`. |
| Old chats vanished after a redeploy | `SESSION_SECRET` changed, or the volume was removed (`down -v`). Keep the secret stable. |
| A conversation shows "verification has expired" | By design: verified sessions last `VERIFIED_TTL_MINUTES` (default 120). Verify again, and the old chat appears under *Your earlier conversations*. |
| "Too many attempts for this number" | 5 wrong phone + DOB tries in 15 minutes lock that number (brute-force protection). Wait 15 minutes. |
| Answers are slow (20 s or more) on the VM | Use the 4-OCPU shape, or switch the LLM to Groq (B3). |
