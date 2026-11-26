# TOBI X RESET SYSTEM

Everything is pre-configured. Nothing to edit.

1. Upload ALL files of this folder to a (PRIVATE) GitHub repository.
2. Render -> New -> Blueprint -> select the repo -> Apply.
   (Render reads render.yaml: build/start commands are automatic.)
3. When it is live, open  https://YOUR-APP.onrender.com/login
   Username: RGCHEAT    Password: 909090
4. API Settings -> set Base URL, then add one or more seller API tokens (yours + friends). A key is tried on every account.

Telegram admin ID: 6739795427 (use /admin in the bot).
Keep-alive pings /health every 4 minutes automatically.

## New in this version
- Tokens are auto-cleaned (spaces, quotes, "Bearer " prefix) — a very common reason one token fails and another works.
- API Settings -> "Debug" box shows the raw server reply for any account; Logs page shows per-account reason.
- Resellers: add a token with "Reseller Telegram ID" in API Settings, or the reseller sends `/addtoken <token> [name]` (also /mytokens, /deltoken). Reseller's own token is tried first.
- Anti-spam: private chats only, 10 msgs/min per user, admin alerts at most once per 30 min.
- Render free plan wipes bot.db on every restart. To keep tokens permanently set env var
  SELLER_TOKENS="Me:txr_xxx,Friend:txr_yyy"  (and BOT_TOKEN, ADMIN_PASS, SECRET_KEY as env vars too).

## Auto-detect key owner
Set the Base URL once (API Settings), then add each seller as name + token only.
A key is checked against all seller tokens in parallel; the owner's token resets it (~1s),
the winner is remembered for next time. If nobody owns it -> "Invalid key".
