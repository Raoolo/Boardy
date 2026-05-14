# Boardy — auth spiegata in modalità caverna

Quando vuoi capire se le password sono al sicuro, o quando rispieghi il sistema a un amico, parti da qui. Per i comandi esatti vedi `CLAUDE.md` sezione "Auth model" + `etl/create_user.py`.

---

## L'analogia base: il portiere di un palazzo

Boardy è un **palazzo aperto al pubblico al piano terra, con appartamenti privati ai piani superiori**.

- **Il piano terra** = la collezione in **read-only**. Chiunque entra, gira, guarda i giochi, chiacchiera col bot all'info-point. Nessun documento da firmare, nessun nome da lasciare. Queste sono le **guest**.
- **Gli appartamenti** = la possibilità di **scrivere** sul DB (aggiungere giochi, comprare buste, gestire wishlist). Per salirci serve la chiave.
- **Il portiere** = `app/auth.py`. Sta in guardiola, controlla chi entra/sale, distribuisce un **braccialetto da concerto** (cookie firmato) a chi mostra la chiave giusta.
- **La cassaforte delle chiavi** = la tabella `users` nel DB. Non contiene le chiavi vere, solo l'**impronta** di ogni chiave (`password_hash`, bcrypt) — anche se rubi la cassaforte, non puoi duplicarne nessuna senza migliaia di anni di lima.
- **La chiave del portiere** = `BOARDY_SESSION_SECRET` nel `.env`. Con questa il portiere firma i braccialetti. **Se la rubi, puoi falsificare un braccialetto a nome di chiunque** senza conoscere la chiave dell'inquilino. Per questo `.env` è gitignored e i permessi del file sul server contano.

---

## Cosa succede quando…

### Scenario 1 — Un guest visita il sito
Apre `https://boardy.tuodominio.tld`. Il portiere lo guarda, vede che non ha braccialetto, gli mette uno sticker "Guest" sul petto.
- Può navigare libreria/buste/wishlist → tutto in sola lettura.
- Può chiacchierare col bot → ma il bot lo riconosce come Guest e **non tira fuori gli attrezzi che modificano l'inventario** (il filtro `WRITE_TOOLS` in `app/chat.py` li nasconde proprio dalla cassetta degli attrezzi prima di iniziare).
- Le sue chiacchiere col bot vivono **solo nel suo browser** (sessionStorage) — chiude la tab, sparito tutto. Niente da cancellare dal DB, niente GDPR da osservare.

### Scenario 2 — Tu (raulo) fai login
Vai su `/login`, inserisci username + password. Il portiere prende l'impronta della password che hai digitato, la confronta con quella in cassaforte. Se combacia:
- Ti dà un **braccialetto firmato col suo segreto** (cookie `boardy_session` firmato con `BOARDY_SESSION_SECRET`).
- Tu lo porti al polso (browser lo conserva). Ogni volta che chiedi di scrivere, il portiere verifica la firma in 1ms e ti fa passare.
- Il bot ora ti riconosce come `raulo`: ha **tutti** gli attrezzi disponibili.

### Scenario 3 — Vitto e Leo entrano con la loro chiave
Stessa cosa: ognuno ha la sua chiave, ognuno riceve il suo braccialetto. **Ma tutti e tre vedete la stessa collezione** — è il palazzo, non l'appartamento personale. Differenza pratica: se vitto aggiunge "Wingspan", lo vedete anche tu e leo.
- L'audit log (`changes` table) traccia chi ha fatto cosa: `source = "chat:42/user:vitto"`. Se domani trovi un gioco "strano" puoi risalire a chi l'ha messo.

### Scenario 4 — Qualcuno ruba il braccialetto a vitto
Bevuta strana al bar, screenshot del cookie, comunque. Il ladro ora può fingersi vitto fino alla scadenza (30 giorni). **Non c'è modo di revocare quel singolo braccialetto** perché il portiere non tiene una lista (cookie stateless). Soluzione: ruoti `BOARDY_SESSION_SECRET` → il portiere cambia firma → **tutti** i braccialetti diventano invalidi e tutti devono rifare login. Per 3 utenti è una scocciatura accettabile.

