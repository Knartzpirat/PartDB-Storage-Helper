# Part-DB Labeltool

Tool für Lagerboxen in Part-DB und Labels für den Brother PT-P710BT.
Ersetzt die beiden Google Sheets und Apps Scripts.

Zwei Betriebsarten:

- **Lokal auf dem Windows-PC** (`start.bat`, `druck.modus: lokal`): Server, b-PAC und P-touch Editor im selben Prozess.
  ```
  Part-DB (Unraid, LAN)  <── REST-API ──>  Labeltool (PC, Browser-UI)  ──CSV──>  P-touch Editor  ──>  PT-P710BT
  ```
- **Server im Docker (Unraid) + Druck-Helfer am PC** (`druck.modus: helfer`): die Oberfläche ist von jedem Gerät im LAN
  erreichbar, gedruckt wird über den Helfer auf dem PC, an dem der Drucker hängt. Siehe
  [Betrieb im Docker (Unraid)](#betrieb-im-docker-unraid) und [Druck-Helfer unter Windows](#druck-helfer-unter-windows).
  ```
  Browser ──HTTP──> Labeltool (Docker, Unraid) ──REST──> Part-DB
                         ▲ WebSocket (vom PC aufgebaut, Token)
                         │
          Druck-Helfer (Windows-PC, b-PAC, P-touch Editor) ──USB──> PT-P710BT
  ```

## Einrichtung

1. **API in Part-DB freischalten:** Benutzer/Gruppe → Berechtigungen → *Verschiedenes → API* (Zugriff + Tokens erstellen).
   Dann in den Benutzereinstellungen ein API-Token mit Schreibrechten erzeugen.
2. **Python 3.10+** installieren (python.org, Haken bei „Add to PATH“).
3. `start.bat` doppelklicken. Beim ersten Start wird `config.yaml` angelegt und im Editor geöffnet:
   `partdb.url`, `partdb.token` und `output.ordner` eintragen. Danach `start.bat` erneut starten.
   Der Browser öffnet sich auf `http://127.0.0.1:8765`.
4. **P-touch-Vorlagen:** `labels.vorlagen_ordner` in `config.yaml` zeigt auf den Ordner mit deinen `.lbx`-Dateien
   (derzeit `BeispielEtiketten`). Das Box-Label steht unter `labels.box_vorlage`, die Bauteil-Vorlagen
   werden je Lagerart im Reiter *Stammdaten* gewählt. Von Hand verknüpfen musst du nichts: Das Tool legt beim Drucken
   eine Kopie der Vorlage unter `<Ausgabeordner>/Vorlagen/` an, die schon auf die richtige CSV zeigt, und öffnet sie
   im P-touch Editor. Layout-Änderungen machst du immer in der Originalvorlage, denn die Kopie wird jedes Mal neu erzeugt.

## Ablauf

| Label | Band | Vorlage | CSV |
|---|---|---|---|
| Box-Label (Name, Inhalt, Farbe, Abteile, Lagerart, 2× DataMatrix) | 24 mm | `Layout_Lagerbox_PartDB.lbx` | `Namenslabels.csv` |
| Bauteil-Label (ein Feld je Abteil) | 12 mm | je Lagerart, z. B. `PartDB_Window-Div-Bin-2.lbx` | `Inhalt_<Lagerart>.csv` |

**Neue Box → Box-Label:** Inhalt und Lagerart wählen (die Abteile kommen aus der Lagerart), Anzahl angeben, „In Part-DB
anlegen“. Das wiederholst du für alle Boxen, die vor dir liegen. Darunter stehen dann alle neuen Boxen ohne Label.
**Box-Labels drucken** schreibt die CSV und öffnet die 24-mm-Vorlage im P-touch Editor. Dort alle Datensätze drucken,
die Labels aufkleben und im Tool „Gedruckt ✓“ klicken. Ab jetzt kannst du in Part-DB Bauteile den Boxen zuweisen.
Unter „Alle Boxen“ lässt sich jedes Box-Label nachdrucken.

**Bauteil-Labels 12 mm:** Bauteile in Part-DB anlegen, Lagerort setzen, Tag `noLabel` vergeben.
Das Tool sammelt alle Boxen mit mindestens einem getaggten Teil und baut das Label aus *allen* Teilen der Box.
„CSVs schreiben“ → je Lagerart „In P-touch öffnen“ → drucken → „Gedruckt – Tags entfernen“. Entfernt wird der Tag
nur bei den Teilen, die im Export waren. Später getaggte Teile bleiben in der Liste.

## Stammdaten (Inhalte und Lagerarten)

Im Reiter **Stammdaten** lassen sich Boxfarben, Inhalte (Kürzel, Name, Boxfarbe) und Lagerarten
(Abteile, P-touch-Vorlage, CSV-Datei) anlegen und ändern.

- **Boxfarben** sind eine feste Palette (Name → Anzeigefarbe). Inhalte wählen ihre Farbe nur daraus,
  deshalb sieht z. B. „Hellblau“ überall gleich aus. Umbenennen ändert die Farbe bei allen Inhalten mit.
  Eine Farbe, die noch ein Inhalt nutzt, lässt sich nicht löschen. Bestehende Boxen in Part-DB werden der Palette
  unabhängig von Groß-/Kleinschreibung und Leerzeichen zugeordnet („Hell Blau“ = „Hellblau“).
- **Abteile** einer Lagerart werden bei „Neue Box“ automatisch eingetragen. Ein abweichender Wert bleibt möglich.
  Die Zahl bestimmt auch, wie viele Bauteile auf das 12-mm-Label kommen (Section1…n). Die CSV hat trotzdem immer
  mindestens 6 Section-/Footprint-Spalten, weil deine Vorlagen diese Felder kennen.
- **Löschen** fragt zweimal nach (in der Zeile, dann als Dialog) und warnt, wenn Boxen in Part-DB den Eintrag noch nutzen.
- **Rückgängig** macht die letzte Änderung rückgängig (Anlegen, Ändern oder Löschen). Die letzten 50 Schritte
  bleiben auch nach einem Neustart erhalten (`stammdaten.verlauf.json`).
- Gespeichert wird in `stammdaten.yaml`. Die Einträge in `config.yaml` sind nur Startwerte für den allerersten Start.
- Umbenennen eines Kürzels oder einer Lagerart ändert keine bestehenden Boxen in Part-DB.

## Vorlagen (Reiter „Vorlagen“)

- **Übersicht** aller `.lbx` im Vorlagenordner: Band × Länge, genutzte Felder, welche Lagerart sie nutzt, Warnungen
  (z. B. Vorlage hat weniger Section-Felder als die Lagerart Abteile, Spalten, die das Tool nicht liefert).
- **Vorschau mit Beispieldaten:** „Worst Case“ (lange Werte), „Text aus der Vorlage“ oder eine echte Box. Die Werte
  lassen sich direkt bearbeiten, die Vorschau folgt live. Mit b-PAC ist sie **exakt** (wie gedruckt), ohne b-PAC eine **Skizze**.
- **Im P-touch Editor bearbeiten:** verknüpft die Originalvorlage mit einer Beispiel-CSV (`<Ausgabe>/Beispiele/`) und
  öffnet sie. Beim ersten Mal wird ein Backup unter `<Vorlagenordner>/.backup/` angelegt. Ändern, speichern, im Tool
  „Vorschau aktualisieren“.
- **Kopie als neue Vorlage:** z. B. aus `PartDB_Window-Div-Bin-5.lbx` eine `…-6.lbx` für WinBin6 machen. Die Lagerart wird
  gleich zugewiesen (rückgängig machbar), im Editor fehlende `Section…`-Felder ergänzen und verknüpfen.
- Vorschau-Knöpfe gibt es auch bei den Box-Labels und in jeder Karte unter „Bauteil-Labels 12 mm“.

## Fächer

Damit ein Teil im richtigen Feld landet, in der **Beschreibung des Part-Lots** `Fach 3` oder `F3` eintragen.
Teile ohne Fach füllen die freien Felder alphabetisch auf. Die UI warnt bei zu vielen Teilen oder doppelt belegten Fächern.

## Datenformat in Part-DB

Neue Lagerorte bekommen den Kommentar im bisherigen Format, ergänzt um die Lagerart:

```
Lagerinhalt: Microcontroller Lagerfarbe: Dunkelgrau Lagerabteile: 2 Lagerart: WinBin2
```

Bestehende Boxen werden ebenfalls gelesen. Dort kommt die Lagerart aus dem Parameter
(Parametername = Boxtyp), so wie es dein altes Script angelegt hat.

## Konfiguration

`config.yaml` ersetzt die Sheets `Lagerinhalt` (→ `kategorien`) und `Lagerart` (→ `boxtypen`).
Den Lagerzähler gibt es nicht mehr. Die nächste Nummer ermittelt das Tool aus den vorhandenen Namen in Part-DB.

- `standard_felder`: Felder für Boxen, deren Lagerart nicht in den Stammdaten steht.
- `kategorie_quelle`: `part` = häufigste Bauteil-Kategorie der Box (wie bisher), `box` = Lagerinhalt.
- `encoding`: Wenn Umlaute (Grün, Weiß) im P-touch Editor falsch erscheinen, `cp1252` eintragen.

## Direktdruck (optional, b-PAC)

Statt über den P-touch Editor kann das Tool direkt auf den PT-P710BT drucken. Dann gibt es zusätzlich
„Direkt drucken“-Knöpfe, und der CSV-Weg bleibt als Ausweichweg erhalten.

1. **Brother b-PAC Client Component** installieren:
   <https://support.brother.com/g/s/es/dev/en/bpac/download/index.html>
   → Abschnitt **„b-PAC Client Component Only“** → neueste Version, **64-bit** (`.msi`).
   Die Bitness muss zu Python passen (hier 64-bit). Die 32-bit-Version funktioniert mit dem Tool nicht.
   Den SDK-Download brauchst du nicht. Der PT-P710BT wird ab b-PAC 3.2 unterstützt
   ([Modellliste](https://support.brother.com/g/s/es/dev/en/bpac/environment/index.html)).
2. Den PT-P710BT per USB anschließen. Er muss in Windows unter *Drucker & Scanner* auftauchen (Treiber von Brother).
3. In `config.yaml` unter `druck:` einstellen:
   - `direktdruck: true`
   - `drucker:` leer lassen (Drucker aus der Vorlage) oder exakt den Windows-Namen eintragen
   - `schnitt:` z. B. `[autocut, cutatend]`

Die UI zeigt neben den Knöpfen, ob der Direktdruck bereit ist oder was fehlt.

Gedruckt wird mit deinen Originalvorlagen, sie werden dabei nicht gespeichert. Das Tool liest aus der Vorlage,
welches Objekt welche CSV-Spalte zeigt, und setzt diese Texte für jedes Label. Bauteil-Labels von Boxen, deren
Lagerart keine Vorlage hat, werden übersprungen und gemeldet. Tags werden erst mit „Gedruckt – Tags entfernen“ entfernt.

## Betrieb im Docker (Unraid)

Der Server läuft als Container, der Drucker bleibt am Windows-PC und wird über den
[Druck-Helfer](#druck-helfer-unter-windows) angesprochen. Ohne verbundenen Helfer funktioniert alles außer Direktdruck,
exakter Vorschau (dann SVG-Skizze) und „In P-touch öffnen“.

> **Sicherheit:** Die Weboberfläche hat **keine Anmeldung**. Jeder, der den Port erreicht, kann Boxen in Part-DB anlegen
> und Einstellungen ändern. Nur im LAN betreiben, den Port **nicht** ins Internet freigeben (kein Port-Forwarding,
> Reverse-Proxy nur mit eigener Authentifizierung). Part-DB-Token und Helfer-Token liegen im Klartext in
> `/config/einstellungen.yaml` (Unraid: `/mnt/user/appdata/labeltool/config/einstellungen.yaml`).

### Image bauen

Es gibt kein fertiges Image in einer Registry, es wird auf dem Unraid-Server aus diesem Ordner gebaut.

1. Projektordner auf den Server kopieren, z. B. nach `/mnt/user/appdata/labeltool/src`
   (über die Freigabe `\\UnraidHome\appdata\labeltool\src`). `.venv`, `config.yaml` usw. braucht es nicht,
   `.dockerignore` lässt sie ohnehin weg.
2. **Variante A – Compose (Plugin „Docker Compose Manager“):** neuen Stack anlegen, als Compose-Datei
   `docker-compose.yml` aus dem Projekt nehmen (bzw. Stack-Ordner auf `/mnt/user/appdata/labeltool/src` legen) und
   „Compose Up“. Im Terminal geht dasselbe mit
   ```
   cd /mnt/user/appdata/labeltool/src
   docker compose up -d --build
   ```
3. **Variante B – Unraid-Template:** erst das Image bauen
   ```
   docker build -t labeltool:latest /mnt/user/appdata/labeltool/src
   ```
   dann `docker/labeltool.xml` nach `/boot/config/plugins/dockerMan/templates-user/my-labeltool.xml` kopieren und unter
   *Docker → Add Container → Template „labeltool“* anlegen. Nach einem Update des Tools den Build-Befehl wiederholen und
   den Container neu erstellen (beim Compose-Weg: `docker compose up -d --build`).

Das Image läuft als Benutzer `99:100` (Unraid `nobody:users`). Andere IDs: Build-Argumente `PUID`/`PGID`
(`docker build --build-arg PUID=1000 --build-arg PGID=1000 …`).

### Volumes, Port und Umgebungsvariablen

| Container | Unraid (Vorschlag) | Inhalt |
|---|---|---|
| `/config` | `/mnt/user/appdata/labeltool/config` | `config.yaml`, `einstellungen.yaml` (Tokens!), `stammdaten.yaml`, `stammdaten.verlauf.json`, `status.json` |
| `/ausgabe` | `/mnt/user/appdata/labeltool/ausgabe` | CSV-Dateien (`output.ordner`), Kopien verknüpfter Vorlagen |
| `/vorlagen` | `/mnt/user/appdata/labeltool/vorlagen` | P-touch-Vorlagen `.lbx` (`labels.vorlagen_ordner`) |

- Port **8765** (Weboberfläche und WebSocket für den Druck-Helfer).
- `PARTDB_URL`, `PARTDB_TOKEN`: optionale Startwerte. Was im Reiter *Einstellungen* gespeichert wird, hat Vorrang.
- `TZ`: Zeitzone, Standard `Europe/Berlin`.
- Intern gesetzt (normalerweise nicht ändern): `LABELTOOL_CONFIG=/config/config.yaml`, `LABELTOOL_HOST=0.0.0.0`,
  `LABELTOOL_PORT=8765`, `LABELTOOL_NO_BROWSER=1`.
- Healthcheck: ruft alle 30 s `/api/meta` auf (Status in der Docker-Übersicht).

### Erste Einrichtung

1. Container starten. Fehlt `/config/config.yaml`, wird sie aus `docker/config.yaml` angelegt
   (Ausgabe `/ausgabe`, Vorlagen `/vorlagen`, `druck.modus: helfer`, Kategorien und Lagerarten wie in
   `config.example.yaml`).
2. Vorlagen: Mit Druck-Helfer und Vorlagen-Ordner (empfohlen) kommen die `.lbx`-Dateien vom PC und werden
   automatisch nach `/vorlagen` abgeglichen. Ohne Helfer-Ordner die `.lbx`-Vorlagen (z. B. aus `BeispielEtiketten`)
   nach `\\UnraidHome\appdata\labeltool\vorlagen` kopieren.
3. `http://<unraid-ip>:8765` öffnen → Reiter **Einstellungen**: Part-DB-URL und Token eintragen, „Verbindung testen“,
   „Speichern“.
4. Unter *Druck-Helfer* „Helfer herunterladen (Windows)“ – am PC entpacken und `helfer.bat` starten
   (siehe nächster Abschnitt).
5. Weitere Optionen (Encoding, Fach-Regex, Box-Vorlage …) stehen wie gewohnt in `/config/config.yaml`; nach Änderungen
   den Container neu starten.

## Druck-Helfer unter Windows

Der Helfer läuft auf dem PC mit dem Etikettendrucker (z. B. PT-P710BT), baut von dort eine Verbindung zum Server auf
und übernimmt alles Windows-Spezifische: Direktdruck und exakte Vorschau über b-PAC, Öffnen von Vorlagen im
P-touch Editor, Ordner öffnen – und er hält den **Vorlagen-Ordner auf dem PC** mit dem Server abgeglichen.
Da die Verbindung **vom PC ausgeht**, ist keine Firewall-Freigabe auf dem PC nötig.

### Voraussetzungen am PC

- Python 3.11+ ([python.org](https://www.python.org/downloads/) oder Microsoft Store – beide bringen tkinter für den
  Assistenten mit; bei python.org die Option „tcl/tk and IDLE“ nicht abwählen)
- P-touch Editor und der Brother-Druckertreiber für dein Modell
- für Direktdruck/Vorschau die **b-PAC Client Component (64-bit)** (siehe [Direktdruck](#direktdruck-optional-b-pac))

### Installation (Download + Assistent)

1. Im Labeltool *Einstellungen → Druck-Helfer* → **„Helfer herunterladen (Windows)“**. Das ZIP
   (`Labeltool-Helfer.zip`) enthält nur, was der Helfer braucht (einige Module aus `labeltool/`,
   `requirements-helfer.txt`, `helfer.bat`, `helfer-autostart.bat`, `LIESMICH.txt`) und eine `helfer.vorgabe.yaml`
   mit der **Server-Adresse** (so, wie der Browser den Server erreicht, auch hinter einem Reverse-Proxy über
   `X-Forwarded-Proto/Host/Port/Prefix`) und dem **aktuellen Helfer-Token**. Gibt es noch keinen Token, wird er beim
   Download erzeugt und gespeichert. Das ZIP also nicht weitergeben.
2. ZIP in einen festen Ordner entpacken, z. B. `C:\Labeltool-Helfer`, und **`helfer.bat` doppelklicken**. Beim ersten
   Start wird eine Python-Umgebung (`.venv`) angelegt (1–2 Minuten), dann öffnet sich der **Einrichtungs-Assistent**:
   - **Server-Adresse** und **Token** sind vorbelegt („Verbindung testen“ prüft `GET /api/meta`)
   - **Vorlagen-Ordner**: Ordner mit deinen `.lbx`-Vorlagen auf diesem PC (Vorschlag `Dokumente\Labeltool-Vorlagen`,
     wird bei Bedarf angelegt; die Anzahl gefundener Vorlagen wird angezeigt). Dieser Ordner ist die **Quelle** der
     Vorlagen: neue und geänderte Dateien übernimmt der Server, auf dem PC gelöschte verschiebt er nach `.geloescht/`.
   - **Druckermodell**: Liste aller Modelle, die der installierte P-touch Editor kennt (126 bei Version 6.13; ohne
     P-touch Editor eine eingebaute Liste). Installierte Drucker stehen oben mit „(installiert)“ und sind vorausgewählt;
     der passende Windows-Druckername (z. B. „Brother PT-P710BT“) wird automatisch eingetragen. Ist das Modell nicht
     installiert, gibt es einen Hinweis mit Link zum Brother-Treiber.
   - **Autostart**: Verknüpfung „Labeltool Druck-Helfer“ im Autostart-Ordner, startet `helfer.bat` minimiert.
3. **„Speichern & starten“** schreibt `helfer.yaml`; danach läuft der Helfer im Konsolenfenster. Die Kopfzeile im
   Labeltool zeigt „Helfer“ und „Drucker“ grün, unter *Einstellungen → Druck-Helfer* stehen Rechner, Druckermodell,
   Vorlagen-Ordner, Anzahl Vorlagen und der letzte Abgleich; der Reiter *Vorlagen* zeigt die Quelle.

Gibt es kein tkinter (oder keine Anzeige), fragt der Assistent dieselben Werte in der Konsole ab
(erzwingen: `.venv\Scripts\python -m labeltool.helfer_setup --konsole helfer.yaml`).

### Später ändern

- **`helfer.bat einrichten`** öffnet den Assistenten erneut (den laufenden Helfer vorher schließen).
- **Neuer Helfer-Token** im Labeltool erzeugt? Den Helfer erneut herunterladen, in denselben Ordner entpacken und
  `helfer.bat einrichten` – der Assistent übernimmt Server/Token aus der neueren `helfer.vorgabe.yaml`, Ordner und
  Modell bleiben. Alternativ den Token von Hand in `helfer.yaml` eintragen (wirkt beim nächsten Verbindungsversuch).
- Autostart entfernen: Häkchen im Assistenten abwählen oder `helfer-autostart.bat entfernen`.
- Diagnose der Modellliste/Drucker: `.venv\Scripts\python -m labeltool.ptouch_modelle`.

### helfer.yaml

```yaml
server: "http://192.168.0.100:8765"
token: "…"
vorlagen_ordner: "C:\\Users\\Name\\Documents\\Labeltool-Vorlagen"   # Quelle der .lbx-Vorlagen
drucker_modell: "PT-P710BT"
drucker: "Brother PT-P710BT"    # leer = passend zum Modell automatisch suchen
autostart: true                 # nur Info für den Assistenten
```

Ohne `vorlagen_ordner` arbeitet der Helfer wie früher mit den Vorlagen auf dem Server. Der alte Abschnitt `pfade`
(Server-Pfad → Windows-Pfad) wird nicht mehr gebraucht, stört aber nicht; der Assistent übernimmt ihn unverändert.
Beispiel mit Kommentaren: `helfer.example.yaml`.

Der Helfer baut die Verbindung nach Abbrüchen (Server-Neustart, WLAN) selbst wieder auf. Es kann immer nur ein Helfer
verbunden sein, ein neu verbundener verdrängt den alten.

### Helfer aus dem Projektordner

Im kompletten Projektordner funktioniert `helfer.bat` genauso (nutzt dann `requirements.txt` und dieselbe `.venv` wie
`start.bat`).

### Server lokal ohne Docker mit Helfer

`start.bat` startet wie bisher alles lokal (`druck.modus: lokal`). Für Tests lässt sich der Server auch mit
Umgebungsvariablen starten: `LABELTOOL_CONFIG` (Pfad zur `config.yaml`, ein Argument hat Vorrang),
`LABELTOOL_HOST`, `LABELTOOL_PORT`, `LABELTOOL_NO_BROWSER=1` (kein Browser). Der Browser öffnet sich ohnehin nur,
wenn der Server an `127.0.0.1`/`localhost` lauscht.

## Tests

```
pip install -r requirements.txt pytest
python -m pytest tests
```

Die Tests laufen gegen ein simuliertes Part-DB und prüfen Anlegen, Namensvergabe, CSV-Spalten, Fachzuordnung und das Entfernen der Tags.
