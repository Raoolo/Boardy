# Boardy — deploy spiegato in modalità caverna

Versione "mental model" del deploy, prima di toccare comandi. Quando torno dopo una pausa e mi sento perso, parto da qui, poi salto a `memo-deploy-howto.md` per i comandi esatti.

---

## L'analogia base: il ristorante

Boardy è un **ristorante in un locale affittato**.

- **L'image Docker** = il **kit pre-confezionato del ristorante**: cucina, forno, ricette stampate, ingredienti di base (Python, le librerie, il modello AI). Lo monti una volta, poi resta lì.
- **Il container in esecuzione** = il **ristorante aperto stasera**: lo staff è dentro, sta cucinando, prende ordini. Lo spegni → il locale chiude. Lo riaccendi → riapre identico.
- **I bind-mount (`app/`, `web/`, `etl/`)** = il **menu plastificato sul tavolo**: lo cambi quando vuoi, lo staff lo legge al momento, non serve rifare la cucina. Cambi una pagina HTML? È come correggere una voce sul menù.
- **Il volume Docker (`boardy.db`)** = la **cassaforte nel retrobottega**: registri contabili, prenotazioni, storico clienti. Se chiudi il ristorante e lo rifai da zero, la cassaforte resta lì col suo contenuto.
- **Il `.env`** = il **mazzo di chiavi del proprietario**: chiave della cassaforte, chiave del fornitore (Anthropic), chiave della cantina (BGG). Non le lasci mai dentro la cucina quando chiudi.

---

## Esempi di vita reale: cosa succede quando…

### Esempio 1 — Cambi una scritta sulla homepage
> "Voglio cambiare 'Cosa vuoi sapere?' in 'Chiedimi qualcosa!' su `/`"

Sul tuo PC apri `web/index.html`, modifichi la frase, salvi, `git push`.
Sul server amico: `git pull`. **Basta così.** Il browser dell'amico al refresh vede la modifica subito perché l'HTML è il menù plastificato — non serve riaprire il ristorante.

### Esempio 2 — Cambi un tool Python (es. `add_to_inventory`)
> "Ho aggiunto una validazione in `app/tools.py`"

Sul tuo PC modifichi, `git push`.
Sul server: `git pull && docker compose restart boardy`. **5 secondi.** Equivale a "lo staff esce un attimo, rilegge il manuale aggiornato, rientra". Il forno e la cucina restano accesi.

### Esempio 3 — Aggiungi una libreria nuova (es. `pillow` per immagini)
> "Modifico `pyproject.toml` per aggiungere una dipendenza"

Sul tuo PC `uv add pillow`, testi in locale, `git push`.
Sul server: `git pull && docker compose --profile tunnel up -d --build`. **~2 minuti.** Questo è "rifare la cucina perché serve un nuovo elettrodomestico". Più lento, ma raro.

### Esempio 4 — L'amico spegne e riaccende il server fisico
Il server riparte → Docker riparte → il container Boardy riparte automaticamente (`restart: unless-stopped`). Il DB nella cassaforte è intatto. Tutto torna online da solo, **niente da fare**.

### Esempio 5 — Vuoi vedere come sta il ristorante da lontano
```bash
docker compose logs -f boardy
```
È come avere una **telecamera in cucina**: vedi in diretta cosa sta cucinando lo staff, se è andato in errore, se sta ricevendo ordini.

### Esempio 6 — Vuoi entrare fisicamente nel locale per controllare
```bash
docker compose exec boardy bash
```
È **entrare nel ristorante con le chiavi** e camminare tra i tavoli. Puoi `ls /data`, controllare se il DB c'è, lanciare uno script Python a mano. Utile per debug, non per uso quotidiano.

---

## Le 3 sequenze che ricorderai a memoria

**A — "Ho cambiato un po' di codice":**
```bash
git pull && docker compose restart boardy
```

**B — "Ho aggiunto una libreria":**
```bash
git pull && docker compose --profile tunnel up -d --build
```

**C — "Qualcosa non va, fammi vedere":**
```bash
docker compose logs -f boardy
```

Il 90% delle volte userai **A**. Il 9% **B**. L'1% **C** quando qualcosa rompe.

---

## La paura più comune: "perdo il DB?"

**No.** Il DB sta nel volume `boardy_db`, separato dall'image. Tutti questi comandi sono **sicuri**:
- `docker compose restart boardy` ✅
- `docker compose down` ✅ (spegne ma lascia il volume)
- `docker compose up -d --build` ✅ (rifà l'image, volume intatto)
- `git pull` ✅ (cambia solo il codice)

L'**unico** comando che cancella il DB è:
- `docker compose down -v` ❌ (`-v` = "anche i volumi" = "svuota la cassaforte"). **Non usarlo mai** salvo che tu voglia ripartire da zero.

---

## Glossario rapido (per quando un termine non ti torna)

| Termine | Traduzione in caverna |
|---|---|
| **Image** | La scatola sigillata col kit del ristorante |
| **Container** | Il ristorante aperto, vivo, che gira |
| **Volume** | La cassaforte fissa al pavimento — non si sposta |
| **Bind-mount** | Una finestra dalla strada al locale: vedi e cambi da fuori |
| **`docker compose up`** | Apri il ristorante |
| **`docker compose down`** | Chiudi il ristorante (cassaforte resta) |
| **`docker compose restart`** | Lo staff esce e rientra (cucina sempre calda) |
| **`docker compose build`** | Rifai la scatola del kit (se hai cambiato ricette) |
| **`docker compose logs`** | Telecamera in cucina |
| **`docker compose exec ... bash`** | Entri con le chiavi |
| **Cloudflare Tunnel** | Il fattorino che porta gli ordini fuori al cliente — senza esporre la porta del locale |

---

👉 **Per i comandi esatti** (con flags, sintassi, troubleshooting per sintomo): salta a `memo-deploy-howto.md`.
👉 **Per il "perché" architetturale** di una scelta: `LEARNINGS.md`.
