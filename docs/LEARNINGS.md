# Boardy — Learnings & Decisions Log

A running notepad for Claude (and humans) working on this project across sessions.
Append, don't rewrite. Newest entries on top.

> 🔖 **Current state breadcrumb** (read `.env` and `CLAUDE.md` for ground truth):
> Production LLM provider is **DeepSeek-chat** (`LLM_PROVIDER=deepseek` in `.env`).
> Older entries below may say "Sonnet is the default" or "we went back to Anthropic" —
> those were true at their date; the provider flipped back to DeepSeek later
> (cost: ~10× cheaper, tool-use quality acceptable for Boardy's needs).
> `/library/filter` is always DeepSeek regardless of `LLM_PROVIDER`.

---

## 2026-06-21 — Buste: 2ª fonte BGG `cardsetsbygame` (sblocca i giochi nuovi tipo Intarsia)

**Sintomo.** Aggiungendo **Intarsia** (bgg 422126) da chat, il bot diceva che "non ha carte". Falso: ha 81 carte 44×68 e l'info è su BGG (pagina `/boardgame/422126/intarsia/sleeves`). Causa doppia: (1) l'unica fonte buste automatica era **sleeveyourgames** (`etl/syg_api`), che NON ha i giochi troppo nuovi → `found:false`; (2) il modello interpretava `found:false` come "il gioco non ha carte" e lo riferiva all'utente.

**Scoperta.** La pagina BGG `/sleeves` è cookie-walled (403 a curl), MA è alimentata da endpoint JSON **aperti** su `api.geekdo.com` (stesso host non-Cloudflare dei Files). Trovati intercettando il traffico del browser:
- `GET /api/cardsetsbygame?objectid=<bggid>` → `{cardSets:[{addon, name, cardTypes:[{width,height,quantity,name}]}]}`. **Questo è quello che serve** (misure + quantità carte). `cardSets:[]` = gioco senza carte; `null` = id inesistente.
- `GET /api/sleevesbycard?width=W&height=H` → buste consigliate per una misura (non ci serve: noi vogliamo le misure, non i prodotti).

Verificato che `cardsetsbygame` risponde **200 a curl semplice** (no browser, no cookie, no auth) → il codice di produzione usa `urllib` come gli altri client. Il browser è servito SOLO per la scoperta.

**Gotcha Playwright su questo server (Ubuntu 26.04).** Playwright (anche l'ultima) NON ha build browser per `ubuntu26.04-x64` (`playwright install chromium/firefox` fallisce). Workaround per la scoperta: `snap install chromium` + `chromium.launch(executable_path="/snap/bin/chromium", args=["--no-sandbox","--disable-dev-shm-usage"])`. **Ma in produzione non serve nessun browser** per le buste (è tutto urllib). Implicazione: il download regolamenti via `bgg_browser` resta KO su questo OS finché Playwright non supporta 26.04 o non si punta allo snap chromium.

**Gotcha edizioni multiple.** Un gioco può avere più `cardSet` con `addon=false` (edizioni diverse, es. Wingspan 212 vs 222) — **non sommarle**. `parse_cardsets` prende il PRIMO base set (ordine BGG = più rilevante; combacia con sleeveyourgames). Aggrega i `cardTypes` per (width,height).

**Fix.** Nuovo client `etl/bgg_cards_api.py` (urllib+cache, stile `bgg_files_api`/`syg_api`). `app/tools.sleeve_lookup` interroga **entrambe** le fonti (quando c'è `bgg_id`) e fa **cross-check** (helper `_compare_sleeve_reqs`): match per misura con tolleranza ±1mm, confronto dei count sulle misure comuni; misure presenti in una sola fonte → `only_*` (NON rompono l'accordo: es. la "Goal board" 150×120 di Wingspan che sleeveyourgames omette). Esiti: concordi → `source="sleeveyourgames+bgg (concordi)"`; divergono → `source="sleeveyourgames (⚠️ diverge da bgg)"` + `warning` + `cross_check.bgg_requirements` (il bot mostra entrambe e fa scegliere l'utente); una sola fonte → `source` la nomina; nessuna → `found:false` con hint che **NON significa "niente carte"**. Schema tool + descrizione `bgg_lookup` aggiornati (passare SEMPRE `bgg_id`). Verifica: Intarsia → `boardgamegeek.com` 81×(44×68); Wingspan → `sleeveyourgames+bgg (concordi)`, goal-board come `only_bgg`; count divergenti → `agree:false` con dettaglio; 63 vs 63.5 → tollerato; Azul → `found:false`.

---

## 2026-06-09 (sera) — BGG Files download: `fileid` ≠ `filepageid` (il bug del 404)

**Sintomo.** Batch di download BGG Files: 2 file su 9 funzionavano (Vampiri, Barrage), gli altri 7 fallivano *sempre* con `BGGBrowserError: could not download` — anche con retry+backoff. Non era rate-limiting (il pattern non era "i primi N falliscono"): erano sempre gli **stessi** fileid a fallire.

**Causa.** L'API Files di BGG (`api.geekdo.com/api/files`) espone per ogni file **due** id distinti: `fileid` e `filepageid`, **e sono diversi** (es. Secret Hitler: fileid=156161, filepageid=125456). La filepage `/filepage/<id>/<slug>` — quella che `bgg_browser` naviga per far girare il JS e intercettare `downloadurls` — chiave su **`filepageid`**, non `fileid`. Navigare `/filepage/<fileid>/x` dà **404** → nessuna XHR `downloadurls` parte → download fallisce. I 2 che funzionavano erano casi in cui BGG risolveva *anche* per fileid (incoerente). Il valore canonico è il `filepageid` (è quello nel campo `href` del JSON: `/filepage/125456/secret-hitler-rules-pdf`).

**Fix.** `etl/bgg_files_api._parse_file` ora espone **entrambi** (`fileid` + `filepageid`). Tutto il resto della pipeline di download usa il **`filepageid`**: `bgg_browser.download/fetch_one(filepageid)`, il tool `download_rulebook(bgg_filepageid=...)` (param + schema rinominati), `find_rulebook`/`_bgg_candidates` (candidati portano `bgg_filepageid`), `_backfill_rulebook`, `etl/backfill_rulebooks.py` (chiave dict + `.download()`), e il system prompt. Source string DB: `bgg:filepage/<id>`. Lezione generale: quando un'API espone più id per la stessa entità, **non assumere quale serve all'URL** — controlla il campo `href` autorevole.

**Stato finale dei download BGG (9 indicizzati).** Echoes Of Time, SETI, Secret Hitler, Splendor (Sun Never Sets + Silk Road), Blackout HK, Vampiri Heritage, Barrage (Final Rulebook fid 242312, non il Pre-Draft top), Elfenland De Luxe. **Copertura regolamenti: 36 → 45/58 posseduti.** Falliti per scelta/limite: **Annunaki** ("Core Game Rulebook" = PDF **scansionato**, 0 testo → no OCR), HeroQuest/Monolyth/Duel-Agora/Tesla/Hamlet/Sagrada (solo homebrew/summary/lingua errata in cima → skippati), + i `none` (D&D set, Tortelli, Andor, Schonbrunn). Per i giochi col top-candidate sbagliato il backfill auto-prende comunque il top: per quelli vanno scaricati col `filepageid` mirato (come fatto per Barrage/Blackout).

---

## 2026-06-09 — Rulebook auto-fetch: 1j1ju beats both Tavily e BGG Files

**Contesto.** La pipeline RAG dei regolamenti (`ingest_rulebook`/`ask_rules`/`list_rulebooks`) esisteva ma pretendeva il PDF già su disco. Aggiunti `find_rulebook` (read) + `download_rulebook` (write) per chiudere la catena find→download→ingest→ask, con UX proponi-e-conferma.

**Fonte PDF — cosa funziona e cosa no:**
- **BGG Files: scartato.** L'XML API2 NON espone la sezione Files; la pagina web è Cloudflare/cookie-walled (WebFetch → 403, coerente coi learning sullo scraping BGG). Un crawler dovrebbe battere Cloudflare → costo alto, resa incerta.
- **Tavily su 1j1ju: inaffidabile.** `include_domains=["1j1ju.com","cdn.1j1ju.com"]` ritorna quasi zero `.pdf` (Tavily indicizza malissimo cdn.1j1ju.com). Diverso dal WebSearch generico che invece li trovava — ma quello non è disponibile a runtime nell'app.
- **1j1ju endpoint diretto: vincente.** La search on-site è JS/AJAX (i param GET su `/rules?search=` sono ignorati), MA l'autocomplete che la guida — `GET /rules/search?q=Q` — ritorna HTML con i link PDF diretti `cdn.1j1ju.com` negli anchor `<a class="dark-link" href="...pdf" title="...">`. Deterministico, gratis, ~4700 regolamenti multilingua. Wrapper isolato in `etl/onejour_api.py` (stile `bgg_api`/`syg_api`: cache su disco, rate-limit, header browser, parse via regex). Lingua dedotta dal suffisso filename (`-rulebook`→EN, `-regle`→FR, `-regole`→IT, ...). Tavily resta solo come fallback `.pdf` generico quando 1j1ju dà vuoto.

**Robustezza download.** `download_rulebook` valida i **magic bytes `%PDF`** (non si fida del Content-Type) → rifiuta pagine HTML scambiate per PDF; cap 30 MB. Smoke test OK: Dune Imperium → 20 pagine, 45 chunk, `ask_rules('conflitto')` (domanda IT su rulebook EN) → p.12 score 0.81. **Cross-lingual confermato**: e5-base interroga in IT su testo EN senza problemi → non serve scaricare il regolamento nella lingua dell'utente.

**Storage PDF: dentro SQLite (schema v9, `rulebooks.pdf_blob`).** Decisione utente: il PDF grezzo vive nel DB, non su disco → un solo `boardy.db` come backup porta tutto (PDF + chunk + embedding), portabile. Refactor: `rulebooks.ingest_bytes(game_name, data, source=...)` è il core (parse da `BytesIO`, salva blob + chunk); `ingest(path)` legge il file e delega; `download_rulebook`/`upload_rulebook`(REST)/auto-hook passano i bytes direttamente → **niente più scrittura su `rulebooks/`**. `get_pdf(name)` ri-estrae i bytes per un eventuale export. Le righe pre-v9 hanno `pdf_blob` NULL (ri-ingerire per riempirlo). Gli embedding erano già in DB (`rulebook_chunks.embedding`, float32 BLOB), quindi il vero contenuto ricercabile non è cambiato.

**Risoluzione nome tollerante (`rulebooks._resolve_game`).** Il match era `LOWER(name)=LOWER(?)` esatto → falliva su cosmetici ("Dune: Imperium" vs "Dune Imperium"). Ora: exact LOWER → fallback normalizzato (`_norm_name`: lower, punteggiatura→spazi, collapse) che accetta solo match **non ambiguo** (una sola riga). Usato sia da `ingest` sia da `search`, così download/ask non si rompono per un due-punti. Nel flusso auto invece il problema non esiste: l'hook lavora sull'`id`.

**Auto-fetch su nuovo gioco (`_backfill_rulebook`, post-`add_game`).** Scelta utente "auto se match certo, altrimenti proponi". L'hook (best-effort, come `_backfill_bgg_media`) cerca su 1j1ju e scarica SOLO se un candidato, tolte le parole-regolamento dal titolo (`_rulebook_core`: rulebook/rules/regle/regole/anleitung/...), normalizza **esattamente** al nome del gioco → esclude espansioni ("Catan Explorers Pirates" ≠ "catan") e omonimi; tra i pari preferisce EN poi il titolo più corto. Se nessun match esatto → non fa nulla (la chat proporrà). Salta se il gioco ha già un rulebook. Conseguenza voluta: giochi con nome **italiano** in DB ("I Coloni di Catan") non scattano in auto (nessun match esatto su titoli EN) → li gestisce la chat col titolo inglese. Test: Azul/Barrage/Spirit Island/HeroQuest ecc. → 11 match certi su 57 giochi, punteggiatura gestita ("Gloomhaven - Jaws of the Lion" ↔ "Gloomhaven: Jaws of the Lion").

---

## 2026-06-09 — BGG Files: come scaricarli (il "cookie-wall" era un mito)

Scoperta costosa, da non ri-derivare. **(a) Elencare i file di un gioco è APERTO**: `GET https://api.geekdo.com/api/files?objectid=<bggid>&objecttype=thing&showcount=50&pageid=1&sort=hot` → JSON con `fileid`, `filename`, `description` ("Rulebook in English"), `language`, `downloadCount`, `numpositive`. `api.geekdo.com` NON è dietro Cloudflare. Il per-file `/api/file/<id>` dà metadati ma NON l'URL di download. **(b) Scaricare richiede browser + login**: l'URL del file è hash-based e generato da JS. La filepage `/filepage/<id>/<slug>` (slug fittizio ok) fa una XHR a `https://api.geekdo.com/api/file/downloadurls?ids=<id>` con header `Authorization: GeekAuth <token>` (token in-page, non in cookie) → risposta `{"downloadUrls":[{"id","url":"/file/download_redirect/<hash>/<file>"}]}`. L'endpoint è **403 da sloggati** (anche per l'app ufficiale). Soluzione (`etl/bgg_browser.py`): Playwright headless, login via `context.request.post(/login/api/v1, {credentials:{username,password}})`, naviga la filepage e **intercetta la risposta** `downloadurls` (così non devo replicare l'header GeekAuth), poi scarico l'URL hash con `context.request` (eredita CF-clearance + sessione). Verificato: fid 3539 → 2.39MB PDF. Cose che NON funzionano (provate tutte): urllib/curl con header+TLS browser, XHR header, Bearer `BGG_API_TOKEN` (vale solo per l'XML API), cloudscraper/curl_cffi (il blocco non è il fingerprint — le pagine danno 200 — ma la generazione JS dell'URL + il login-gating). `geekfile_download_redirect.php` = 404 (deprecato). **Stato implementazione: vedi `## 🚧 IN CORSO` in docs/TODO.md.**

---

## 2026-06-09 — Backfill rulebooks: 36/58 coperti, scoperti bgg_id errati

**Run.** `etl/backfill_rulebooks.py --apply --level likely` su 56 giochi posseduti senza regolamento → **33 indicizzati** al primo giro (+ Dune già presente). Risoluzione nome **inglese via BGG** essenziale: i titoli 1j1ju sono EN, i nomi DC spesso IT ("I Coloni di Catan" → "Catan"). Classificazione match: `strong` (titolo normalizzato == nome, tolte parole-regolamento) = affidabile; `likely` (subset token) = da rivedere (sottotitoli/edizioni); `weak` = quasi sempre gioco diverso (non su 1j1ju, la ricerca fa fuzzy-fallback su roba a caso).

**bgg_id errati in DB (dato pre-esistente) smascherati dal backfill.** La risoluzione del nome EN ha rivelato 3 righe col `bgg_id` che puntava al gioco SBAGLIATO → metadati inquinati (descrizione/peso/tag di un altro gioco): `I Coloni di Catan Mercanti e Barbari`=36218(**Dominion**)→27760; `Duel - Agora`=284378(**Kanban EV**)→309116; `Room-25 - Ultimate`=332366(**Magnytour**)→212956. Fix: `bgg_lookup(id_corretto)` + `update_game(**metadati)` → corregge id E rinfresca i metadati in un colpo (ri-embedda descrizione + rigenera friendly_tags). Lezione: un bgg_id sbagliato non si vede finché non incroci una fonte esterna sul nome — vale la pena un check periodico nome-DB vs `_bgg_name`.

**Limiti 1j1ju (restano manuali).** (a) **PDF scansionati** (immagine, 0 testo): Barrage, HeroQuest → `ingest` rifiuta ("no extractable text"), servono OCR o un PDF text-based alternativo. (b) **Non presenti su 1j1ju**: giochi troppo nuovi (SETI), espansioni fan-made (Splendor "Sun Never Sets"/"Silk Road"), titoli di nicchia/parodia, e alcuni mainstream che semplicemente non ci sono (7 Wonders Duel: Agora, Elfenland, Secret Hitler) — verificato interrogando 1j1ju diretto: ritorna giochi non correlati = assente. Per questi: `web_search` fallback o URL/PDF manuale via `download_rulebook`/`ingest_rulebook`. Stato finale: **36/58 posseduti con regolamento**.

---

## 2026-06-05 — Deploy ARM (Oracle Cloud): torch CPU-only + auto-find capacity

**Contesto.** Avvio fase esecutiva del deploy su **Oracle Cloud Always Free ARM** (Ampere A1, fino 4 OCPU/24GB, gratis *per sempre* — non il Free Trial da 30gg). Due lavori tecnici preparati da locale prima di avere la VM.

**1. torch CPU-only su tutte le piattaforme (`pyproject.toml` + lock rigenerato).** Il `torch` di PyPI dichiara le dipendenze CUDA/nvidia (`cuda-bindings`, `cuda-toolkit`, `nvidia-*-cu13`, `triton`) con marker `sys_platform == 'linux'` — che è vero **anche su `aarch64`**. Conseguenze sulla VM ARM: (a) quelle wheel CUDA non esistono per aarch64 → `uv sync --frozen` **rompe il build**; (b) anche dove esistono, sono **GB di CUDA inutile** (l'e5 fa inferenza su CPU ovunque — VM senza GPU + APU AMD locale). Fix: indice dedicato `[[tool.uv.index]] pytorch-cpu = https://download.pytorch.org/whl/cpu` + `[tool.uv.sources] torch = { index = "pytorch-cpu" }`. Risultato: `torch 2.12.0+cpu`, **zero** pacchetti nvidia nel lock, immagine molto più piccola. La wheel critica `torch-2.12.0+cpu-cp313-cp313-manylinux_2_28_aarch64.whl` esiste sull'indice CPU → ARM ok.
- **Gotcha uv #1:** `[tool.uv.sources]` viene onorato **solo sulle dipendenze dirette**. torch arriva via sentence-transformers (transitiva) → l'override era ignorato finché non ho aggiunto `"torch>=2.2.0"` esplicito in `dependencies`.
- **Gotcha uv #2:** il marker su una source (`{ index=..., marker="sys_platform=='linux'" }`) **non** viene rispettato dal lock universale di uv 0.11 → restava su PyPI. Source *unconditional* (CPU ovunque) funziona ed è comunque ciò che vogliamo.
- **Gotcha uv #3:** `uv lock` è conservativo — non cambia sorgente se la versione pinnata "soddisfa" ancora. Serve `uv lock --upgrade-package torch` (o aggiungere la dep diretta, che forza il re-resolve).

**2. OneDrive + `.venv` = file locking infernale.** Il progetto vive sotto OneDrive; durante lo swap torch 2.11→2.12 OneDrive teneva lockate `torchgen/static_runtime` ecc. → `uv sync` falliva con `Access is denied (os error 5)` e lasciava dist-info orfane (RECORD mancante), rompendo l'import. Fix: rimozione forzata con retry di `torch/torchgen/functorch/*.dist-info` poi `uv sync` pulito. Per i prossimi sync usa `uv run --no-sync` quando vuoi solo eseguire.

**3. Build ARM = nativo sul server, niente cross-build.** Il workflow di deploy fa `docker compose up -d --build` **sulla VM ARM** → `docker build` produce arm64 nativamente. Il `Dockerfile` (base `python:3.13-slim`, multi-arch) non richiede modifiche `platform`. NON hardcodare `platform: linux/arm64` nel compose: romperebbe il build x86 locale.

**4. Script `etl/oci_find_capacity.py`.** Auto-retry contro `Out of host capacity` (ServiceError 500, frequentissimo sulle A1 free). Cicla sulle AD con backoff+jitter, notifica Telegram opzionale (riusa `TELEGRAM_BOT_TOKEN`/`TELEGRAM_OWNER_IDS`). OCI SDK = dipendenza opzionale → lancia con `uv run --with oci python etl/oci_find_capacity.py ...`. Param via flag o env `OCI_*`.

---

## 2026-06-04 — BGG metadata: tool `bgg_search`/`bgg_lookup` invece di scraping via web_search

**Sintomo.** Aggiungendo "Intarsia" (conv #27), il modello ha fatto **13 chiamate `web_search`** (tutte Tavily), ha sbagliato anno (2025→2024) e designer ("Bordspelwereld", che è un publisher olandese, invece di **Michael Kiesling**), e ha confabulato categorie/meccaniche. Quando l'utente ha chiesto "non hai la chiave API?", il modello ha *detto* di sì e poi ha incollato `xmlapi2/thing?id=422126` **dentro la query Tavily** — che fa una ricerca testuale, non scarica l'URL.

**Causa radice — buco di tooling, non bug di logica.** `etl/bgg_api.fetch_thing()`/`search()` esistevano e funzionano perfettamente, ma erano chiamati SOLO internamente da `_backfill_bgg_media()` (thumbnail/image post-write). **Nessun tool BGG-API era esposto alla chat**: l'unico strumento per dati esterni era `web_search`. E lo scraping della pagina pubblica BGG è cookie/Cloudflare-walled → Tavily ritorna `raw_content: ""` (l'unica cosa leggibile è il banner consensi cookie — da lì il "bloccato dai cookie"). Questo combacia col vecchio learning sullo scraping BGG fallito.

**Fix.** Aggiunti due read tool in `app/tools.py`: `bgg_search(query, types?)` → candidati `{id,name,year,type}`; `bgg_lookup(bgg_id)` → metadati completi (keys già allineate a `add_game`/`add_to_wishlist`). System prompt (`app/chat.py`, sia BASE che SLIM) riscritto: BGG metadata = `bgg_search`+`bgg_lookup`, `web_search` solo come fallback se l'API erra. Verifica end-to-end: stessa richiesta Intarsia → **2 chiamate**, dati corretti.

**Da ricordare.** (a) L'API XML BGG **non** espone misure carte/sleeve → per le "Buste previste" vedi la sezione sleeve qui sotto. (b) `bgg_lookup` ritorna già `complexity_label` calcolata + strippa le chiavi interne `_bgg_*`. (c) Sono read tool → NON in `WRITE_TOOLS`, quindi anche i guest possono consultare BGG (corretto). (d) Richiedono `BGG_API_TOKEN` in `.env` (già necessario per i backfill).

**Sleeve sizes → tool `sleeve_lookup` (API privata sleeveyourgames.com).** Stessa sessione: l'API BGG non dà le buste, quindi ho reverse-engineerato sleeveyourgames (Nuxt SPA). Scoperte: la pagina pubblica `/sleeves/{id}` è SSR e dà **500 ai bot**; ma il backend è un'**API JSON CORS-aperta, senza Cloudflare**, a `https://api.sleeveyourgames.com`. Endpoint utili (servono header browser UA+Origin+Referer, sennò WAF "noindex"): `GET /game/autocomplete?query=<nome>` → `[{text, id}]`; `GET /game/{id}` → JSON con `cards: [{card_quantity, height, width}]` (mappa 1:1 su `set_sleeve_requirements`!), `expansions[].cards`, e un `bgg_id` per confermare il match. Client isolato in `etl/syg_api.py` (stile `bgg_api.py`: HTTP+parse separati, cache `etl/.syg_cache/`); tool `sleeve_lookup(name, bgg_id?)` in `app/tools.py`. Verificato: Wingspan → 212 carte 57×87 + 4 espansioni, `bgg_id_match:true`. **Caveat:** API privata non documentata → può rompersi (mitigato: fallback a web_search→manuale, tutto in un file). **Limite strutturale:** giochi troppo nuovi (es. Intarsia 2024) NON sono ancora nel DB di sleeveyourgames → `found:false`, nessuna fonte automatica esiste, solo inserimento manuale. Pipeline nuovo: `bgg_search`+`bgg_lookup`+`sleeve_lookup` (in parallelo) → tabella → conferma → `add_game`+`set_sleeve_requirements`.

---

## 2026-05-20 — Telegram bot come thin client di `POST /chat`

Implementato `bot/telegram_bot.py` (PTB v21, async). Scelte di design che vale la pena ricordare:

**Thin client, non chat-loop duplicato.** Il bot NON importa `app.chat.chat()` — fa una HTTP call a `POST /chat`. Sembra ridondante (processo separato che parla allo stesso host) ma sblocca: (a) restart indipendente (bug nel bot non tira giù la web app); (b) opt-in totale via profilo docker; (c) zero duplicazione del tool-gating/audit/persistence — se cambio i tool in `app/tools.py`, il bot li eredita gratis.

**Allow-list owner via env, non login Telegram-side.** Discusso: chiedere `/start <password>` per promuovere chat_id a owner. Scartato perché la password girerebbe in cleartext nei log Telegram (CDN, server, mirror). L'allow-list di `user_id` numerici è (i) non-spoofabile lato Telegram (l'API li dà autenticati), (ii) un dato pubblico (basta `/whoami` per scoprirlo), (iii) zero credenziali sensibili in env.

**Due client httpx separati per owner vs guest.** Mantiene il cookie owner in un `AsyncClient` long-lived; per i guest crea un `AsyncClient` one-shot. Motivo: paranoia. Se la stessa chat passa owner→guest (cambio env, rimozione di un user_id), zero chance di portare avanti il cookie owner per errore.

**Persistenza mapping `chat_id → conv_id` su JSON file, non DB.** File `<BOARDY_DB dir>/telegram_chats.json`. Sta nel named volume con il DB → sopravvive ai restart container. Alternative scartate: (a) tabella in `boardy.db` — overkill per un dict piatto e mette il bot in dipendenza dallo schema (migrate, ecc.); (b) solo in-memoria — restart perde la conv attiva, UX bruttina.

**Guest history NON persistita.** Specchia il web frontend che usa `sessionStorage`. Restart bot = history guest perse, by design.

**Fallback Markdown → plain text.** Telegram parse_mode legacy "Markdown" tollera molto, ma occasionalmente fallisce su asterischi non bilanciati o URL strani. `try: parse_mode=MARKDOWN / except: plain` evita di perdere risposte legittime.

---

## 2026-05-20 — `add_game` ora chiama anche `set_sleeve_requirements` di default

Modifica al solo system prompt (`app/chat.py`, sia `SYSTEM_PROMPT_BASE` che `SYSTEM_PROMPT_SLIM`): quando l'utente chiede di aggiungere un nuovo gioco posseduto via chat, Boardy fa **due** `web_search` (BGG + sleeveyourgames), propone **una** tabella unica con riga "Buste previste", e su conferma chiama in sequenza `add_game(sleeve_status='to_sleeve')` + `set_sleeve_requirements`.

Prima la ricerca buste era documentata in una sezione separata ("se ti servono le buste fai questa query") e il modello la innescava in modo non deterministico — a volte sì, a volte no, e tipicamente l'utente doveva chiedere "ah, cercami anche le buste" come follow-up. Ora è codificata come pipeline unica per il caso "nuovo gioco owned".

Pattern: la scelta è **opzione 1** (catena con una sola conferma) anziché **opzione 2** (auto-fetch dopo `add_game` confermato). L'opzione 2 era più snappy ma rischiava di sleevare cose sbagliate senza che l'utente le vedesse in tabella. Una conferma di più si traduce in più trust.

Skip esplicito: "solo metadati" / "niente buste" salta la web_search sleeveyourgames; se il gioco non ha carte (dadi-only, astratto) → `sleeve_status='na'` senza set_sleeve_requirements.

NB: la pipeline analoga per **wishlist** (`add_to_wishlist`) era già implementata da prima — quella resta invariata.

---

## 2026-05-14 (sera) — Auth (owner login + guest read-only) & gotcha passlib/bcrypt 5

Aggiunti login owner (username/password locale) + modalità guest read-only. Schema v7 = `users` table; cookie firmato (itsdangerous); chat guest in `sessionStorage` lato client (zero GDPR concern); tool gating per ruolo in `app/chat.py`. Dettagli completi nel TODO `Auth + guest mode`. Tre cose vale la pena ricordare oltre quel changelog:

### Gotcha — `passlib[bcrypt]` ≠ funziona con `bcrypt>=5.0`

Avevo aggiunto `passlib[bcrypt]>=1.7.4` come dipendenza. uv ha tirato `bcrypt 5.0.0` come transitive. Primo `hash_password()` → crash:

```
(trapped) error reading bcrypt version
AttributeError: module 'bcrypt' has no attribute '__about__'
…
ValueError: password cannot be longer than 72 bytes
```

passlib legge `bcrypt.__about__.__version__` per il sniffing; in bcrypt 5 quell'attributo è stato rimosso. Quando passlib non riesce a sniffare, prova un "wrap-bug detection" che usa una password di 73 byte per testare un bug storico di bcrypt — bcrypt 5 rifiuta tutto >72 byte e crasha lo stub.

**Fix**: rimosso passlib, usato `bcrypt` direttamente (API pulita: `bcrypt.hashpw`, `bcrypt.checkpw`). Una dipendenza in meno, niente abstraction layer rotta. Truncate manuale a 72 byte in `app/auth.py:hash_password` per essere espliciti sul limite invece di lasciare crashare con password lunghe (unlikely ma deterministico).

Se in futuro vuoi argon2 (più moderno di bcrypt), usa `argon2-cffi` diretto invece di tornare a passlib.

### Decisione — guest chat = ephemera client-side (no DB write)

Discusso GDPR per chat guest. Salvare chat anonime senza consenso = violazione (art. 6 GDPR — il testo può contenere PII tipo "il mio amico Marco vuole giocare a..."). Tre opzioni: (a) ephemera in `sessionStorage`, (b) full GDPR (banner consenso + `/privacy` + retention + purge script + cookie session per right-to-delete), (c) niente chat per guest.

**Scelto (a)**. Costo compliance = 0, prezzo = nessuna analytics su cosa chiedono i guest. Se in futuro le analytics diventano interessanti, c'è già una TODO con la lista dei pezzi da aggiungere — meglio aggiungere superficie di compliance quando serve davvero, non "tanto un domani".

### Pattern — explicit `WRITE_TOOLS` set invece di euristica `_source`

Il filtro per ruolo guest/owner non può basarsi solo su "il tool ha `_source` kwarg" (euristica già usata per il source injection). `ingest_rulebook` scrive su `rulebooks`/`chunks` ma non dichiara `_source` — passerebbe il filtro come "read". Soluzione: `tools.WRITE_TOOLS = {...}` esplicito, source of truth per il gating. `_source` rimane l'euristica per l'audit injection (cosa diversa). Lezione generalizzabile: quando un meccanismo è una **policy** (chi può fare cosa), tienilo dichiarativo (set/lista) e centralizzato; quando è una **convenzione di firma** (cosa il chiamante può/deve passare), un'euristica su `inspect.signature` va bene.

---

## 2026-05-14 (PM) — Wishlist polish: skip-confirmation, ready-to-sleeve, BGG media hook

Sessione di rifinitura post-feature wishlist. Quattro pattern che emergono come riusabili oltre il caso specifico.

### Pattern — skip-confirmation policy diversa per livello di stake
La convenzione storica di Boardy è "propose table → wait sì/confermo" prima di
ogni write. Vale per `add_game`, `update_game`, `delete_game` perché ogni
riga partecipa a counts, sleeve math, audit visibility. **Per i wishlist
write il ritual è eccesso**: rollback costa 1 click su "✗ Rimuovi", l'item
non entra nei totali e l'utente ha già detto "aggiungi X". Tradurre questo
in tool description + system prompt salva ~300 token a turno e elimina il
loop "Confermi? sì → confermato → loading…".

**Regola**: la confermazione non è una virtù in sé, è copertura dal costo
del rollback. Quando il rollback è UI-locale e immediato, l'utente non vuole
essere fermato. Catalogare i write tool per stake-level (high/low) è una
distinzione che vale la pena fare esplicita nel prompt.

### Pattern — "ready" surface = what you can do NOW with what you have
Le tre tabelle storiche di `/sleeves` (Da comprare / Inventario / Aggiungi)
descrivono stati e gap. Manca la sezione **actionable**: "ho già tutto per
sleevare questi giochi". Aggiunta come prima sezione (sopra "Da comprare")
perché è la cosa più immediatamente utile da vedere appena apri la pagina.

Implementazione: per-game independent check vs greedy-deductive. Indipendente
è facile da spiegare ma sovrastima la capacità se due giochi competono per
lo stesso stock. Greedy alphabetical pass come secondo passaggio cattura il
caso e emette un `contention_note` esplicito. Mostrare il caso ottimistico
(per-game) È utile — mostra cosa potresti fare in linea di principio —
ma annotare la realtà sequenziale evita lo "stato di sorpresa a metà".

**Generalizzabile**: ogni dashboard che mostra "available capacity" su
risorsa condivisa ha questo dual-view. Fai sempre entrambi: ottimistico +
sequenziale, e annota la differenza.

### Pattern — bouncy animation è site-specific, non page-specific
Sulla `/sleeves` la `:focus-within { max-width: ...}` con `cubic-bezier
(0.34, 1.56, 0.64, 1)` è stata adorata. Sulla `/wishlist` lo stesso pattern
è stato bocciato perché "non sta bene". Differenza: su `/sleeves` la chat-
dock è full-bleed (background bordo-a-bordo, contenuto centrato), su
`/wishlist` è nested dentro una card con margini e border-radius. Espandere
la max-width di una card-nested combatte il rhythm dei KPI/filters circostanti;
espandere una full-bleed bar lo amplifica.

**Lezione**: prima di copiare un'animazione tra pagine, chiedersi *quale
livello di nesting* ha l'elemento. Animazioni "espansive" funzionano su
elementi alla cornice esterna, non su elementi nested. Stile statico +
glow accent sulla textarea bastano quando l'elemento è già visivamente
"contained".

### Pattern — post-write deterministic backfill per i campi che il modello manca
Il chat-driven enrichment via `web_search` estrae bene dalla prosa
(designer, weight, rating) ma manca regolarmente gli URL delle immagini
(sono `<img>` HTML, non testo). Soluzione: hook post-write
(`_backfill_bgg_media`) che, se `bgg_id` è valido e `thumbnail_url`/
`image_url` sono vuoti, fetch via `etl/bgg_api.fetch_thing()` (XML API
deterministica). Best-effort: try/except, mai blocca la write.

**Regola generale**: per ogni campo che è "deterministico-da-fetchare" ma
che il modello sbaglia/manca regolarmente, mettere un hook post-write che
backfilla via API ufficiale. Non insistere sul modello — è ottimo per
descrizioni e sintesi, scarso per URL exact-match. Dividere i campi in
"prose fields" (modello fa bene) e "structured fields" (API fa bene), e
backfillare i secondi automaticamente.

Cache su disco di `bgg_api` (CACHE_DIR + 0.6s rate-limit) rende l'hook
ripetibile a costo zero dopo la prima fetch — anche per add multipli dello
stesso gioco in test.

### Side-finding — `ALTER TABLE ... ADD COLUMN NOT NULL DEFAULT 'x'` su SQLite OK con literal
Già confermato nell'entry mattutina del 14, lo ribadisco perché ho continuato
ad aggiungere colonne (priority, notes_wishlist, target_price): finché il
default è un literal (stringa, numero, NULL) funziona. Espressioni dinamiche
come `CURRENT_TIMESTAMP` su ALTER falliscono — meglio NULL-able + UPDATE
seguente in quei casi.

---

## 2026-05-14 — Wishlist: status column su `games` invece di tabella separata

### Decisione strutturale
Wishlist nuova feature, due opzioni di schema sul tavolo:
1. Tabella `wishlist` separata con stessi campi BGG.
2. Colonna `status` ('owned' | 'wishlist') su `games`.

Scelta **(2)**. Tre vantaggi che pagano l'effort:
- BGG pipeline + auto-embed in `add_game`/`update_game` riusati identici.
  Wishlist item arricchito = stesso flusso web_search → propose → confirm.
- Promote-to-owned è `UPDATE games SET status='owned'` — niente row migration,
  embedding/bridges/audit storia preservati per definizione.
- Cross-discovery ("non lo possiedi ma è in wishlist") = stesso
  `search_games_semantic` con `status='any'`. Single SQL path.

Costo: aggiungere `WHERE status='owned'` a 5 query (list_games, sleeve_summary,
search_games_semantic, library_data, games_names). Bounded, esplicito.

### Pattern — read-path fence con opt-out anziché opt-in
Le 5 funzioni di read sopra hanno tutte default `status='owned'`. Il parametro
opzionale espone 'wishlist' / 'any', mai stringa libera. **Opt-out perché**:
un nuovo tool della famiglia rischia di mancare il filtro per dimenticanza, e
il default sbagliato pollutea silenziosamente i totali ("hai 58 giochi" dove
2 sono in wishlist). Default = scope sicuro, l'esplicito è "espandi".

Generalizzabile: ogni volta che si introduce uno stato che divide il dataset,
default al sottoinsieme "principale" e fai opt-in per il resto. Non viceversa.

### Pattern — `mark_as_owned` come single-column flip
Un UPDATE di una colonna, niente trasferimento riga a riga. Le tentazioni
"creiamo `wishlist_items` e quando compri sposti": NO. Schema unificato +
status flag = stato pulito, audit gratis ("status field: wishlist → owned"
riga del log di `changes`), zero risk di perdere campi nel transfer. **Da
ricordare**: ogni volta che pensi di "spostare un record tra tabelle", chiedi
"posso aggiungere una colonna che lo classifica?". 9 su 10 sì.

### Gotcha — `ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT` su SQLite funziona
Funziona perché il default è un literal (`'owned'`), non un'espressione.
SQLite rifiuta default non-costanti su ALTER. Per ora viva, ma sappiamo che
serve cautela su default dinamici (es. `CURRENT_TIMESTAMP` su ALTER fallisce
storicamente; meglio aggiungere la colonna NULL-able e popolarla con UPDATE
in un secondo step).

### Smoke test pattern riusato
Lo smoke test 18-step (`uv run python -c "..."`) sui tool ha catturato:
- Validazione enum (`priority='ultra'` → errore esplicito).
- Duplicate guard (`already exists` su name match case-insensitive).
- Cross-tool refusal (`update_wishlist` su owned, `remove_from_wishlist` su
  owned, `mark_as_owned` su già-owned).
- Fence verification (`list_games default` ritorna 56, non 58; `status='any'`
  ritorna 58).

Pattern stabile: per ogni gruppo di tool nuovi → script `python -c` end-to-end
con add/list/update/refusal/cleanup. Più veloce di test unitari per Boardy
(single-user, niente CI), ma assertable. **Sempre cleanup esplicito alla fine
con `delete_game` o `remove_from_wishlist` sui TEST_* row**, altrimenti il DB
accumula crud.

### Decisione UI — quick-add form senza enrichment
Form nella pagina `/wishlist` accetta solo nome + priorità + prezzo target.
**No web_search automatico dalla pagina**: l'enrichment BGG è esplicitamente
delegato alla chat ("aggiungi X alla wishlist" → web_search → confirm).
Motivo: la pagina è capture rapido, la chat è il rituale di conferma. Mischiare
i due flussi obbliga a duplicare la logica di "propose then confirm" in JS,
che è dove abbiamo già storicamente avuto bug. Microcopy `add-hint`
nell'add-dock punta l'utente alla chat per il flow completo.

---

## 2026-05-06 — UI polish pass: gotchas e preferenze del proprietario

Sessione di refactor estetico su tutte e tre le pagine (`/`, `/library`,
`/sleeves`). Cose che vale la pena ricordare per il prossimo lavoro UI.

### Gotcha — `<img>` con URL valorizzato ma broken
Il filtro `if (g.thumbnail_url) <img>` non basta: se l'URL è 404 / CDN morto /
cookie-gated, il browser disegna comunque un chrome di 1px ("bordino bianco
orrendo" — feedback testuale del proprietario su Here to Slay, HeroQuest,
Room 25, che hanno `thumbnail_url` impostato ma la GET fallisce).

**Pattern fix** (in `web/library.html`):
1. Stampa sempre `data-initial` e `--hue` sul `<div class="thumb">` wrapper,
   anche quando metti l'`<img>` dentro.
2. `<img onerror="thumbFallback(this)">` promuove il wrapper in placeholder
   in-place: `wrap.classList.add('placeholder')` + `wrap.textContent = wrap.dataset.initial`.
3. Niente refetch, niente skip silenzioso, niente broken-image chrome.

Generalizzabile a qualunque CDN-backed thumbnail in futuro.

### Pattern — full-bleed bar che si allinea con `main` sotto
La dock di `/sleeves` ha background full-width ma contenuto centrato che deve
combaciare con i KPI sotto a tutte le viewport.

**Trucco**: stesso `padding-x` orizzontale sulla bar (24px) e su `main`
(24px), poi sul wrapper interno della bar imposta `max-width = main_max -
2*padding` (cioè 1200 - 48 = 1152px) + `margin: 0 auto`. Risultato: gli edge
sono identici a viewport pieno e collassano insieme su viewport stretto
(entrambi diventano `viewport - 48px`).

`:focus-within` + transition su `max-width` con cubic-bezier overshoot
(`0.34, 1.56, 0.64, 1`) dà l'effetto "bouncy" CSS-only — zero JS, zero
listener.

### Decision — colori bolla utente prendono dalle CSS vars del brand
Iterazione cromatica su `--user`: prima slate freddo (`#2c3a4d`, troppo
simile al bot), poi verde-slate desaturato (`#2d4a37`, "smorto"), poi più
saturo (`#335a40`, "ancora un po' più vivo"), infine `var(--accent-dim)` /
`var(--accent)` direttamente.

**Regola**: se serve un verde in qualunque contesto (bolla, bottone,
hover), pescalo dalle custom properties esistenti. Niente più hex
hand-tuned: l'iterazione manuale è dispendiosa e crea drift cromatico.
Le palette vars stanno in cima al `:root` — `--accent`, `--accent-dim`,
`--user`, `--bot`, `--user-edge`, `--bot-edge`.

### Preferenze proprietario (regole stabili)
Confermate questa sessione, applicarle d'ufficio nei prossimi lavori UI:

1. **Niente `confirm()` / `alert()` nativi** — sempre custom modal
   Promise-based. Helper di riferimento: `confirmDialog()` in
   `web/index.html` (gestisce Esc, Enter, click sullo sfondo, focus
   automatico sul bottone primario, cleanup listener). Riutilizzabile.

2. **Header coerenti su tutte le pagine** — stesso padding (`14px 24px`),
   stessa h1 (`20px`, suffisso `· Chat`/`· Libreria`/`· Buste`), stesso
   ordine `h1 + nav + spacer`, stessa classe `.active` sul link corrente.
   Quando si crea/refattorizza una pagina, allineare anche le altre.

3. **Conteggi non ridondanti** — "56/56 giochi" è stato esplicitamente
   bocciato come "non ha senso". Mostrare un numero solo quando il
   denominatore non aggiunge informazione. La filter-state è già visibile
   dai dropdown / banner semantico.

4. **Empty-state non vuoti** (TODO low-priority ma flag esplicito):
   l'utente ha chiesto un empty-state con prompt suggeriti per la chat.
   Generalizzabile: dove c'è un "primo schermo vuoto" pensare a un seed
   visivo (chip, splash, microcopy).

5. **Animazioni "bouncy" piacciono** — l'ovvio: cubic-bezier con overshoot
   leggero sul focus dock è stato accolto bene. Gli stessi parametri
   funzionano per qualunque "expand on focus" futuro.

### Status piccoli pattern che hanno funzionato
- **`stripMarkdown()` per status-line clamp** (`/sleeves`): regex chain
  che converte `**bold**` / `[text](url)` / list markers in plain
  inline-text. Da usare ovunque un componente ha clamp 1-2 righe ma
  riceve markdown grezzo dal modello.
- **Hash-hue placeholder**: `sum(charCode * 31) % 360` → HSL deterministico
  da una stringa. 4 righe, niente librerie, output stabile. Riutilizzabile
  per qualunque "iniziale colorata su sfondo unico-ma-stabile" (avatar
  utenti, tag, categorie).
- **Sidebar conv-list con hover-reveal `×`**: `.conv-del { opacity: 0 }`
  + `.conv-item:hover .conv-del { opacity: 1 }`. Discoverability via
  hover, niente clutter. Pattern adottabile per qualunque list-row con
  azione destructive secondaria.

---

## 2026-05-04 — ETL upsert + name-divergence gotcha

### TL;DR
`etl/import_excel.py` ora fa upsert per `name` invece di DROP+CREATE. Chat-added
games sopravvivono al re-import. **Però**: se un nome è stato ripulito in chat
(typo Excel, virgola in coda, newline embedded), il re-import crea un duplicato
con il nome Excel originale. Diff Excel↔DB è ora un side effect visibile.

### Cosa ha funzionato
- DDL passato a `CREATE TABLE IF NOT EXISTS` — le migrazioni v3/v4 di
  `app/schema.py` rimangono autorevoli, l'ETL non le sovrascrive.
- UPDATE limitato alle colonne **ETL-managed** (players, duration,
  complexity_label, condition, sleeve_status). Tutto il resto (`bgg_id`,
  `description`, `description_embedding`, `complexity_weight`, `notes`,
  `thumbnail_url`...) sopravvive perché non è nell'UPDATE statement.
- Bridges designer/publisher e `sleeve_requirements` ricostruiti **solo per i
  giochi presenti in Excel** (`DELETE WHERE game_id=?` + INSERT). Chat-only
  games conservano i loro bridges intatti.
- Output: "Inserted N new, updated M existing, preserved K" rende la divergenza
  Excel↔DB ispezionabile a colpo d'occhio.

### Gotcha: nomi divergenti = duplicati silenziosi
Smoke test sul DB attuale (56 giochi, 3 chat-added):
```
Inserted 3 new, updated 53 existing
Preserved 3 game(s) not in Excel: Here To Slay, Il Signore dei Tortelli -Le Due Torri-, Sherlock Holmes ...
```
I "3 new" non erano davvero nuovi — erano gli **originali Excel** dei tre giochi
chat-added, perché in chat erano stati ripuliti:
| DB (chat) | Excel | Causa |
|---|---|---|
| `Here To Slay` | `Here To Slay, Gioco` | virgola+suffisso Excel |
| `Il Signore dei Tortelli -Le Due Torri-` | `Il Singore dei Tortelli ...` | typo "Singore" |
| `Sherlock Holmes Consulente Investigativo: ...` | stesso ma con `\n` embedded | wrap Excel |

Il match per nome è esatto (`SELECT id FROM games WHERE name=?`), quindi i 3
nomi Excel sono finiti come INSERT puliti accanto alle versioni chat. Ho
cancellato manualmente i duplicati dopo il test.

**Lezione**: l'upsert per nome funziona solo se chi ripulisce un nome in chat
ripulisce **anche** la cella Excel, oppure se aggiungiamo un layer di
matching fuzzy (Levenshtein / strip punteggiatura / normalizzazione newline).
Per ora: se il re-import stampa "Inserted N" e ti aspettavi 0, controlla i
nomi prima di accettare.

### Quando aggiungere fuzzy matching
Solo se la divergenza si ripresenta. Per ora la regola ad-hoc è: dopo un
re-import, ispezionare la sezione "Inserted N new" del log; se contiene nomi
che assomigliano a giochi già presenti, decidere se patchare Excel o il DB.
Non aggiungere normalizzazione preventiva — la stiamo facendo *due volte*
(import + chat-edit) e ognuna ha contesto diverso.

---

## 2026-05-04 — Coverage gap chiusa con backfill description-only

### TL;DR
4 giochi residui senza description (`7 Wonders II`, `I Coloni di Catan`,
`Il Signore dei Tortelli -Le Due Torri-`, `War Chest`) ora indicizzati. Coverage
56/56 (100%), `excluded_count=0` su qualsiasi query semantica.

### Cosa ha funzionato
Nuovo script `etl/backfill_descriptions_websearch.py` complementare al
`backfill_descriptions_tavily.py` esistente. Differenze chiave:

| Aspetto | tavily (esistente) | websearch (nuovo) |
|---|---|---|
| Domini search | Solo BGG | BGG + Wikipedia IT/EN + 6 publisher |
| Schema estratto | All-fields (~16 campi) | Solo `description` |
| LLM | DeepSeek `json_object` | DeepSeek `json_object` (payload più piccolo) |
| Manual override | No | `--manual TEXT` |

L'insight: **ridurre la superficie del JSON output elimina i bug di parsing**.
Lo script "all-fields" falliva su `War Chest`/`I Coloni` con
`Unterminated string`. Stesso provider, stessa modalità, ma un payload
con solo `{description: "..."}` non innesca i casi limite del json_object
mode di DeepSeek. **Lezione**: quando un task si può scomporre in due
prompt — uno generale e uno mirato — i fallimenti del primo si
recuperano col secondo senza cambiare provider.

### Cosa ha richiesto override manuale
Due dei quattro non sarebbero mai usciti dall'auto-extraction, indipendentemente
dal provider:
- **`Il Signore dei Tortelli`**: parodia italiana del SdA, non esiste su BGG né
  su Wikipedia. Niente da estrarre, serve descrizione utente-fornita.
- **`I Coloni di Catan`**: il LLM continua a confondere col "Catan Card Game"
  perché Tavily restituisce tante varianti (Card, Histories, Junior, ...). Il
  modello giustamente fa skip per non guessare. Per l'utente quel nome è
  inequivocabile (= Catan classico, edizione italiana 1999), quindi
  `--manual` è l'output corretto.

→ **Pattern generale**: per giochi ambigui/parodia/edizioni non standard,
`--manual` non è un fallback "di emergenza" ma la soluzione naturale.
Un editor inline su `/library` resta utile per onboarding di nuovi giochi
in chat (vedi TODO medium), ma per il backfill batch il flag CLI basta.

### Bug fix collaterale
Operator precedence nel WHERE del nuovo script:
```sql
-- BUG (in script vecchio): AND lega più stretto di OR
WHERE description IS NULL OR description='' AND LOWER(name) LIKE ?
-- Equivale a: WHERE description IS NULL OR (description='' AND LOWER(name) LIKE ?)
-- → tutti gli IS NULL passano a prescindere dal --only filter

-- FIX (nel nuovo script):
WHERE (description IS NULL OR description='') AND LOWER(name) LIKE ?
```
Lo script `backfill_descriptions_tavily.py` ha lo stesso bug — flagged
ma non patchato per non sporcare questa modifica. Fix indipendente.

### Conseguenza per gli aggiornamenti futuri
L'auto-embed hook su `add_game`/`update_game` significa che ogni gioco
nuovo o modificato si indicizza da solo. Lo script websearch resta
disponibile per:
- Re-import o reset DB (rifaresti backfill_v2 → backfill_tavily → websearch
  come "secondo passaggio" sui residui).
- Giochi aggiunti via chat senza description (l'`add_game` da chat la
  popolerebbe se il modello la include, altrimenti websearch in batch).

### Credito Anthropic finito
Primo tentativo del nuovo script con Anthropic Haiku: HTTP 400
"Your credit balance is too low". Ho switchato a DeepSeek (key già nel
`.env`, provider attivo). Da considerare: il `LLM_PROVIDER=deepseek` nel
`.env` è ancora il default operativo nonostante MEMORY note dicesse
".env tornato ad Anthropic" — la realtà è DeepSeek attivo, Anthropic key
presente ma a credito 0. Aggiornare la memory.

---

## 2026-05-03 (PM, post-mortem) — Skip-reason column + tool surfaces excluded games

### TL;DR
Two follow-ups to the backfill run:
1. **Schema v5**: `games.description_skip_reason TEXT` (idempotent migration).
   Backfill script now writes the reason on skip/error, clears on success.
   So next time we run it, the laggards stay visible — no need to grep
   stdout from a past run.
2. **Tool surface**: `search_games_semantic` now returns `{count, items,
   excluded_count, excluded}`. The model is told (in the tool description)
   that when `excluded_count > 0` it MUST tell the user "ti ricordo che N
   giochi non sono inclusi nella ricerca semantica perché senza
   descrizione" and list them. Otherwise the model silently presents an
   incomplete subset as if it were the whole collection — exactly the
   counting-style bug we fight elsewhere.

### Fixed: DeepSeek json_object apostrophe bug (mostly)
Two complementary fixes after observing the deterministic
unterminated-string failure on "Memoir '44 - Refresh" and "War Chest":
- **Prompt-side**: explicit STRICT JSON FORMATTING block (single-line
  strings, ASCII apostrophes, escape rules, no trailing commas). Special
  carve-out for titles containing `'` — instructed to either rewrite
  ("Memoir 44") or ensure plain ASCII. Cheap, no-cost change.
- **Code-side**: `_try_repair_json()` fallback. On `JSONDecodeError`, first
  try curly→ASCII normalization (’ → ', “ → "), then collapse newlines
  inside the payload, then give up. Catches the residual cases the prompt
  doesn't fix. Two lines of code, kills 90% of the failure mode.
The two fixes stack: the prompt prevents most failures, the repair
catches the rest. **Result on the rerun (2026-05-03 PM): 4/8 of the
previously-skipped games now indexed → final coverage 52/56 (93%).**
Memoir '44 - Refresh recovered (was a JSON-parse fail before, OK now
thanks to the prompt's apostrophe rule). 4 still out: "7 Wonders II"
(genuine ambiguity), "Il Signore dei Tortelli" (likely fan-game),
"I Coloni di Catan" + "War Chest" (residual JSON-parse — DeepSeek still
finds new ways to break json_object on these specific raw_contents,
likely needs an Anthropic retry path or a stricter repair).

### Why not switch to Anthropic just for the failures
Considered briefly. Rejected because:
- DeepSeek's failure mode is a known, fixable formatting bug — not a
  fundamental quality issue. Switching providers for one prompt is the
  kind of "if you have a hammer" decision that obscures the real fix.
- Sonnet costs ~10× more for an extraction task that DeepSeek does well
  in 95% of cases. The user already accepted that trade-off when they
  set LLM_PROVIDER=deepseek.
- A repair fallback is provider-agnostic — works the same day Anthropic
  has its own JSON-mode quirk in the future.

### Reusable pattern: persisted skip-reason on rows
The `description_skip_reason` column is a tiny pattern with outsized
value: it turns "I tried to fill X and failed" into a queryable, durable
fact instead of a one-shot stdout message. Now the model can answer
"perché Sushi Go Party ha la description e Memoir no?" by reading the
column. Worth repeating for any future enrichment pipeline (PDF→summary,
manual-edit hints, etc).

---

## 2026-05-03 (PM) — Tavily+DeepSeek backfill of missing BGG descriptions

### TL;DR
After landing semantic search, only 32/56 games had a description (the rest
were never enriched by BGG). Built `etl/backfill_descriptions_tavily.py`:
Tavily search restricted to boardgamegeek.com → DeepSeek (json_object mode)
extracts a structured payload → `update_game(...)` writes it and the
auto-embed hook indexes the new description. **Result: 48/56 indexed
(+16 in ~3 minutes), 0 errors, 8 skipped.**

### Skip reasons (the interesting part)
- **4 ambiguous Italian editions / fan-titles** ("7 Wonders II", "7 Wonders II Cities",
  "I Coloni di Catan", "Il Signore dei Tortelli -Le Due Torri-"). DeepSeek
  correctly returned `{"skip": true, "reason": "..."}` rather than guessing
  metadata for the wrong base game. Good behavior — we *want* this kind of
  refusal because the alternative is poisoning the embedding with the wrong
  description.
- **2 too-generic names** ("Duel", "Elfenland De Luxe"). Same as above.
- **2 DeepSeek JSON-mode bugs**: "Memoir '44 - Refresh" and "War Chest" both
  failed with `json parse error: Unterminated string` — the model produced
  unescaped apostrophes inside a string field even with `response_format=
  {"type": "json_object"}`. Reproducible. Fix: retry on Anthropic for those
  two, or post-process with a fixup (e.g. `json5`).

### Why DeepSeek over Sonnet for this
LLM_PROVIDER=deepseek is the active chat provider; ~10× cheaper than Sonnet
for an extraction task that doesn't need frontier reasoning. JSON-mode is
adequate apart from the apostrophe escape bug. Total cost for 24 games:
~$0.005 in DeepSeek + 48 Tavily credits.

### Coverage gap → semantic search blind spot
Games without a `description` are filtered out by
`games_semantic.search_semantic` (`WHERE description_embedding IS NOT NULL`).
So 8/56 games (14%) are currently INVISIBLE to vibe queries. This is
intentional — embeddings don't exist for them — but the model must be told
this so it doesn't claim "no results" when really "no embeddings yet".
Tracked in TODO.md High priority. Two recovery paths worth pursuing:
- PDF→description for games whose rulebook is already ingested (RAG infra
  is right there).
- Manual textarea editor on `/library` for fan-made / Italian-only titles
  that BGG won't have.

### Tavily query pattern that works
`query=f"{italian_or_english_name} boardgame BGG"` +
`include_domains=["boardgamegeek.com"]` + `search_depth="advanced"` +
`include_raw_content=True` + `raw_content_chars=4000`. Concatenate the top
3 results' raw_content with `## <url>` headers so the LLM has provenance.
Capping each raw_content to 4000 chars keeps the prompt ≤8000 chars
(also capped in the script). Worked even when the user-owned name was
Italian (Tavily fuzzy-matches on BGG's "alternate names" field).

### Reusable extraction prompt structure
The key pattern in `EXTRACT_PROMPT`:
1. State the user-owned game name explicitly upfront.
2. Hand over RAW BGG content.
3. Ask for a JSON object with a fixed schema, but say "omit any field you
   can't confidently fill — DO NOT guess."
4. Provide an explicit escape: `{"skip": true, "reason": "..."}` when the
   page is for the wrong game / edition / expansion.
The escape clause is what saved us from polluting the DB with wrong data
on the 6 ambiguous cases.

---

## 2026-05-03 — Semantic "vibe" search over `games.description`

### TL;DR
Hybrid retrieval: SQL filters narrow the candidate set, then cosine
similarity on an e5-base embedding of the BGG description ranks the
survivors. New tool `search_games_semantic(query, players?,
max_complexity_weight?, max_duration_min?, sleeve_status?,
category_contains?, mechanic_contains?, k=10)`. Use case the user actually
asked for: "gioco da viaggio portatile facile da imparare per colleghi
di lavoro in pullman e hotel" → top-5 includes Sushi Go Party (party,
20min) and Obscurio (coop, 45min) with cosine ≥0.77.

### Why hybrid, not pure embedding
The example query mixes three signals:
- "facile da imparare" → numeric, already in `complexity_weight` — filter first.
- "in 4 giocatori" → numeric range — filter first.
- "portatile / facile / colleghi" → semantic vibe — embedding ranks.

Pure cosine over the description would underperform on hard constraints
(weight=4 game still ranks high if its blurb mentions "easy to learn the
basics"). Pure SQL filtering leaves the model to pick winners from
hundreds of candidates. The combo gives both: hard filters cut the pool,
embedding picks the vibe match.

### Implementation
- Schema v4 (`app/schema.py:_migrate_v4_description_embedding`):
  idempotent `ALTER TABLE games ADD COLUMN description_embedding BLOB`
  + `description_hash TEXT`. Hash = SHA1 of the description used to
  embed; lets us skip re-encoding when nothing changed.
- New module `app/games_semantic.py`: `embed_one(game_id)`,
  `reindex_all(force=False)`, `search_semantic(query, **filters, k)`.
  Reuses `_model_lazy()` from `rulebooks.py` so we don't load the
  280MB model twice. Same e5 prefixes (`passage:` for docs,
  `query:` for queries).
- Auto-embed hook in `add_game` / `update_game` (try/except — never
  fails the write). Hash check inside `embed_one` makes a sleeve-only
  update a no-op for the embedding pipeline.
- Backfill script `etl/embed_descriptions.py` (uses argparse,
  `--force` to rebuild from scratch). 32/56 games had a description on
  2026-05-03; the other 24 still need BGG enrichment via
  `backfill_v2.py` and will pick up the embedding automatically next
  time they're updated.

### Embedding storage shape
e5-base = 768 dims × float32 = 3072 bytes per row. SQLite holds 56
games → ~170KB total. Brute-force cosine in NumPy is fine; no need for
sqlite-vec or a vector store. Same scaling argument as the rulebook RAG.

### Gotcha — don't filter too hard
SQL filters use `IS NULL OR <= X` for `complexity_weight` and
`duration_min` because BGG enrichment is incomplete. A strict
`weight <= 2.5` would silently drop 24 games we own that lack the
metadata, even if their description matches the vibe. Better to let
them through and let cosine speak. If the user explicitly said "only
games where I know the weight", we'd add a `require_metadata=True`
flag — not worth the schema bloat for a hypothetical case.

### Score thresholds (e5-base, multilingual)
Empirically on this collection:
- ≥0.78 = strong match (model is confident)
- 0.72–0.77 = borderline, mention with reservation
- <0.72 = noise, say "nessun match forte" rather than overselling

These are lower than monolingual English models because the e5 multilingual
backbone trades some sharpness for IT/EN flexibility — fine for our case,
the user types both languages.

---

## 2026-05-01 (PM) — `/sleeves` dashboard + frontend rerender bug

### TL;DR
New page `/sleeves` consolidates the inventory workflow that previously
lived only in chat. Three sections: KPI cards, "Da comprare" table from
`sleeve_summary`, and an inventory editor with inline +/- preset buttons
(`-50 / -10 / +10 / +50 / +100`) per row. Plus a quick-add form and a
mini-chat with its own `conversation_id` (localStorage key
`boardy_sleeves_conv_id`) so sleeve-focused turns don't pollute the
main chat. Library got a Buste status pill + filter; shared nav.

### Frontend bug found along the way
`web/index.html:rerender()` assumed Anthropic shape only
(`assistant.content` is array of blocks). After the DeepSeek switch the
shape became OpenAI (`content: "string"` + separate `tool_calls`) and
the rerender silently skipped every assistant turn — reloaded
conversations showed only user bubbles. Now accepts both shapes per
turn so mixed-provider histories render correctly. The `/sleeves`
mini-chat reuses the same dual-shape logic.

### Server endpoints added (`app/main.py`)
- `GET /sleeves`, `GET /sleeves/data` — read-only dashboard payload.
- `POST /sleeves/inventory/delta` — wraps `add_to_inventory`,
  audit-source `web:sleeves`.
- `POST /sleeves/inventory/upsert` — wraps `update_inventory` for the
  add-form (absolute count, useful for "ho 100 buste di X").
- The "save" button per inventory row sends only the delta (not the
  absolute), so the audit log records `delta=+50` etc. — matches the
  semantics chat uses.

### UX detail worth keeping
Numeric table columns are right-aligned (digits line up by place value,
magnitudes scan instantly: "569" sticking out left of "29" reads as
"bigger" without parsing each number). Headers must match cell
alignment — initial bug here was `td.right` only matching td, leaving
headers left-aligned over right-aligned data. Fixed to `th.right,
td.right`.

### Open follow-ups
- Mini-chat does NOT currently restrict the model to sleeve topics —
  the same provider/system-prompt as the main chat. Felt over-engineered
  for now; revisit if the chat strays into off-topic territory.
- Library still doesn't show per-game sleeve sizes (only the status
  pill). Decided against — the detail belongs on `/sleeves`. Reopen
  only if multiple users disagree.

---

## 2026-05-01 — Tavily raw_content + count envelope (LLMs can't count)

### TL;DR
Two surgical fixes to chat quality:
1. **Web search now returns FULL page text**, not just SERP-style snippets.
   `web_search` defaults flipped to `search_depth='advanced'` +
   `include_raw_content=True`. The model reads `raw_content` (full markdown-
   cleaned page) for facts, not the snippet.
2. **List-returning tools now wrap results in `{"count": N, "items": [...]}`**.
   Verified bug: the model was writing "28 giochi" in a header while the list
   below had 29 items, even when it had just received `list[29]` from the
   tool. The integer wasn't reachable via attention; the count envelope makes
   the integer literal a token the model can transcribe.

### The bug, in numbers
Test query: "Dammi la situazione completa della mia collezione".

DB reality: sleeved=29, to_sleeve=10, na=14, unknown=3, total=56.

Before the fix:
| Status | DB | Header | List items |
|---|---|---|---|
| sleeved | 29 | **28 ❌** | 29 |
| to_sleeve | 10 | 10 | 10 |
| na | 14 | **15 ❌** | 14 |
| unknown | 3 | 3 | 3 |

The off-by-one errors **cancel** (28+10+15+3=56), so the totals look right
while two headers are wrong. Classic LLM backsolve under a soft "must total
N" constraint. The model had `list[29]` literally in its tool result,
*and still wrote 28*.

After the fix (same query, second run): every header matches the DB.

### Root cause analysis (worth keeping)
The "anti-hallucination" rule "header MUST equal len(list_below)" doesn't
work because **LLMs can't reliably count list elements in attention**. They
estimate. Counting tokens is the same family of problem as counting letters
in a word — well-known weakness.

The fix is structural: don't ask the model to count, give it the count.
`{"count": 29, "items": [...]}` puts the integer literal in the tool result
where it's a single transcribe-this-token operation.

### What changed in code
- `app/tools.py` — `list_games`, `sleeve_summary`, `list_inventory`,
  `recent_changes`, `list_dimension`, `list_rulebooks` all now return
  `{"count": N, "items": [...]}`. No external Python callers, only the LLM
  consumes them, so no migration needed elsewhere.
- `app/tools.py` — `web_search` now uses `search_depth='advanced'` +
  `include_raw_content=True` by default. New params: `include_raw_content`
  (bool), `raw_content_chars` (int cap, default 6000 chars to control
  context size). Each result item carries a `raw_content` field with the
  full page text.
- `app/chat.py` — system prompt (BASE + SLIM):
  - Replaced "header MUST equal len(list)" rule with **"COUNT FIELD IS THE
    TRUTH — TRANSCRIBE the `count` field verbatim, never re-estimate"**.
  - Added "READ `raw_content`, NOT `content`" rule to the web_search
    section, with rationale (snippet = misleading SERP excerpt; raw = full
    page).
  - Updated the verbalize-`sleeve_summary` example to show the new
    `{count, items}` shape.
- `app/chat.py:_log()` — Windows cp1252 crash fix. The arrow `→` in result
  log lines (`result list_games → list[56]`) was raising `UnicodeEncodeError`
  and turning chat 500. Now catches and strip-encodes; telemetry can never
  break the chat.

### Tavily numbers (one search, one game)
For "Wingspan sleeves" with `include_domains=['sleeveyourgames.com']`:
- `content` (snippet): "Added to your shopping list. Add Sleeve Data…" —
  unhelpful.
- `raw_content` (full page): structured table with mm sizes, pack counts,
  brand models (Mayday MTL257, Paladin Gawain PALGAWCLR, Sapphire
  SPORANGE), 57.0/57.5 × 89.0 mm. Exactly the data we want.

Cost: advanced search = 2 Tavily credits/query (vs 1 for basic). Still
fine on the 1000/month free quota for personal use.

### Open follow-ups
- **`get_game` and `ask_rules` still return un-enveloped objects.** Not
  applied because `get_game` returns a single game (no count) and
  `ask_rules` returns `{game, question, chunks}` where `chunks` is the
  array. Could wrap `chunks` in `{count, items}` for symmetry, but the
  model uses `ask_rules` differently (pick best chunk, cite page) — count
  doesn't matter for that flow.
- The new "TRANSCRIBE count verbatim" rule has been verified on 2 queries
  (full collection + top-5 publishers). Watch for regressions on novel
  query shapes (cross-product groupings, paginated results).

---

## 2026-04-29 (Late PM) — DeepSeek + Tavily, sleeve schema redesign, import bug fix

### TL;DR
Massive cleanup session. Three big changes:
1. **Provider**: switched default from Anthropic Sonnet to **DeepSeek-chat** via the
   OpenAI-compatible endpoint (~10× cheaper). `web_search_20250305` (Anthropic-only)
   replaced by a **client-side `web_search` tool backed by Tavily** — works the
   same with any provider.
2. **Sleeve schema redesign** — `sleeve_raw` dropped (Excel artifact, fully
   redundant with `sleeve_status` + `sleeve_requirements`). `sleeve_requirements`
   reinterpreted as a **TODO list** (only games NOT yet sleeved have rows).
   Status `no` collapsed into `na`. Cascade-on-status-flip + guard rules enforce
   the invariant.
3. **Import bug**: `classify_sleeve()` defaulted numeric-only Excel cells to
   `sleeved`. Wrong — those cells listed *card sizes*, not sleeving status.
   Fixed default to `unknown`. 5 games (Gloomhaven JoL, Room-25, HeroQuest,
   Memoir '44, Obscurio) restored from audit log to `to_sleeve` with their
   original requirements.

### What's new in code
- `app/llm.py` — new `DeepSeekProvider` (subclass of `OllamaProvider` with own
  api_key + base_url). Removed `WEB_SEARCH_TOOL` server-side config from
  `AnthropicProvider`. `supports_web_search` → `prefer_slim_prompt` (only Ollama
  uses slim now; both API providers use the full BASE prompt).
- `app/tools.py` — new tool `web_search(query, include_domains?, max_results?,
  search_depth?)` Tavily-backed with `DEFAULT_TRUSTED_DOMAINS` allowlist
  matching the old Anthropic one. Total tools: **16** (was 15).
- `app/tools.py` — `update_game` now does **cascade-clear** of
  `sleeve_requirements` when status flips to `sleeved`/`na`. Audit-logged with
  `cascade=status->X` suffix on source. `set_sleeve_requirements` **rejects**
  with explicit error if game is already `sleeved`/`na`.
- `app/schema.py` — `NEW_GAMES_DDL` no longer contains `sleeve_raw`. Added
  idempotent v3 migration `_migrate_v3_drop_sleeve_raw` (drops column +
  collapses leftover `no`→`na` if present at boot).
- `etl/import_excel.py` — `classify_sleeve` rules refined:
  - `Sleeved`/`Sleevato` → `sleeved, []`
  - `No`/`n.a.`/`na`/`n/a` → `na, []`
  - "DA COMPRARE …" → `to_sleeve, [reqs]`
  - "COMPRATE …" → `sleeved, []` (invariant: drop reqs even if parsed)
  - **only numeric data, no marker** → `unknown, [reqs]` (was `sleeved`!)
  Plus a defensive belt-and-braces: even if classify_sleeve returns reqs for a
  `sleeved`/`na` status, the INSERT path forces `reqs = []`.
- `app/chat.py` — system prompts:
  - BASE: rewrote sleeve section with **TWO sources + invariant** + cascade
    behavior; added strict count-integrity rule ("the number you write in a
    header MUST equal len(list_below)").
  - SLIM: compressed version of the same rules.
  - Web search guidance moved into BASE (English game names, sleeveyourgames
    flow, BGG flow).
  - Anti-hallucination: "for full-collection queries call `list_games()`
    with NO filters first — prior tool results are subsets, never recall from
    memory."
- `app/chat.py` — **terminal logging** of every tool-use round. `[boardy]`
  prefix lines on stdout via `_log()` show conv id, round, tool calls with
  truncated args, and tool results with size+shape. Visible live in the
  uvicorn terminal — no UI work needed.

### Cleanup scripts (all in `etl/`, all idempotent, all audit-logged)
- `cleanup_sleeve.py` — dropped `sleeve_raw` column (audited 44 prior values),
  collapsed 3 `no` rows into `na`. Sources: `cleanup_sleeve_v3_drop_raw`,
  `cleanup_sleeve_v3_collapse_no_to_na`.
- `sync_sleeved_status.py` — removed phantom requirements from 11 sleeved
  games (1807 sleeves of inflation in `sleeve_summary.to_buy`).
  Source: `sync_sleeved_status_2026-04-29`.
- `fix_misclassified_sleeve.py` — restored 5 games to `to_sleeve` and
  re-inserted their original requirements pulled from the
  `sync_sleeved_status_2026-04-29` audit rows.
  Source: `fix_misclassified_sleeve_2026-04-29`.
- `fix_encoding.py` — renamed 3 games (`Here To Slay, Gioco` → `Here To Slay`;
  `Singore`→`Signore`; stripped `\n` from Sherlock Holmes).
  Source: `manual_encoding_fix_2026-04-29`.

### The recurring lesson: model hallucinates counts even with anti-hallucination prompt
Even with the "ALWAYS call tools, never invent" rule already in place, the
model wrote count headers (e.g. "5 to_sleeve") that didn't match the lists it
then printed (6 items). The fix is to make the rule **operational**:
> the number you write MUST equal `len(list_below)`. Count by enumeration,
> not from memory. There is no situation where a header count and its list
> disagree.
TBD if this works in practice — needs another full-collection query to verify.

### Costs
- DeepSeek-chat: ~$0.27/M input, ~$1.10/M output. ~10× cheaper than Sonnet 4.6.
- Tavily: 1000 searches/month free (`TAVILY_API_KEY`). Paid tier ~$4/1000 ricerche
  if exceeded. Boardy uses ≤5 ricerche per query, easy to stay under.
- Net: personal-use Boardy budget ~$0.10/month on DeepSeek + free Tavily.

### Recovery commands (if something needs un-doing)
```sql
-- Original sleeve_raw values
SELECT row_label, old_value FROM changes
WHERE source='cleanup_sleeve_v3_drop_raw' ORDER BY id;

-- Original requirements deleted by sync
SELECT row_label, old_value FROM changes
WHERE source='sync_sleeved_status_2026-04-29' AND field='requirements';

-- Misclassified games before fix
SELECT row_label, old_value, new_value FROM changes
WHERE source='fix_misclassified_sleeve_2026-04-29';
```

### Next-time touchpoints
- The 23 sleeved games with NO requirements stay as-is (Wingspan, Sagrada
  family, 7 Wonders family, etc.). User confirmed: "fine to leave them — they're
  done, no decision needed for buying." If user ever wants size info for those,
  use `web_search` on sleeveyourgames per game on demand.
- If Tavily quota becomes an issue, switch the `web_search` impl to Brave
  Search or self-hosted SearXNG — the tool interface is provider-agnostic, only
  the function body in `app/tools.py:web_search` needs swapping.
- `Le Leggende di Andor - L'ultima speranza` has a curly apostrophe (`'`) in
  the DB; ASCII apostrophe lookup fails. Cosmetic; fix if it bites.

---

## 2026-04-29 (PM) — Local LLM archived: hardware + 7B not enough for Boardy

### TL;DR
Tornati ad Anthropic Sonnet 4.6 dopo aver portato a termine la fase di benchmark.
Il provider abstraction resta in codice (zero rollback); è solo un flip di `LLM_PROVIDER`
in `.env` per riattivarlo. Motivi: hardware sbagliato + 7B troppo piccolo per il
tool-use complesso di Boardy. **Non è un bug fixabile a parità di hardware.**

### Numeri reali misurati su HP ZBook G1a (Ryzen AI 7 PRO 350 + Radeon 860M + 32GB)
| Config | Eval rate | Note |
|---|---|---|
| Vulkan iGPU + flash_attn | 5.54 tok/s | GPU usata al 100% ma più lenta del CPU |
| **CPU + flash_attn** | **5.74 tok/s** | Baseline reale, marginalmente più veloce |
| End-to-end "quante buste mi mancano?" | **254 secondi** | tool-loop + prefill |

### Insight strutturale: iGPU AMD non aiuta su questo hardware
- La Radeon 860M è RDNA 3.5 ma è una **iGPU che condivide la RAM col CPU**.
  Senza VRAM dedicata, zero vantaggio di bandwidth → il bottleneck (memory-bound
  inference su modelli 7B Q4) resta identico tra CPU e iGPU.
- Vulkan attivato via `OLLAMA_VULKAN=1` mostra `library=Vulkan` con 16.1 GiB
  "VRAM" (in realtà RAM riassegnata) ma il throughput è uguale o peggiore.
- **Lasciato `OLLAMA_VULKAN=0`** come env utente persistente; `OLLAMA_FLASH_ATTENTION=1`
  attivo (dà +32% reale su CPU).
- **NPU AMD XDNA da 50 TOPS NON usata** da Ollama. Per sfruttarla serve sostituire
  runtime con AMD Ryzen AI Software / Lemonade SDK — side-project a sé, non
  praticabile come "ottimizzazione" di Ollama.

### Insight: 7B Q4 non basta per tool-use complesso in italiano
- Qwen2.5 7B Q4 con `num_ctx=8192` (Modelfile ok, vedi sotto) regredisce comunque
  emettendo tool calls **come testo letterale** (`[tool_call sleeve_summary()]`,
  `{"name":"sleeve_summary","arguments":{}}`).
- Few-shot inseriti per insegnare a verbalizzare i JSON di `sleeve_summary` /
  `add_to_inventory` hanno **peggiorato il problema**: avevo usato `[tool_call X → {...}]`
  come marker pseudocodice nei `## Examples`, e il modello l'ha imitato pari pari.
  **Lezione: nei few-shot per modelli piccoli, mai usare sintassi che assomiglia
  a struttura tool-use.** Ho riscritto la sezione in forma puramente dichiarativa
  ("If sleeve_summary returns rows like X, reply like Y") — non testato a fondo
  perché abbiamo deciso di mollare prima.
- Il salto a `qwen2.5:14b-instruct` non è stato provato (entra in 32GB ma a ~3 tok/s
  diventa inutilizzabile). Per Boardy serve almeno un 32B+ con tool-use serio,
  che non gira su questa macchina.

### Cosa resta nel codice (preservato, riusabile)
- `app/llm.py`, `app/chat.py` (con `SYSTEM_PROMPT_SLIM` aggiornato), `app/tools.py`
  invariati. `_build_system_prompt(supports_web_search)` sceglie il prompt giusto.
- `boardy-qwen.Modelfile` + `test_local.py` restano nel repo.
- `.env`: `LLM_PROVIDER=anthropic`, riga `LLM_MODEL=boardy-qwen` commentata.

### Quando riaprire il discorso
- Se cambi hardware (dGPU NVIDIA con VRAM dedicata, o nuovo laptop con stack
  Ryzen AI funzionante in Ollama).
- Se esce un modello 4-7B con tool-use davvero buono (Llama 4 small? Qwen3?).
- Se Anthropic alza i prezzi al punto da pesare sul budget personale (oggi
  Boardy stimato ~$1-5/mese, non un problema).

### Gotcha confermati durante il tentativo (utili per il futuro)
- Ollama OpenAI-compat ignora silenziosamente `extra_body.options` → bake in
  Modelfile. (vedi sezione storica sotto)
- Context overflow → tool-call regression silenziosa. (idem)
- Prima richiesta dopo `ollama serve` è glaciale (load+kernel+prefill stack).
- Su Windows, `taskkill //F //IM ollama.exe` chiude il runner ma **non** la
  "ollama app.exe" che fa da launcher; vanno killati entrambi prima di un riavvio
  pulito con env vars nuove.
- `setx VAR VALUE` aggiorna il registro ma le shell già aperte vedono ancora il
  vecchio valore — il riavvio Ollama deve avvenire da una shell nuova oppure
  passandolo inline (`OLLAMA_VULKAN=1 ollama serve`).

---

## 2026-04-29 (AM) — Local LLM provider (Ollama) — half-shipped, two open issues

### What's new
- `app/llm.py` introduces a **provider abstraction**: `Provider` ABC + two
  implementations (`AnthropicProvider`, `OllamaProvider`) + a `get_provider()`
  factory selected by `LLM_PROVIDER` env var. The chat loop in `app/chat.py`
  now talks to providers, not directly to a vendor SDK.
- Vendor-neutral content blocks (`TextBlock`, `ToolUseBlock`, `ProviderResponse`)
  modeled after Anthropic's shape — that was already the on-disk history format
  in `conversations.history_json`, so keeping the model-facing dataclasses
  Anthropic-shaped means **OllamaProvider translates on input/output rather
  than rewriting stored history**. Switching back-and-forth between providers
  mid-conversation works.
- `OllamaProvider._tool_anthropic_to_openai` wraps Anthropic schemas
  (`name`, `description`, `input_schema`) in OpenAI's `function` envelope.
  Same JSON Schema inside; only the wrapper differs. Zero schema duplication.
- `app/chat.py` has dual system prompts:
  - `SYSTEM_PROMPT_BASE` (~2.5k tok) + `SYSTEM_PROMPT_WEBSEARCH_ADDENDUM`
    (~150 tok) for Anthropic. Cache_control makes the length cheap.
  - `SYSTEM_PROMPT_SLIM` (~470 tok) for Ollama. CPU prefill is the bottleneck;
    every saved token shaves ~30ms. Slim prompt explicitly forbids tool-call-
    as-text emission.
- `web_search_20250305` config (allowed_domains, etc.) lives **inside**
  `AnthropicProvider`, not at module top-level. Ollama doesn't see it.
- `boardy-qwen.Modelfile` — derived model from `qwen2.5:7b-instruct` with
  `num_ctx=8192` and `temperature=0.3` baked in. **Required workaround**: see
  gotcha §"Ollama OpenAI-compat ignores extra_body options" below.
- `test_local.py` at project root — smoke test that runs 6 realistic Italian
  prompts through the local model and reports tool-routing accuracy. Useful
  for evaluating any new model before swapping it in.

### How to switch providers
```
# .env
LLM_PROVIDER=anthropic                    # default; full Sonnet experience
# or:
LLM_PROVIDER=ollama
LLM_MODEL=boardy-qwen                     # the Modelfile-derived one
```
Restart uvicorn after changing `.env` — `python-dotenv` reads it at process
start. The provider is then re-instantiated per request, so future env
changes (e.g. swapping models) take effect on the next chat without app
reload of the abstraction layer (only of the env var).

### Gotcha — Ollama OpenAI-compat silently drops `extra_body.options`
- We tried passing `extra_body={"options": {"num_ctx": 8192}, "keep_alive": "30m"}`
  to `client.chat.completions.create()`. Both were **silently ignored** by
  Ollama's `/v1/chat/completions` endpoint. `ollama ps` continued to show
  `CONTEXT 4096` and `UNTIL 4 minutes from now`.
- The fix: **bake parameters into a Modelfile**. `boardy-qwen.Modelfile`:
  ```
  FROM qwen2.5:7b-instruct
  PARAMETER num_ctx 8192
  PARAMETER temperature 0.3
  PARAMETER top_p 0.9
  ```
  then `ollama create boardy-qwen -f boardy-qwen.Modelfile`. This is
  idempotent, lives in the repo, and works with any client.
- General rule for Ollama customization: **trust Modelfile, distrust extra_body**.

### Gotcha — context overflow ⇒ silent tool-call regression
- With Boardy's full system prompt + 16 tool schemas, a single chat turn
  exceeds 4096 tokens. When Ollama silently truncates, Qwen2.5 doesn't error
  out — it regresses to its base-model template behavior and starts emitting
  tool calls **as chat text**: `{"name": "sleeve_summary", "arguments": {}}`
  printed verbatim, sometimes preceded by hallucinated tokens like "Sinistro".
- Symptom in the UI: the user sees the JSON of the tool call inside the chat
  bubble as if it were prose. The actual `tool_calls` field on the response
  is empty, so the chat loop has nothing to dispatch.
- Diagnostic: check `ollama ps` — if `CONTEXT 4096` and your prompt is bigger,
  you're hitting this. Bump via Modelfile.

### Gotcha — first request after server boot is glacial
- Three costs stack on the first turn after a fresh `ollama serve`:
  1. Model load from disk (~4.6 GB → RAM): 30-60s
  2. Kernel compilation (per-load, one-time): a few seconds
  3. System prompt + tools prefill: ~50-100s on CPU at ~30 tok/s for
     ~3000 tokens (Anthropic-equivalent prompt) or ~15s for the slim 470-token
     prompt.
- After the first turn, the model stays resident (default 5min keep_alive,
  unfortunately also not bumpable via extra_body — set per-request via the
  raw `/api/chat` endpoint or wait for it to reload).
- Subsequent turns reuse partial KV cache for the system prompt: still slow
  on CPU but ~2-3× faster than the cold path.

### Open issues (resume here next session)
1. **Inference speed** — `ollama ps` shows `100% CPU` on a Ryzen AI 7 PRO 350
   with Radeon 860M iGPU (RDNA 3.5). Ollama on Windows + AMD APU has patchy
   GPU support; the iGPU should be usable via Vulkan or ROCm but isn't being
   picked up automatically. Diagnostic: read `ollama serve` startup log for
   GPU-detection lines. Possible levers: Vulkan backend, `OLLAMA_NUM_GPU=999`,
   or accept CPU and downsize. NPU on the 350 is not yet supported by Ollama
   (as of 2026-04).
2. **Output quality below threshold for 7B** — even within 8192 ctx, Qwen2.5
   7B drifts:
   - **Scarce summaries** when tools return rich JSON (e.g. `sleeve_summary`
     returns full per-size needed/owned/to_buy + `games`, the model collapses
     it to one sentence missing detail).
   - **Sporadic tool-call-as-text regression** even without overflow
     (`["$sleeve_summary", {}]` printed in chat).
   - Two paths to try in order: (a) **few-shot examples in slim prompt** —
     show the model a "good answer" template that walks through every size,
     cheap and might be enough; (b) **upgrade to `qwen2.5:14b-instruct`**
     via a parallel Modelfile (~9 GB Q4, fits 32 GB RAM with margin). Quality
     jump from 7B → 14B is significant for tool-use; speed loss is moderate
     because we're memory-bandwidth-bound.

### Decision: keep both providers usable
- AnthropicProvider remains the production default — Sonnet is still the
  best UX for this app right now. OllamaProvider is a working alternative
  for cost-conscious or privacy-conscious sessions.
- This means **no Anthropic-specific code at module level** anymore — anything
  vendor-specific (allowed_domains, cache_control, citations post-processing)
  lives inside the provider class. If we add Groq / OpenRouter / Together
  later, it's a single new subclass.

---

## 2026-04-28 — Audit log + add_to_inventory tool

### What's new
- Table `changes(id, ts, table_name, row_id, row_label, action, field, old_value,
  new_value, source)` created via `app/schema.py` migrate (idempotent).
- Helper module `app/audit.py` (`log_change` / `log_diff` / `log_full` / `recent`).
  All helpers run inside the caller's existing connection so audit rows share
  the transaction with the mutation — failed UPDATE rolls back its log row too.
- Write tools (`add_game`, `update_game`, `delete_game`, `set_sleeve_requirements`,
  `update_inventory`) now accept a kwarg `_source: str | None`. The kwarg is
  **not** declared in the JSON schema seen by Claude — `app/chat.py` injects it
  automatically using `inspect.signature` (cached per tool) so the model can't
  spoof it.
- `etl/backfill_v2.py` passes `_source="backfill_v2"` for its updates.
- New write tool `add_to_inventory(width_mm, height_mm, delta, brand?, note?)`:
  delta-based, server-side arithmetic, refuses negative results. Use this for
  "ho comprato N buste" — `update_inventory` (absolute count) stays available
  but the prompt should prefer the delta variant to avoid model arithmetic
  errors.
- New read tool `recent_changes(limit, table?, game_name?)`: lets the model
  answer "quando ho aggiunto X?" / "cosa è cambiato di Y?" from the audit log.

### Source convention
- `chat:{conversation_id}` for chat-driven writes
- `chat:?` if conversation_id wasn't passed (legacy callers)
- `backfill_v2` for the BGG-API backfill
- `etl` for `import_excel.py` (not yet wired — direct SQL writes there don't
  hit the audit log; deliberate, since ETL is a destructive bulk reset)
- `manual` / `unknown` for ad-hoc scripts

### Fields excluded from diffs
`updated_at` and `created_at` are filtered out in `audit._IGNORED_FIELDS` —
otherwise every UPDATE would log a noisy timestamp diff.

### Things left for later
- ETL → audit wiring (low priority; ETL is destructive by design).
- A simple "/changes" page in the UI to browse history without going through
  the chat. SQL ad-hoc query works for now.

---

## 2026-04-28 — Backfill v1 post-mortem & switch to BGG XML API2

### What happened with the Haiku + web_search backfill
- **Cost: ~€3 / ~80 tool rounds** for 56 games. Result: 20/56 still missing
  `bgg_id` (16 of those marked "ambiguous BGG match — manual review"), 27/56
  with `bgg_id` but missing `complexity_weight`/`bgg_rating`/categories/mechanics.
- Two structural failures:
  1. **BGG pages are JS-rendered** — weight/rating/category widgets are loaded
     client-side via the internal API, so `web_search_20250305` reads the
     static HTML and gets nothing for those fields. We paid for a search that
     literally cannot return what we need.
  2. **No a-priori disambiguation** — when BGG has multiple editions
     (HeroQuest 1989/1990/2021, base vs expansion for Sagrada/Splendor/Catan,
     etc.), Haiku correctly bailed with "manual review" — but each bail still
     cost ~$0.03. Should have given the model a candidate list up front.

### BGG XML API status (re-tested 2026-04-28)
- **Both v1 (`xmlapi/...`) and v2 (`xmlapi2/...`) are now Bearer-gated** by
  Cloudflare. All anonymous requests → HTTP 401, varying User-Agent doesn't
  bypass it. Confirmed the policy at `boardgamegeek.com/using_the_xml_api`:
  > "Registration and authorization is required for use of the XML API."
- Path forward: register an app, get a bearer token, put it in `.env` as
  `BGG_API_TOKEN`. With token, deterministic backfill is free + fast.
- The earlier note ("BGG XML API is paywalled (401)") referred to the same
  Cloudflare gate — it applies to xmlapi2 too, not just v1.

### New backfill architecture (in `etl/bgg_api.py` + `etl/backfill_v2.py`)
- **No LLM in the loop** for backfill. Pure XML parsing.
- Phase 1: for every game with known `bgg_id`, GET `thing?id=X&stats=1` and
  patch only fields that are currently NULL (preserve manual edits).
- Phase 2: for games without `bgg_id`, GET `search?query=NAME` → list of
  candidates with id+year+type. Human picks via `apply --gid N --bgg X`
  (or `--auto` for single-result/non-expansion hits).
- On-disk cache at `etl/.bgg_cache/` keeps dev cheap; rate-limited at ~0.6s
  between requests (BGG asks ≤2 req/s).
- The complexity_weight → label mapping is now a function (`_label_from_weight`)
  instead of being inlined in a system prompt.

### DB cleanup done today
- Cleared `notes` for the 30 games that still had stale `backfill: ...`
  messages from the v1 run. Two legitimate descriptive notes (7 Wonders Babel,
  Mysterium Refresh) preserved.
- The candidate BGG ids that were mentioned in those notes (e.g. "111661 vs
  316378 for 7 Wonders Cities") will be re-discovered deterministically by
  `xmlapi2/search` in Phase 2, so wiping was safe.

### Convention: don't reuse `web_search_20250305` for structured DB enrichment
- It's good for: looking up a sleeve size on sleevegeeks, fetching a quick fact
  for a chat answer, finding the publisher of a niche game.
- It's bad for: anything that needs structured fields from a JS-rendered
  page (BGG, Asmodee shop, BGG GeekLists, dragonshield product specs).
- For structured BGG data, always go through `etl/bgg_api.py`.

---

## 2026-04-27 — Initial build session

### User preferences
- **Replies in Italian** when the user writes in Italian; conversational tone, no excessive hedging.
- **Concise prose by default**; tables only when comparing multi-attribute items; emojis sparingly when they aid scanning (✅/❌/🎲/📕).
- **NO "Fonti:" sections** — Sonnet has a strong bias toward writing them after web_search; the post-processor mangles them. Use inline `[label](url)` links and a single `[↗](url)` suffix on cited sentences. Reinforce via system prompt; model still drifts occasionally.
- **Cheapest path preferred** — user accepts local infra (sentence-transformers ~280MB) over paid alternatives (Voyage/OpenAI embeddings).
- **Same key for all models**: user has a single `ANTHROPIC_API_KEY` from console.anthropic.com (separate from claude.ai Pro). API billing is pay-per-token.
- **No emoji avalanches** — single emoji per line max, never decorative.

### Architectural decisions
- **BGG XML API is paywalled (401)** as of 2026-04-27 (Cloudflare). Don't try to integrate — use Anthropic `web_search_20250305` server-tool with a trusted-domain allowlist (`app/chat.py:ALLOWED_DOMAINS`). web_search reads full pages, not just snippets.
- **Star schema** chosen over flat `games` table after user explicitly asked for "data engineer" view: `games` (dim) + outrigger dims (designers/publishers/categories/mechanics) via bridge tables; sleeve_requirements/inventory are facts. v1 → v2 auto-migration in `app/schema.py`.
- **Local embeddings** for rulebook RAG: `intfloat/multilingual-e5-base`, brute-force cosine over float32 BLOBs (no `sqlite-vec` — overkill at our scale).
- **Sonnet 4.6 for chat, Haiku 4.5 for batch backfill** — Haiku is ~3× cheaper and fine for structured "extract from BGG" tasks.
- **Server-side conversation persistence**: `conversations(history_json)` table; browser keeps only `conversation_id` in localStorage. Cross-device-ready.

### Gotchas
- **Windows console encoding** is cp1252 by default; any script printing Unicode (→, ✓, etc.) must `sys.stdout.reconfigure(encoding="utf-8")` early or set `PYTHONIOENCODING=utf-8`. See `etl/backfill_bgg.py`.
- **ETL is destructive** on `games`, `sleeve_requirements`, and bridges — re-running `etl/import_excel.py` wipes any game added via chat (e.g. Concordia). Inventory is also wiped. Conversations and dim tables (designers/publishers/...) survive.
- **pypdf page count > visible pages** — pypdf includes blank/cover pages. The Dune Imperium PDF reports 17 visible but 20 to pypdf. Cosmetic only.
- **Server runs as Claude Code background task** on port 8765. Killing the Claude Code session kills the server. For persistent operation, user runs `uv run uvicorn app.main:app --port 8765` from their own terminal.
- **`marked.js` from CDN** is used for Markdown rendering in chat bubbles. If we ever go offline-first, vendor it locally.
- **Web search citation blocks** arrive as separate `text` blocks with a `citations` field. We append `[↗](url)` from the first citation; multi-citation handling is naive.

### Tool catalog (as of this session)
13 tools live in `app/tools.py`:
- Read: `list_games`, `get_game`, `sleeve_summary`, `list_inventory`, `list_dimension`, `list_rulebooks`
- Write: `add_game`, `update_game`, `delete_game`, `set_sleeve_requirements`, `update_inventory`, `ingest_rulebook`
- RAG: `ask_rules` (returns top-k chunks; the calling Claude synthesizes the answer)
Plus Anthropic server tools: `web_search_20250305` (allowlisted).

### Cost benchmarks (Apr 2026 pricing)
- Backfill 1 game with Haiku 4.5 + 1 web_search: ~14s, ~18k tok in / 1k tok out, ≈ $0.033.
- Rulebook ingest (17-page PDF): ~3s on cached embedding model.
- Rulebook query: ~50ms search + 1 Sonnet call (~$0.005).
- Embedding model first download: ~1GB to `~/.cache/huggingface/`, ~30s on first run.

### Things to fix next time you touch the area
- Rulebook chunker is line-based; works for prose, breaks tabular content (HeroQuest tables, Twilight Imperium reference cards). Switch to layout-aware chunking when needed.
- Sleeve sizes `63×88` vs `63.5×88` are stored as separate rows; some old Excel data has the imprecise 63 form. Could normalize but rare.
- 22 games imported as `sleeve_status='sleeved'` have no per-size breakdown — backfill from BGG won't fix this; needs measuring physical cards.
