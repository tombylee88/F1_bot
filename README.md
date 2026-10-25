# Bot Telegram F1 + MotoGP

Ti manda gli orari (ora italiana) dei weekend di F1 e MotoGP e risponde ai comandi del menu.

## Cosa fa
- **Ogni giovedì mattina**: se nel weekend c'è un GP, ti scrive il riepilogo con tutte le sessioni.
- **Comandi dal menu** (risposta entro ~5 minuti): `/prossima_sessione`, `/qualifiche`, `/gara`, `/weekend`, `/f1`, `/motogp`.
- Risponde solo a te (controllo sul tuo chat id).

## Configurazione (una volta sola)
1. Crea un repository **pubblico** su GitHub e carica questi file.
2. Repository → *Settings* → *Secrets and variables* → *Actions* → *New repository secret*:
   - `TELEGRAM_TOKEN` = token di BotFather
   - `TELEGRAM_CHAT_ID` = il tuo chat id (vedi punto 3)
3. Per sapere il chat id: *Actions* → **Rispondi ai comandi** → *Run workflow*, poi scrivi `/start` al bot. Ti risponde con il chat id da copiare nel secret.
4. *Actions* → **Riepilogo settimanale** → *Run workflow* con `force` per provare subito.

## Se qualcosa non va
Lancia **Riepilogo settimanale** con modo `debug` e guarda il log: mostra cosa restituiscono le fonti dati.

## Note
- Dati F1: API Jolpica (successore di Ergast). Dati MotoGP: API ufficiale MotoGP.
- Se per 60 giorni non c'è attività nel repository, GitHub sospende le automazioni: basta riattivarle dalla scheda *Actions*.
- Non pubblicare mai il token nel codice: va solo nei Secrets.
