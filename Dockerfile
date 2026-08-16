# Immagine base: Python 3.12, versione "slim" (Debian minimale, senza i
# pacchetti superflui dell'immagine Python completa) -- piu' piccola,
# meno superficie di attacco. Nota: e' indipendente dalla versione di
# Python installata sul tuo PC Windows -- dentro il container gira sempre
# questa, a prescindere da cosa hai in locale.
FROM python:3.12-slim

WORKDIR /app

# Copiamo SOLO requirements.txt prima del resto del codice, di proposito:
# Docker mette in cache ogni istruzione come "livello" separato. Finche'
# requirements.txt non cambia, questo passaggio (pip install) viene
# riusato dalla cache invece di essere rieseguito -- le ricostruzioni
# successive, dopo una modifica al solo codice Python, sono molto piu' veloci.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ora copiamo il codice dell'applicazione.
COPY app/ ./app/

# Utente non privilegiato: l'app non ha alcun bisogno di girare come
# root dentro il container. Se qualcosa andasse storto, limita il danno
# possibile a quell'utente, non all'intero sistema del container.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Niente --reload qui: quel flag e' solo per lo sviluppo locale.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]