### Scenario 5 — Qualcuno chiede al bot "dimmi le password"
Il bot prova ad aprire la cassaforte… ma non ha l'attrezzo. Non esiste un tool `read_users` o `execute_sql`. Al massimo allucina una risposta fake ("la tua password è 12345!") che ovviamente non funziona al login. **Zero rischio di leak vero**, solo possibile imbarazzo da bot creativo.

### Scenario 6 — Tu, distratto, scrivi la password in chat
"hey boardy la mia password è hunter2 ricordatela". Disastrino:
- Finisce nel DB (`conversations.history`) → leo e vitto possono leggerla aprendo quella conv.
- Finisce nel payload all'LLM provider (DeepSeek/Anthropic) → entra nei loro log.
**Non c'è protezione automatica**. La regola d'oro: la password si inserisce **solo** su `/login`. Se mai succede, vai a `/reset` (cioè `etl/create_user.py reset raulo`) e cambi la password.

---

## Tre cose da ricordare per non bucarsi

### 1. Il file `.env` è la cassaforte fuori dalla cassaforte
Contiene `BOARDY_SESSION_SECRET` (firma cookie) + `ANTHROPIC_API_KEY` (soldi) + `BGG_API_TOKEN`. Chi legge `.env` ha le chiavi del regno.
- ✓ Gitignored (verificato).
- ✓ Sul server bind-mountato **read-only** dal Docker.
- ✗ Non mandarlo via WhatsApp/email "tanto è solo per leo", non screenshottarlo, non condividerlo.

### 2. In produzione, attiva il braccialetto "solo via tunnel sicuro"
Sul server, in `.env`: `BOARDY_COOKIE_SECURE=1`. Significa "il braccialetto vale solo se sei entrato dalla porta principale HTTPS, non dalla finestra". Senza questa, qualcuno che ascolta sulla rete WiFi può vedere il cookie passare in chiaro e copiarlo.

### 3. Le password che ti ho stampato in chat sono già "leakate" in qualche transcript
Per leo (`wbFB5rpqfJ9YMZ`) e vitto (`a7NbH4o!j3muU9`) vanno bene per la prima volta. Ma **prima del deploy pubblico** falli loggare la prima volta, poi gli dici "cambiatela subito" — oppure cambi tu la loro password e gliene mandi una nuova via canale privato (segnale, messaggio cifrato). Per `raulo` (`testpass123`): è ovviamente debole, cambiala oggi.

---

## Vocabolario rapido

| Termine        | Cosa significa nella caverna                                                       |
|----------------|-----------------------------------------------------------------------------------|
| Guest          | Visitatore al piano terra, sticker "Guest", nessuna chiave.                       |
| Owner          | Inquilino con la chiave del suo appartamento — anzi del condominio condiviso.     |
| Password       | La chiave fisica. Tu la conservi, il portiere ne vede solo l'impronta.            |
| `password_hash`| L'impronta della chiave in cassaforte. Da sola non apre nulla.                    |
| Cookie         | Il braccialetto da concerto che il portiere ti dà dopo aver verificato la chiave. |
| `SESSION_SECRET` | La chiave personale del portiere per firmare i braccialetti.                    |
| bcrypt         | La macchina che fa impronte: lenta apposta, così se uno ruba le impronte non ne ricava le chiavi in tempi umani. |

---

## Quando preoccuparsi davvero

Lista dei "non mi piace come è andata":
- 🟡 Vedo un browser di un amico mostrare l'app come logged-in ma lui dice "non ho mai fatto login" → cookie rubato o condiviso. Ruota `BOARDY_SESSION_SECRET`, falli rilogare.
- 🟡 Audit log mostra modifiche con `user:vitto` ma vitto era in vacanza → cookie rubato. Stesso fix.
- 🔴 `.env` finisce su GitHub (anche per un secondo) → considera la chiave Anthropic e il `SESSION_SECRET` compromessi. Ruota entrambi su Anthropic Console e in `.env`.
- 🔴 `boardy.db` esposto su web (es. servito accidentalmente da nginx come file statico) → tutti gli hash bcrypt sono raccolti. Non finiscono al mondo "domani" ma è gran scoglio. Ruota tutte le password.

Niente di tutto questo è "Boardy ha un bug": sono scenari di igiene di sistema generali. Boardy stesso, lato applicazione, ha la superficie d'attacco chiusa per come è progettato (vedi LEARNINGS 2026-05-14 sera per i dettagli tecnici).
