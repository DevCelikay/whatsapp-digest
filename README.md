# WhatsApp Morning Digest

Every morning at ~7am, this project reads your unread WhatsApp messages, asks an AI to sort them into what actually needs your attention, and emails you a short digest. It runs for free on GitHub's servers — your laptop doesn't need to be on.

You get four tidy sections:

- **Reply today** — the urgent stuff (questions, blockers, decisions)
- **Waiting on you** — people you owe a reply, with a suggested reply for each
- **Waiting on them** — threads where you sent the last message and got ghosted
- **Group activity** — one-liner per busy group chat

---

## Before you start

You'll need accounts on three services. Sign-up links:

1. **GitHub** — https://github.com/signup (free)
2. **Unipile** — https://www.unipile.com (free tier works for personal use — this is what actually talks to WhatsApp for us)
3. **One of:**
   - **Anthropic (Claude)** — https://console.anthropic.com (recommended; a few dollars of credit lasts months at this volume), **OR**
   - **OpenAI** — https://platform.openai.com/signup

   You only need one. If you set both, Claude is used.

You'll also need a **Gmail account** you're willing to send from. A second/throwaway Gmail is fine.

---

## Step 1 — Connect WhatsApp to Unipile

1. Go to https://dashboard.unipile.com and sign in.
2. Click **Add account** → **WhatsApp**.
3. A QR code appears on screen.
4. On your phone, open WhatsApp → **Settings** → **Linked Devices** → **Link a device**.
5. Scan the QR code on the Unipile screen.
6. Wait a few seconds — the account shows up as connected.

> ⚠️ Keep your phone online. WhatsApp Web-style connections rely on your phone being reachable.

## Step 2 — Grab your three Unipile values

Still inside the Unipile dashboard:

| Value | Where to find it |
|---|---|
| `UNIPILE_API_KEY` | Top-right profile menu → **API Keys** → **Create** → copy the key (you only see it once — save it somewhere safe) |
| `UNIPILE_API_URL` | Shown next to the key, e.g. `https://api8.unipile.com:13888` — copy it exactly (the script appends `/api/v1` automatically) |
| `UNIPILE_ACCOUNT_ID` | **Accounts** page → click your WhatsApp account → copy the ID (a long string) |

## Step 3 — Get an AI API key (Claude *or* OpenAI)

Pick one. Claude is recommended — similar price, very strong at this kind of summarisation.

**Option A: Anthropic (Claude)**

1. Go to https://console.anthropic.com and sign in.
2. **Settings** → **API keys** → **Create key** → name it "whatsapp digest" → copy the key (starts with `sk-ant-…`). You only see it once.
3. **Settings** → **Billing** → add **$5 or $10** of credit. Lasts many months at this usage.

**Option B: OpenAI**

1. Go to https://platform.openai.com/api-keys.
2. Click **Create new secret key** → name it "whatsapp digest" → **Create**.
3. Copy the key (starts with `sk-…`). You only see it once.
4. Go to https://platform.openai.com/settings/organization/billing/overview and add **$5 or $10** of credit.

## Step 4 — Create a Gmail app password

You can't use your normal Gmail password — Google requires a special "app password".

1. Turn on 2-Step Verification if it isn't already: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Name it "WhatsApp Digest" → **Create**.
4. Copy the 16-character password Google shows you (spaces don't matter — you can paste with or without them).

> If `/apppasswords` says the feature isn't available, you still need to enable 2-Step Verification first.

## Step 5 — Fork this repo and add your secrets

1. At the top of this GitHub page, click **Fork** → create your own copy.
2. In **your** fork, click **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
3. Add each of the following, one by one:

| Secret name | What to paste |
|---|---|
| `UNIPILE_API_KEY` | Your Unipile API key from Step 2 |
| `UNIPILE_API_URL` | Your Unipile API URL from Step 2, e.g. `https://api8.unipile.com:13888` |
| `UNIPILE_ACCOUNT_ID` | Your WhatsApp account ID from Step 2 |
| `ANTHROPIC_API_KEY` | Your Claude key from Step 3 (set this **OR** `OPENAI_API_KEY`) |
| `OPENAI_API_KEY` | Your OpenAI key from Step 3 (only if you didn't set `ANTHROPIC_API_KEY`) |
| `GMAIL_ADDRESS` | The Gmail address you want to send FROM |
| `GMAIL_APP_PASSWORD` | The 16-character app password from Step 4 |
| `RECIPIENT_EMAIL` | The address you want the digest sent TO (can be the same as above) |

## Step 6 — Test it

1. In your fork, click the **Actions** tab.
2. If GitHub asks "workflows are disabled" → click **I understand, enable them**.
3. Pick **WhatsApp Morning Digest** on the left.
4. Click **Run workflow** → **Run workflow**.
5. Wait ~30 seconds. When the job turns green, check your inbox. Check spam/promotions if you don't see it.

After the first successful run, it will auto-send every morning at 06:00 UTC (07:00 UK winter / 08:00 UK summer).

---

## Troubleshooting

**"Authentication failed" from Gmail**
You pasted your regular Gmail password, not the app password. Redo Step 4. Also confirm 2-Step Verification is on.

**Unipile 401 Unauthorized**
API key wrong or expired. Regenerate in the Unipile dashboard and update the `UNIPILE_API_KEY` secret.

**Unipile 404 / empty chats**
Your `UNIPILE_ACCOUNT_ID` is wrong, or WhatsApp got disconnected. In the Unipile dashboard, check the account status. If disconnected, re-scan the QR.

**Email arrives but says "Inbox zero" every day**
You genuinely have nothing waiting, or all your chats are archived/muted/broadcast-only. Try messaging yourself from another phone as a test.

**No email at all, and the Actions run is green**
Check the Gmail **Sent** folder of the sender account — it may have sent, but the recipient filtered it. Also check spam on the recipient side.

**The Actions run is red**
Click into the failed run → expand the **Run digest** step. The error message usually says exactly which env var or credential is wrong.

---

## Tuning

If you want to adjust thresholds (how many days before something counts as "ghosted", etc.), edit the constants at the top of `digest.py`:

```python
UNREAD_LOOKBACK_HOURS = 36
WAITING_ON_YOU_DAYS = 7
WAITING_ON_THEM_DAYS = 3
SMALL_GROUP_MAX_MEMBERS = 15
```

To change when it runs, edit the `cron:` line in `.github/workflows/digest.yml`. Crontab syntax: `minute hour day month weekday`, in UTC.

---

## Privacy note

Your unread message previews are sent to whichever AI provider you configured (Anthropic or OpenAI) for summarisation. If that's not acceptable for your chats, don't use this.
