# Part-DB Labeltool

Webanwendung, mit der du Lagerboxen in [Part-DB](https://github.com/Part-DB/Part-DB-server) anlegst und dafür Etiketten
auf einem Brother-P-touch-Drucker druckst – ein **Box-Label** (z. B. 24 mm) für jede Box und ein **Bauteil-Label** (z. B. 12 mm)
mit den Bauteilen, die darin liegen. Die Layouts sind normale P-touch-Editor-Vorlagen (`.lbx`), die du selbst gestaltest.

Entwickelt und getestet mit dem **Brother PT-P710BT**; andere P-touch-/QL-Modelle, die der P-touch Editor kennt,
sind auswählbar.

## Funktionen

- **Boxen anlegen:** Inhalt, Lagerart und Anzahl wählen – das Tool vergibt fortlaufende Namen (z. B. `MIC-2.0021`),
  legt die Lagerorte per API in Part-DB an und sagt dir, in welcher Farbe die Box gedruckt/gewählt werden soll.
- **Box-Labels:** Name, Inhalt, Farbe, Abteile, Lagerart und zwei DataMatrix-Codes (Part-DB-Barcode `S0123` und Name).
- **Bauteil-Labels:** Boxen, deren Bauteile in Part-DB einen Tag (Standard `noLabel`) tragen, bekommen ein Label mit
  *allen* Bauteilen der Box, auf Wunsch fachgenau. Nach dem Druck entfernt das Tool den Tag.
- **Drucken:** über CSV + P-touch Editor oder **direkt** über die Brother b-PAC-Schnittstelle.
- **Vorlagen-Werkzeuge:** Übersicht mit Prüfungen, **exakte Vorschau** mit Beispiel- oder Echtdaten, Vorlage im
  P-touch Editor bearbeiten, neue Vorlage aus Kopie.
- **Stammdaten:** Inhalte (Kürzel, Name, Boxfarbe), Lagerarten (Abteile, Vorlage) und Boxfarben-Palette – mit
  doppelter Nachfrage beim Löschen und Rückgängig.
- **Statusanzeige** für Part-DB, Druck-Helfer und Drucker; Part-DB-Adresse und API-Token in der Oberfläche änderbar.

## Aufbau

Druck und exakte Vorschau brauchen Windows (P-touch Editor, b-PAC). Deshalb gibt es zwei Betriebsarten:

**A – Server im Docker + Druck-Helfer am Windows-PC (empfohlen)**

```
Browser (beliebiges Gerät) ──HTTP──> Labeltool-Server (Docker) ──REST-API──> Part-DB
                                         ▲  WebSocket, vom PC aus aufgebaut
                                         │
          Druck-Helfer (Windows-PC: b-PAC, P-touch Editor) ──USB──> Etikettendrucker
```

Die Oberfläche ist im ganzen LAN erreichbar. Der Druck-Helfer läuft auf dem PC, an dem der Drucker hängt, verbindet
sich selbst zum Server (keine Firewall-Freigabe am PC nötig) und hält einen **Vorlagen-Ordner auf dem PC** mit dem
Server abgeglichen. Ohne verbundenen Helfer funktioniert alles außer Direktdruck, exakter Vorschau (dann eine Skizze)
und dem Öffnen im P-touch Editor.

**B – Alles lokal auf einem Windows-PC**

Server, b-PAC und P-touch Editor laufen in einem Prozess (`start.bat`). Kein Docker nötig.

## Voraussetzungen

- **Part-DB** mit aktivierter API und einem API-Token mit Schreibrechten:
  Benutzer/Gruppe → Berechtigungen → *Verschiedenes → API* (Zugriff und Tokens erstellen erlauben), dann in den
  Benutzereinstellungen unter *API-Token* ein Token anlegen.
- **Zum Drucken** ein Windows-PC mit
  - Brother-Druckertreiber und **P-touch Editor** (für eigene Vorlagen und den CSV-Druckweg),
  - optional der **b-PAC Client Component (64-bit)** für Direktdruck und exakte Vorschau – siehe
    [Direktdruck und exakte Vorschau](#direktdruck-und-exakte-vorschau),
  - Python 3.11 oder neuer ([python.org](https://www.python.org/downloads/) mit „Add to PATH“ oder Microsoft Store).
- **Für Betriebsart A** ein Rechner mit Docker (z. B. Unraid, Synology, Linux-Server).

## Installation

### Docker

Das Image wird von GitHub Actions gebaut und liegt in der GitHub Container Registry:

```
ghcr.io/knartzpirat/partdb-storage-helper:latest
```

**docker run:**

```bash
docker run -d --name labeltool \
  --restart unless-stopped \
  -p 8765:8765 \
  -e TZ=Europe/Berlin \
  -v labeltool_config:/config \
  -v labeltool_ausgabe:/ausgabe \
  -v labeltool_vorlagen:/vorlagen \
  ghcr.io/knartzpirat/partdb-storage-helper:latest
```

**docker compose** (`compose.yml`):

```yaml
services:
  labeltool:
    image: ghcr.io/knartzpirat/partdb-storage-helper:latest
    container_name: labeltool
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      TZ: Europe/Berlin
      # optional: Startwerte, später in der Oberfläche änderbar
      # PARTDB_URL: "http://partdb.lan:8080"
      # PARTDB_TOKEN: "tcp_…"
    volumes:
      - config:/config
      - ausgabe:/ausgabe
      - vorlagen:/vorlagen

volumes:
  config:
  ausgabe:
  vorlagen:
```

```bash
docker compose up -d
```

Danach `http://<server>:8765` öffnen und mit den [ersten Schritten](#erste-schritte) weitermachen.

**Eigene Ordner statt benannter Volumes:** Der Container läuft nicht als root, sondern standardmäßig als `99:100`.
Bei Bind-Mounts (`./config:/config` …) entweder die Ordner diesem Benutzer geben (`chown -R 99:100 ./config …`) oder
den Container mit dem eigenen Benutzer starten: `--user "$(id -u):$(id -g)"` bzw. in Compose `user: "1000:1000"`.

| Pfad im Container | Inhalt |
|---|---|
| `/config` | `config.yaml`, `einstellungen.yaml` (enthält die Tokens), `stammdaten.yaml`, Verlauf und Status |
| `/ausgabe` | erzeugte CSV-Dateien für den P-touch Editor |
| `/vorlagen` | P-touch-Vorlagen (`.lbx`) – mit Druck-Helfer automatisch vom PC abgeglichen |

| Umgebungsvariable | Bedeutung |
|---|---|
| `TZ` | Zeitzone (Standard `Europe/Berlin`) |
| `PARTDB_URL`, `PARTDB_TOKEN` | optionale Startwerte; was in der Oberfläche gespeichert wird, hat Vorrang |
| `LABELTOOL_PORT` | Port im Container (Standard `8765`) |

Port **8765** dient der Weboberfläche und der WebSocket-Verbindung des Druck-Helfers. Der Container hat einen
Healthcheck (`/api/meta`). Beim ersten Start legt er `/config/config.yaml` mit Standardwerten an.

### Unraid

**Mit Template:**

1. [`docker/labeltool.xml`](docker/labeltool.xml) nach
   `/boot/config/plugins/dockerMan/templates-user/my-labeltool.xml` kopieren.
2. *Docker → Add Container* → Template **labeltool** wählen, Pfade prüfen, *Apply*.

Vorgeschlagene Pfade: `/mnt/user/appdata/labeltool/{config,ausgabe,vorlagen}`. Unraid-Rechte (`nobody:users` =
`99:100`) passen ohne weitere Einstellungen.

**Mit dem Plugin „Docker Compose Manager“:** neuen Stack anlegen, Inhalt von
[`docker-compose.yml`](docker-compose.yml) einfügen, *Compose Up*.

**Update:** im Docker-Tab *Check for Updates* → *Update* (Compose: `docker compose pull && docker compose up -d`).

### Druck-Helfer unter Windows

Der Druck-Helfer wird mit einem Setup installiert – Python ist dafür nicht nötig. Voraussetzungen am PC: Windows 10/11
(64 bit), Brother P-touch Editor mit dem Druckertreiber und die
[b-PAC Client Component](https://support.brother.com/g/s/es/dev/en/bpac/download/index.html) (64 bit).

1. Labeltool im Browser **unter der Adresse öffnen, die der PC später verwenden soll** (z. B.
   `http://unraid:8765`, nicht `localhost`) → *Einstellungen → Druck-Helfer* → **„Installer herunterladen
   (empfohlen)“**. Das ZIP `Labeltool-Helfer-Installer.zip` enthält `Labeltool-Helfer-Setup.exe`,
   `helfer.vorgabe.yaml` (Server-Adresse und Helfer-Token – nicht weitergeben) und eine `LIESMICH.txt`.
   Das Setup gibt es außerdem bei jeder [Version](../../releases) als Release-Asset; dann Server und Token im
   Assistenten von Hand eintragen.
2. ZIP **entpacken** (Setup und `helfer.vorgabe.yaml` müssen im selben Ordner liegen) und
   `Labeltool-Helfer-Setup.exe` starten.
   - Das Setup ist nicht digital signiert. Windows SmartScreen meldet deshalb „Der Computer wurde durch Windows
     geschützt“: **„Weitere Informationen“ → „Trotzdem ausführen“**.
   - Die Benutzerkontensteuerung fragt nach Administratorrechten, weil nach `C:\Program Files\Labeltool-Helfer\`
     installiert wird.
3. Das Setup legt im Startmenü den Ordner „Labeltool Druck-Helfer“ mit **„Labeltool Druck-Helfer“**,
   **„Druck-Helfer einrichten“** und **„Deinstallieren“** an. Die Aufgabe **Autostart** (voreingestellt) startet den Helfer bei jeder Anmeldung des Benutzers unsichtbar im
   Hintergrund.
4. Am Ende öffnet sich der **Einrichtungs-Assistent**:
   - **Server und Token** sind vorbelegt; „Verbindung testen“ prüft beides.
   - **Vorlagen-Ordner:** Ordner mit deinen `.lbx`-Vorlagen auf diesem PC. Er ist die Quelle der Vorlagen (siehe
     [Vorlagen-Abgleich](#vorlagen-abgleich)). Ist er leer, holt der Helfer die Vorlagen vom Server.
   - **Druckermodell:** alle Modelle, die der installierte P-touch Editor kennt; installierte Drucker stehen oben und
     sind vorausgewählt. Fehlt der Treiber, gibt es einen Link zu Brother.
5. **„Speichern & starten“.** In der Kopfzeile des Labeltools werden „Helfer“ und „Drucker“ grün.

Einstellungen, Zwischenspeicher und Protokoll (`helfer.log`) liegen in **`%LOCALAPPDATA%\Labeltool-Helfer\`**.
Später ändern: Startmenü → „Druck-Helfer einrichten“. Nach einem neuen Helfer-Token den Installer erneut
herunterladen und installieren oder den Token im Assistenten eintragen – Ordner und Modell bleiben erhalten.

**Deinstallieren:** Windows-Einstellungen → *Apps* → *Installierte Apps* → „Labeltool Druck-Helfer“ (oder
Startmenü → „Deinstallieren“). Programm, Startmenü-Einträge und Autostart werden entfernt; den Ordner
`%LOCALAPPDATA%\Labeltool-Helfer\` danach bei Bedarf von Hand löschen.

Die Einstellungen stehen in `helfer.yaml` (Vorlage: [`helfer.example.yaml`](helfer.example.yaml)):

```yaml
server: "http://unraid:8765"
token: "…"
vorlagen_ordner: "C:\Users\Name\Documents\Labeltool-Vorlagen"
drucker_modell: "PT-P710BT"
drucker: "Brother PT-P710BT"    # leer = passend zum Modell suchen
autostart: true
```

Der Helfer baut die Verbindung nach Unterbrechungen selbst wieder auf. Es ist immer nur ein Helfer verbunden; ein neu
verbundener ersetzt den alten.

#### Helfer aktualisieren

Server-Image und Helfer-Setup haben dieselbe Version (aus dem Git-Tag `v1.2.3` → `1.2.3`; Builds ohne Tag heißen
`0.0.0-<commit>`). Ist auf dem Server ein neueres Setup als beim verbundenen Helfer, zeigt die Kopfzeile
**„Helfer: Update verfügbar“** (gelb), unter *Einstellungen → Druck-Helfer* steht „installiert X → verfügbar Y“.

1. **„Helfer aktualisieren“** klicken und bestätigen. Der Helfer lädt das Setup selbst vom Server (mit seinem
   Helfer-Token), prüft die Prüfsumme und startet es.
2. **Am Windows-PC** fragt die Benutzerkontensteuerung nach Administratorrechten – mit **„Ja“** bestätigen. Wer die
   Abfrage ablehnt, behält die alte Version; der Helfer läuft weiter.
3. Das Setup installiert ohne Rückfragen, übernimmt Einstellungen und Autostart und startet den Helfer neu. Die
   Oberfläche zeigt „Update läuft … Helfer verbindet sich neu“, bis sich der Helfer mit der neuen Version meldet
   (nach 5 Minuten ohne Neuverbindung erscheint ein Hinweis, was am PC zu prüfen ist).

Der Knopf funktioniert nur mit dem **installierten** Helfer. Die Python-Variante (ZIP, `helfer.bat`) wird nicht per
Knopf aktualisiert – dafür einmal den Installer verwenden. Von Hand geht es weiterhin: neues Setup herunterladen und
über die bestehende Installation installieren; die Einstellungen bleiben.

**Alternative ohne Installation (Python 3.11+ nötig):** *Einstellungen → Druck-Helfer → Alternative ohne
Installation* lädt `Labeltool-Helfer.zip`. In einen festen Ordner entpacken (z. B. `C:\Labeltool-Helfer`) und
`helfer.bat` doppelklicken – beim ersten Start wird eine Python-Umgebung eingerichtet (1–2 Minuten), danach öffnet
sich derselbe Assistent. Hier liegt `helfer.yaml` neben `helfer.bat`; ändern mit `helfer.bat einrichten`, Autostart
entfernen mit `helfer-autostart.bat entfernen`. Liegt auf dem Server kein Installer (z. B. selbst gebautes Image),
bietet die Oberfläche nur diesen Weg an.

### Lokal unter Windows (ohne Docker)

1. Projekt herunterladen (Code → *Download ZIP*) und entpacken.
2. **`start.bat`** doppelklicken. Beim ersten Start wird `config.yaml` aus `config.example.yaml` angelegt und im Editor
   geöffnet – Part-DB-Adresse und Token kannst du dort oder später in der Oberfläche eintragen.
3. `start.bat` erneut starten; die Python-Umgebung wird eingerichtet und der Browser öffnet `http://127.0.0.1:8765`.

In dieser Betriebsart (`druck.modus: lokal`) laufen Direktdruck, Vorschau und P-touch Editor ohne Helfer. Den
Vorlagenordner legt `labels.vorlagen_ordner` in `config.yaml` fest.

## Erste Schritte

1. **Einstellungen → Part-DB:** Adresse (ohne `/api`) und API-Token eintragen, „Verbindung testen“, „Speichern“.
   Die Kopfzeile zeigt danach *Part-DB: online* mit Version und Benutzer.
2. **Druck-Helfer einrichten** (Docker-Betrieb), siehe oben.
3. **Stammdaten prüfen:** Inhalte, Lagerarten und Boxfarben an die eigene Lagerhaltung anpassen
   (siehe [Stammdaten](#stammdaten)).
4. **Vorlagen zuordnen:** Im Reiter *Stammdaten* jeder Lagerart ihre Bauteil-Vorlage zuweisen; die Box-Label-Vorlage
   steht in `config.yaml` unter `labels.box_vorlage`. Beispiele liegen in [`BeispielEtiketten/`](BeispielEtiketten).

## Arbeitsablauf

| Label | Band (aus der Vorlage) | Vorlage | CSV |
|---|---|---|---|
| Box-Label | z. B. 24 mm | `labels.box_vorlage`, z. B. `Layout_Lagerbox_PartDB.lbx` | `Namenslabels.csv` |
| Bauteil-Label | z. B. 12 mm | je Lagerart, z. B. `PartDB_Window-Div-Bin-2.lbx` | `Inhalt_<Lagerart>.csv` |

**Neue Boxen:** Reiter *Neue Box* → Inhalt und Lagerart wählen (die Abteile kommen aus der Lagerart), Anzahl
angeben, *In Part-DB anlegen*. Alle neuen Boxen ohne Label stehen darunter; *Box-Labels drucken* (bzw. *Direkt
drucken*) erzeugt die Etiketten. Nach dem Aufkleben *Gedruckt ✓* klicken. Unter *Alle Boxen* lässt sich jedes Label
nachdrucken.

**Bauteil-Labels:** In Part-DB Bauteile anlegen, Lagerort setzen und den Tag `noLabel` vergeben. Der Reiter
*Bauteil-Labels* sammelt alle Boxen mit mindestens einem getaggten Teil und zeigt das Label mit allen Teilen der Box.
Drucken (direkt oder über *CSVs schreiben* → *In P-touch öffnen*), dann *Gedruckt – Tags entfernen*. Entfernt wird der
Tag nur bei den Teilen, die tatsächlich gedruckt wurden.

**CSV-Dateien:** Das Tool schreibt sie in den Ausgabeordner (Docker: `/ausgabe`) und lädt sie zusätzlich automatisch
im Browser herunter – eine Datei als CSV, mehrere zusammen als ZIP. Abschalten unter *Einstellungen → Druck*
(gilt pro Browser).

**Fächer:** Damit ein Teil in einem bestimmten Feld landet, in Part-DB beim Bauteil unter *Bearbeiten →
Lagerbestände* in der **Beschreibung des Bestands** dieser Box die Fachnummer eintragen: nur die Zahl (`3`) oder
`Fach 3` / `F3` (auch mit weiterem Text dahinter, z. B. `Fach 3 – oben`). Ersatzweise wird der Kommentar des
Bestands gelesen. Text wie `3 Rollen` oder `100 Stück` zählt nicht als Fach. Teile ohne Fach füllen die freien
Felder in der Reihenfolge, in der sie angelegt wurden.

**Inhalt eines Bauteil-Labels:** `Location` = Boxname, `Category` = Part-DB-Kategorien der Bauteile
(oder der Inhalt der Box, `kategorie_quelle: box`), `Section1…n` = Bauteilname, `Footprint1…n` = Footprint.
Passen nicht alle Bauteile in eigene Felder (mehr Bauteile als Abteile oder mehrere im selben Fach), werden sie
im Feld mit ` | ` getrennt zusammengefasst – es fällt keins weg. Gleiches gilt für Footprints und Kategorien
(z. B. `Wellenkupplung | GT-Riemen`). Das Trennzeichen legt `labels.trenner` fest.

## Stammdaten

Reiter **Stammdaten**:

- **Boxfarben:** feste Palette (Name → Anzeigefarbe), damit z. B. „Hellblau“ überall gleich aussieht. Umbenennen
  ändert die Farbe bei allen Inhalten; eine genutzte Farbe lässt sich nicht löschen. Farben bestehender Boxen werden
  unabhängig von Schreibweise zugeordnet („Hell Blau“ = „Hellblau“).
- **Inhalte:** Kürzel (Teil des Boxnamens), Name und Boxfarbe.
- **Lagerarten:** Anzahl Abteile (wird bei *Neue Box* vorbelegt und bestimmt die Felder auf dem Bauteil-Label),
  P-touch-Vorlage und optional der CSV-Dateiname.

Löschen fragt zweimal nach und warnt, wenn Boxen in Part-DB den Eintrag noch nutzen. *Rückgängig* nimmt die letzten
Änderungen zurück (bis zu 50 Schritte, auch nach einem Neustart). Gespeichert wird in `stammdaten.yaml`; die Einträge in
`config.yaml` sind nur Startwerte. Umbenennen ändert keine bestehenden Boxen in Part-DB.

## Vorlagen

### Eigene Vorlagen gestalten

Vorlagen sind P-touch-Editor-Dateien (`.lbx`), deren Textfelder und Barcodes per *Datenbank → Verbinden* mit
CSV-Spalten verknüpft sind. Das Tool befüllt diese Spalten:

| Vorlage | Spalten |
|---|---|
| Box-Label | `ID`, `Lagername`, `Barcodeinhalt`, `Lagerinhalt`, `Lagerabteile`, `Lagerfarbe`, `Lagerart`, `Lager-ID` |
| Bauteil-Label | `Location`, `Category`, `Section1…n`, `Footprint1…n`, `Barcode`, `Lagerfarbe`, `Lagerinhalt` |

Bauteil-CSVs haben immer mindestens sechs `Section`/`Footprint`-Spalten. Am einfachsten ist es, eine der Vorlagen aus
[`BeispielEtiketten/`](BeispielEtiketten) zu kopieren und anzupassen.

### Reiter „Vorlagen“

- **Übersicht** aller Vorlagen mit Band, Länge, genutzten Feldern, zugehöriger Lagerart und Warnungen (z. B. weniger
  Felder als Abteile, unbekannte Spalten).
- **Vorschau** mit Beispieldaten („Worst Case“ mit langen Werten, Text aus der Vorlage oder eine echte Box). Die Werte
  sind bearbeitbar, die Vorschau aktualisiert sich live – mit b-PAC exakt wie gedruckt, sonst als Skizze.
- **Im P-touch Editor bearbeiten:** öffnet die Vorlage mit Beispieldaten; beim ersten Mal wird ein Backup angelegt.
- **Kopie als neue Vorlage:** z. B. aus einer Vorlage für 5 Abteile eine für 6 machen und gleich einer Lagerart
  zuweisen.

### Vorlagen-Abgleich

Mit Druck-Helfer und Vorlagen-Ordner ist der **Ordner auf dem PC die Quelle**: Neue und geänderte Vorlagen übernimmt
der Server automatisch (etwa alle 10 Sekunden), im Labeltool angelegte Kopien landen im PC-Ordner. Auf dem PC
gelöschte Vorlagen verschiebt der Server nach `/vorlagen/.geloescht/`. Fehlen auf einmal viele Vorlagen (z. B.
falscher Ordner gewählt), wird nichts verschoben, bis du es im Reiter *Vorlagen* bestätigst.

## Direktdruck und exakte Vorschau

Beides läuft über die **Brother b-PAC Client Component**:

1. Download: <https://support.brother.com/g/s/es/dev/en/bpac/download/index.html> → Abschnitt
   **„b-PAC Client Component Only“** → neueste Version, **64-bit** (`.msi`). Das SDK wird nicht benötigt.
   Unterstützte Modelle: [Modellliste](https://support.brother.com/g/s/es/dev/en/bpac/environment/index.html).
2. Drucker per USB anschließen; er muss in Windows unter *Drucker & Scanner* erscheinen.
3. In der Oberfläche unter *Einstellungen → Druck* Direktdruck einschalten und Schnittoptionen wählen
   (z. B. `autocut`, beim PT-P710BT zusätzlich `chainprint`; nicht unterstützte Optionen lässt das Tool weg).

**Vor jedem Direktdruck** erscheint eine Bestätigung mit Anzahl der Labels, dem Band laut Vorlage und dem eingelegten
Band (das meldet der Drucker über b-PAC). Dort lässt sich die Zahl der **Kopien je Label** einstellen. Ohne Bestätigung
verschwindet sie nach 15 Sekunden, und es wird nichts gedruckt. Ist das **falsche Band** eingelegt, bleibt sie stehen,
bis der Rollenwechsel bestätigt ist; der Helfer prüft das Band unmittelbar vor dem Druck noch einmal und bricht bei
falschem Band ab, ohne etwas zu senden. Alle Labels eines Druckvorgangs – auch aus verschiedenen Vorlagen gleicher
Bandbreite – gehen als **ein Auftrag** an den Drucker (ein Vorlauf, Autocut dazwischen).

Gedruckt wird mit den Originalvorlagen, sie werden dabei nicht verändert. Ist der Drucker nicht verbunden, wird kein
Auftrag gesendet – so bleiben keine Aufträge in der Warteschlange hängen, die später unerwartet drucken.

## Konfiguration

Die meisten Einstellungen sind in der Oberfläche erreichbar und landen in `einstellungen.yaml`. Weitere Optionen in
`config.yaml` (Vorlage mit Kommentaren: [`config.example.yaml`](config.example.yaml), im Docker
[`docker/config.yaml`](docker/config.yaml)):

| Schlüssel | Bedeutung |
|---|---|
| `partdb.url`, `partdb.token`, `partdb.ssl_pruefen` | Part-DB-Verbindung (in der Oberfläche änderbar) |
| `output.ordner`, `output.encoding`, `output.trennzeichen` | CSV-Ausgabe; erscheinen Umlaute im P-touch Editor falsch, `encoding: cp1252` |
| `labels.tag` | Part-DB-Tag für „braucht Bauteil-Label“ (Standard `noLabel`) |
| `labels.vorlagen_ordner`, `labels.box_vorlage` | Ordner der `.lbx`-Vorlagen und Vorlage des Box-Labels |
| `labels.kategorie_quelle` | `part` = Kategorien der Bauteile, `box` = Inhalt der Box |
| `labels.trenner` | trennt mehrere Bauteile, Footprints und Kategorien in einem Feld (Standard ` \| `) |
| `labels.fach_regex` | Muster für die Fachnummer in der Bestandsbeschreibung |
| `labels.standard_felder` | Felder für Boxen ohne bekannte Lagerart |
| `druck.modus` | `lokal` (alles auf einem Windows-PC) oder `helfer` (Docker + Druck-Helfer) |
| `druck.direktdruck`, `druck.drucker`, `druck.schnitt` | Direktdruck, Windows-Druckername (leer = aus der Vorlage), Schnittoptionen |

Nach Änderungen an `config.yaml` den Server bzw. Container neu starten.

## Daten in Part-DB

Boxen sind Part-DB-**Lagerorte**. Der Name hat das Format `<Kürzel>-<Abteile>.<Nummer>`, z. B. `MIC-2.0021`; die
Nummer läuft je Kürzel fort und wird aus den vorhandenen Lagerorten ermittelt. Im Kommentar stehen die übrigen Angaben:

```
Lagerinhalt: Microcontroller Lagerfarbe: Dunkelgrau Lagerabteile: 2 Lagerart: WinBin2
```

Der Barcode auf dem Box-Label ist der Part-DB-Lagerort-Barcode (`S` + vierstellige ID), den der Part-DB-Scanner
direkt erkennt. Lagerorte, bei denen die Lagerart als Parameter hinterlegt ist, werden ebenfalls erkannt.

## Sicherheit

- Die Weboberfläche hat **keine Anmeldung**. Jeder, der den Port erreicht, kann Boxen in Part-DB anlegen und
  Einstellungen ändern. Nur im LAN betreiben, den Port nicht ins Internet freigeben; ein Reverse-Proxy nur mit eigener
  Authentifizierung.
- Part-DB- und Helfer-Token liegen im Klartext in `/config/einstellungen.yaml`.
- Die Verbindung des Druck-Helfers ist ohne Reverse-Proxy mit TLS unverschlüsselt (`ws://`).

## Entwicklung

```bash
pip install -r requirements.txt pytest
python -m pytest tests
```

Die Tests laufen gegen ein simuliertes Part-DB und feste Testvorlagen (`tests/vorlagen/`); Windows-spezifische Tests
überspringen sich unter Linux. Der Workflow [`.github/workflows/docker.yml`](.github/workflows/docker.yml) führt die
Tests bei jedem Push aus und veröffentlicht danach das Image (`main` → `:latest`, Tag `v1.2.3` → `:1.2.3` und `:1.2`).

Image selbst bauen:

```bash
docker build -t labeltool:latest .
# andere Benutzer-IDs: --build-arg PUID=1000 --build-arg PGID=1000
```

Server ohne Docker mit Umgebungsvariablen starten: `LABELTOOL_CONFIG` (Pfad zur `config.yaml`), `LABELTOOL_HOST`,
`LABELTOOL_PORT`, `LABELTOOL_NO_BROWSER=1`. `LABELTOOL_INSTALLER` zeigt auf die Setup-Datei, die unter
*Einstellungen → Druck-Helfer* angeboten wird (Standard: `/app/helfer-installer/Labeltool-Helfer-Setup.exe` im Image,
sonst `installer/dist/Labeltool-Helfer-Setup.exe` im Projekt).
