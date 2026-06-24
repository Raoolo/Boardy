# Brief per Codex — adversarial check su un code-review di Boardy (2026-06-24)

Claude Code ha fatto un pass generico sul codice di Boardy. Ti chiediamo un
**secondo parere avversariale**: non ripetere ciò che è già scritto, ma dire
dove sbaglia, cosa ha sopravvalutato/sottovalutato, e cosa ha *mancato*.

## Contesto del progetto
- Chatbot personale single-user per l'inventario di giochi da tavolo. FastAPI +
  SQLite (star schema) + RAG sui regolamenti (embeddings e5 locali, cosine
  brute-force). Provider LLM pluggable (DeepSeek default in prod, Anthropic,
  Ollama archiviato). Deploy Docker su `surfacesrvr` (prod = working tree).
- Auth: owner (cookie firmato) = full; guest = read-only. I write tool sono
  filtrati dal registry per i guest + guardia hard a runtime nel dispatch.
- Leggi `CLAUDE.md` (mappa del codice), `docs/LEARNINGS.md`, `docs/TODO.md`.

## Findings di Claude (da contestare/confermare)

1. **`app/tools.py` è un monolite (2594 righe)** — propone split per dominio
   (`tools/games.py`, `sleeves.py`, `rulebooks.py`, `bgg.py`, `wishlist.py` +
   `__init__.py` che assembla `TOOLS`/`TOOL_FUNCS`/`WRITE_TOOLS`). Rischio: che
   lo split rompa l'iniezione `_source` (chat.py usa `inspect.signature`),
   l'allineamento `WRITE_TOOLS`, o gli hook post-write (`_backfill_*`).
   → È davvero a basso rischio? Quali insidie concrete in questo specifico
   codice? C'è uno split migliore (es. separare gli SCHEMI JSON dai func)?

2. **Nessun test.** Propone `tests/test_invariants.py` con poche asserzioni:
   nessun write tool eseguibile da guest, `WRITE_TOOLS` ⊆ `TOOL_FUNCS` e
   coerente con i tool che dichiarano `_source`, invariante `sleeve_requirements`
   vuoto sui giochi `sleeved`/`na`, `_source` mai presente negli schemi JSON.
   → Quali altre invarianti del progetto meritano un test di regressione?
   Quali sono i buchi più probabili che un test del genere NON coprirebbe?

3. **`rulebooks._model_lazy()` non è thread-safe** (global lazy senza lock,
   endpoint sync nel threadpool → possibile doppio load del modello e5 280MB).
   Propone un `threading.Lock`. → È un problema reale dato il pattern di uvicorn
   qui, o è teorico al punto da non valerne la pena? Ci sono altri global
   mutabili con lo stesso problema (`_model`, cache, connessioni)?

4. **Import duplicati/sparsi in `main.py`** (doppio `from pathlib import Path`,
   `import re` mal posizionato, import leggeri inline in `library_filter`/
   `library_data`). Cosmetico. → Quali import inline sono invece *voluti* (lazy
   per non pagare import pesanti al boot: `fitz`, `google.genai`, `openai`) e
   NON vanno hoistati? Distinguere.

5. **System prompt ~300 righe inline in `chat.py`** — propone di estrarlo in
   file esterni. → Vale la complessità o è meglio lasciarlo inline e versionato?

## Cosa vogliamo da te, Codex
- Per ciascun finding: **confermi / ridimensioni / smonti**, con motivo.
- **Cosa ha MANCATO Claude?** Cerca attivamente: race condition, gestione errori
  che perde contesto, leak di dati tra owner/guest, posti dove un'eccezione
  rompe una transazione a metà, performance reali, dipendenze rischiose, punti
  dove il comportamento diverge tra i 3 provider LLM.
- Priorità finale tua (non la nostra): cosa faresti **per primo** e cosa
  lasceresti perdere.

Sii conciso e concreto, cita `file:riga`. Niente refactor: solo il parere.
