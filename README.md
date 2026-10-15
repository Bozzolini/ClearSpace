# ClearSpace

ClearSpace è un programma per Windows 10 e Windows 11 che mostra cosa occupa spazio sul disco e, solo se lo confermi, sposta i file nel Cestino. Funziona solo sul tuo PC: non ha account, non si collega a internet e non invia file da nessuna parte.

## Cosa fa

Scegli uno o più dischi e una o più categorie. ClearSpace legge il disco una sola volta, classifica i file e ti mostra soltanto quelli che corrispondono alle categorie scelte. Un file normale non compare solo perché si trova in una cartella analizzata.

| Pagina | Cosa cerca |
|---|---|
| File grandi | File più grandi della soglia in MB. Le cartelle non compaiono. |
| Duplicati | Copie identiche. Prima confronta la dimensione, poi un campione, poi l'intero contenuto. Due file «simili» non sono duplicati. Di ogni gruppo resta almeno l'originale. |
| File temporanei | Solo file riconosciuti come temporanei o cache (`.tmp`, file `~$`, download incompleti, cache dei browser, log temporanei). |
| File vecchi | File la cui ultima modifica è più vecchia del numero di giorni indicato. Non usa la data di creazione né quella di ultimo accesso. |
| Cartelle vuote | Cartelle davvero vuote, anche di file nascosti. Una cartella illeggibile non viene segnata come vuota. |
| Download vecchi | Solo file dentro le cartelle Download, più vecchi della soglia in giorni. |

Ogni pagina ha il pulsante **Analizza**, che cerca solo quella categoria. La pagina **Analisi & Risultati** può cercarle tutte insieme. Un file che rientra in più categorie compare una volta sola, con tutte le categorie indicate.

## Come si usa

1. Apri ClearSpace. Nella **Dashboard** vedi lo spazio totale, usato e libero di ogni disco.
2. In **Analisi & Risultati** seleziona i dischi. Puoi aggiungere anche una singola cartella.
3. Avvia l'analisi completa, oppure entra in una categoria e premi **Analizza**.
4. Durante la scansione puoi mettere in **Pausa** o **Annullare**. Il programma resta utilizzabile.
5. Nella tabella cerca, ordina e seleziona i file. **Esplora file** apre la cartella in Esplora risorse. **Dettagli** mostra dimensione, data e categorie.
6. In **Pulizia** controlli l'elenco, la dimensione e la distinzione tra elementi sicuri (temporanei, cartelle vuote, copie duplicate) e candidati da verificare (file grandi, vecchi, download).
7. Confermi due volte. I file vanno nel **Cestino di Windows**, non vengono cancellati per sempre. Si possono ripristinare dal Cestino.

Nella pagina **Duplicati**, **Rimuovi tutti i duplicati** sposta nel Cestino le copie e tiene l'originale di ogni gruppo. Se l'originale non c'è più, quel gruppo non viene toccato.

In **Impostazioni** cambi lingua (italiano o inglese), tema chiaro o scuro, soglie, cartelle escluse e cache. **Cancella tutti i dati locali** elimina indice, cronologia e log di ClearSpace. Non tocca i tuoi file.

## Cosa non elimina

ClearSpace non tocca Windows, Programmi, il Cestino, le cartelle di sistema, le chiavi private, i database delle password, i portafogli di criptovalute e i file `Login Data` dei browser. Non segue collegamenti e junction, così un collegamento non può aggirare queste esclusioni. Prima di spostare un file ricontrolla che esista ancora e che non sia cambiato dopo l'analisi.

Lo spazio indicato è **potenzialmente** recuperabile: un file in uso viene saltato e segnalato.

## Installazione

### Da una release

Scarica lo zip e il file `SHA256SUMS` solo dalla pagina [Releases](https://github.com/Bozzolini/ClearSpace/releases) di questo repository. Poi, prima di aprire lo zip:

```powershell
powershell -File scripts\verify_release.ps1 -Zip .\ClearSpace-2.0.0-win64.zip -Sums .\SHA256SUMS
```

Se l'hash non coincide, cancella lo zip. Non eseguire un exe trovato su un altro sito e non disattivare SmartScreen. Windows può mostrare un avviso perché l'eseguibile non è firmato: l'hash serve a verificare che il file sia quello costruito da questo repository. I dettagli sono in [SECURITY.md](SECURITY.md).

### Dal codice sorgente

Serve Python 3.12 su Windows.

```bat
python -m pip install --require-hashes --no-deps -r requirements.lock
python main.py
```

`build_dist.bat` esegue i test e crea `dist\ClearSpace\ClearSpace.exe` insieme allo zip e a `SHA256SUMS`. Le dipendenze sono bloccate con hash: se un pacchetto non coincide, l'installazione si ferma.

## Privacy

Indice, cronologia e log restano in `%LOCALAPPDATA%\ClearSpace`, leggibili solo dal tuo account Windows. La cache memorizza percorsi, dimensioni e date, non il contenuto dei file. Non c'è telemetria e il programma non si aggiorna da solo.

## Per chi vuole i dettagli

L'analisi è una pipeline unica: scoperta dei file, raccolta dei metadati, classificazione, filtro, risultati. La pagina Risultati non mostra tutti i file letti, solo quelli validi. I duplicati sono confermati con un hash dell'intero file. La scansione gira fuori dall'interfaccia; pausa e annullamento restano attivi. I test automatici sono in `tests/` (`python -m pytest`).
