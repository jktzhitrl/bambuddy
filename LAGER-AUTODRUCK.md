# Druckübersicht – Bambuddy mit Lager-Autodruck

Dieser Fork heißt in der Oberfläche **Druckübersicht** und trägt das Logo von
B&E 3D Druck. Er basiert auf [Bambuddy](https://github.com/maziggy/bambuddy)
von maziggy (Lizenz: AGPL-3.0, siehe `LICENSE`). Im Programmcode, in den
Datenbank- und Ordnernamen heißt er weiter „bambuddy“, damit Updates vom
Original ohne Konflikte übernommen werden können. Der Hinweis auf das Original
(GitHub-Link unten in der Seitenleiste, Stream-Overlay) bleibt erhalten.

## Lager-Autodruck

Dieser Fork von [Bambuddy](https://github.com/maziggy/bambuddy) verbindet
Bambuddy direkt mit dem Werkstattlager (Supabase). Bambuddy

1. liest regelmäßig den Bestand aus dem Lager,
2. stellt fehlende Teile automatisch in die Druckwarteschlange, je nach Regel
   sofort oder erst nach Freigabe, und
3. verbucht jeden fertigen Druck direkt im Lager (Stück und Material).

Damit fallen im Lager-Programm der Vercel-Cron, der `bambuddy-poller` und der
„Druck fertig“-Webhook weg.

## So funktioniert es

```
Lager (Supabase) ◄── Bestand lesen / fertige Drucke verbuchen ──► Bambuddy (Modul „Lager-Autodruck“) ──► Drucker
```

Ein Durchlauf läuft alle *N* Minuten oder per Knopf „Jetzt prüfen“:

- **Bedarf**: Für jedes Teil mit Regel gilt
  `verfügbar = Bestand + in Arbeit − Bedarf aus offenen Aufträgen`. Bestellte
  Sets werden wie im Lager beim Abschließen zuerst aus fertigen Sets gedeckt,
  nur der Rest wird in Einzelteile zerlegt (auch Sets in Sets). Liegt das bei oder unter dem Mindestbestand, wird
  auf den doppelten Mindestbestand aufgefüllt, in ganzen Drucken.
  „In Arbeit“ sind Drucke, die in der Warteschlange stehen, gerade laufen oder
  fertig sind, deren Buchung aber noch nicht im Lager angekommen ist. So wird
  nichts doppelt gedruckt.
- **Dringlichkeit**: Jeder Nachdruck bekommt die Dringlichkeit niedrig, mittel
  oder hoch. „hoch“ rückt in der Warteschlange nach vorn. Standardmäßig nach
  fester, kostenloser Regel: *hoch* = Bestand 0 und offene Aufträge brauchen
  das Teil, *mittel* = verfügbar höchstens halber Mindestbestand, sonst
  *niedrig*. Optional (Schalter „KI-Einschätzung verwenden“, standardmäßig
  aus) bewertet stattdessen Claude, mit Begründung; das kostet pro Aufruf
  etwas über den eigenen Anthropic-Schlüssel.
- **Liefertermine**: Hat ein Auftrag im Lager ein „Versand bis“, zählt je Teil
  der früheste Termin, für den das Teil noch fehlt (fertige Sets gehen zuerst an
  die Aufträge mit dem frühesten Termin). Reichen Bestand und laufende Drucke
  nicht für die offenen Aufträge, wird die Dringlichkeit angehoben: Versand in
  höchstens 2 Tagen oder schon überfällig → *hoch*, in höchstens 7 Tagen →
  mindestens *mittel*. Das gilt auch, wenn die KI es lockerer sieht. Bei gleicher
  Dringlichkeit kommt der frühere Termin zuerst in die Warteschlange. Die
  Übersicht zeigt den Termin in der Spalte „Versand bis“.
- **Regel je Teil**:
  - *Automatisch*: druckt ohne Nachfrage.
  - *Nach Dringlichkeit*: ohne Nachfrage nur bei Dringlichkeit „niedrig“, sonst Freigabe.
  - *Immer freigeben*: wartet immer auf Freigabe.
  - *Pausiert*: plant nichts ein.

  Dazu gehören pro Regel: die Druckdatei aus dem Archiv, Stück pro Druck, ein
  fester Drucker oder „irgendein freier Drucker vom Modell X“ (optional
  Standort) und höchstens N Drucke pro Tag.
- **Druckende optimieren** (Einstellungen, gilt für alle Regeln): Gedruckt wird
  rund um die Uhr, es wird nichts gesperrt. Nur der Start wird so gelegt, dass
  kein automatischer Druck zwischen „Fertig spätestens um“ (z. B. Schlafengehen)
  und „Fertig frühestens um“ (z. B. Aufstehen) fertig wird. Aus der Druckdauer
  laut Druckdatei plus Puffer wird das Ende berechnet. Fiele es dazwischen,
  startet der Druck später, sodass er genau zur „Fertig frühestens“-Zeit fertig ist.
  Geprüft wird laufend und genau zu dem Zeitpunkt, ab dem ein wartender Druck
  nicht mehr rechtzeitig fertig würde. Auch wenn ein Drucker erst spät frei
  wird, rutscht der Druck so nicht in die Nacht. Ein Druck, der über die
  Bambuddy-Warteschlange von Hand mit **Start** gestartet wird, läuft sofort.
- **Vorschau**: Ist der Autodruck ausgeschaltet, wird trotzdem gerechnet und in
  der Übersicht angezeigt, was jetzt eingeplant würde – ohne etwas anzulegen.
- **Freigeben**: Aufträge, die eine Freigabe brauchen, stehen mit „manueller
  Start“ in der Bambuddy-Warteschlange. Freigeben geht über **Start** in der
  Warteschlange, über **Freigeben** auf der Seite „Lager-Autodruck“ oder per
  Knopf in Telegram (siehe unten).
- **Telegram-Knöpfe** (Einstellungen → Benachrichtigungen, „Knöpfe in
  Telegram“, an, sobald ein Telegram-Kanal ausgewählt ist):
  - „Freigabe nötig“ kommt je Teil als eigene Nachricht mit **✅ Freigeben** und
    **🗑 Verwerfen**. Ein Knopf gilt für alle wartenden Drucke dieses Teils bis zu
    dieser Nachricht; später dazugekommene bekommen ihre eigene Nachricht.
  - „Druck fertig – Platte abräumen“ (Meldung „platte“) kommt mit Kamerabild
    und **🧹 Platte ist frei**. Der Knopf macht dasselbe wie „Druckplatte als
    freigegeben markieren“ auf der Druckerkarte, danach startet der nächste
    Druck. Voraussetzung: in Bambuddy unter *Einstellungen → Workflow →
    Warteschlange* „Druckplatte-Bestätigung erforderlich“ einschalten, sonst
    startet der nächste Druck ohne Nachfrage und es kommt keine Meldung. Das
    Kamerabild folgt Bambuddys Einstellung für das Fertig-Foto.
  - Nach dem Tippen verschwinden die Knöpfe und das Ergebnis steht unter der
    Nachricht („2 Drucke freigegeben (Ben)“).
  - Bambuddy holt die Knopfdrücke selbst bei Telegram ab (getUpdates); es
    braucht keine öffentliche Adresse. Angenommen wird nur ein Tippen aus dem
    Chat, der im Kanal eingetragen ist – in einer Gruppe darf jedes Mitglied.
    Fragt ein anderes Programm denselben Bot ab, den Schalter ausschalten
    (oder einen eigenen Bot nehmen); dann kommen die Meldungen ohne Knöpfe.
- **Verbuchen**: Nach jedem Druckende ruft Bambuddy im Lager die bestehende
  Funktion `druck_verbuchen` auf, dieselbe, die vorher der Webhook benutzt hat.
  Meldungen werden zuerst lokal gespeichert und bei Netzproblemen automatisch
  erneut gesendet. Drucke, die in der Warteschlange abgebrochen wurden, werden
  nicht verbucht.
- **Benachrichtigungen** (Einstellungen): Bambuddy meldet sich über die
  vorhandenen Kanäle (ntfy, Telegram, E-Mail …, angelegt unter
  *Einstellungen → Benachrichtigungen*), wenn ein Druck auf Freigabe wartet, eine
  Buchung nach 3 Versuchen nicht ankommt oder das Lager sie nicht verbucht
  (z. B. „unbekannt“), eine Regel angehalten wurde oder das Lager 3 Prüfungen
  in Folge nicht erreichbar ist (und wenn es wieder da ist). Optional auch bei
  jedem automatisch eingeplanten Druck. Jede Meldung kommt nur einmal, nicht
  bei jeder Prüfung. „Wartet auf Freigabe“ und „eingeplant“ aus der Nacht
  (zwischen den beiden Zeiten von „Druckende optimieren“) kommen gesammelt zur
  „Fertig frühestens“-Zeit; Fehlermeldungen kommen sofort.
- **Sicherung**: Meldet das Lager auf eine Buchung „unbekannt“ (die
  Druck-Zuordnung fehlt), hält die Regel an, damit nicht endlos nachgedruckt
  wird. Freigeben: Zuordnung im Lager prüfen, dann die Regel in Bambuddy einmal
  speichern.

## Auf den Server bringen (bisher Docker mit offiziellem Image)

### Schnell: mit dem Umstell-Skript

Im Ordner der bisherigen Installation (dort, wo die `docker-compose.yml` liegt):

```bash
cd /pfad/zu/bambuddy
curl -fsSL https://raw.githubusercontent.com/jktzhitrl/bambuddy/main/deploy/druckuebersicht-umstellen.sh -o druckuebersicht-umstellen.sh
curl -fsSL https://raw.githubusercontent.com/jktzhitrl/bambuddy/main/deploy/druckuebersicht-zurueck.sh -o druckuebersicht-zurueck.sh
bash druckuebersicht-umstellen.sh
```

Das Skript prüft Docker, `docker-compose.yml` und freien Speicher, baut das
Image (Bambuddy läuft solange weiter), stoppt Bambuddy, sichert die Daten als
`bambuddy-daten-<Datum>.tgz`, passt die `docker-compose.yml` an (Original:
`docker-compose.yml.vorher`), startet und prüft, ob die Druckübersicht
antwortet. Geht zwischendurch etwas schief, stellt es den vorherigen Zustand
wieder her. Anderer Port als 8000: `PORT=1234 bash druckuebersicht-umstellen.sh`.

Zurück zum Original: `bash druckuebersicht-zurueck.sh`.
Spätere Updates: `bash druckuebersicht-umstellen.sh` einfach erneut ausführen.

### Von Hand

Das fertige Bambuddy-Image von GitHub (`ghcr.io/maziggy/bambuddy`) enthält
diese Erweiterung **nicht** – das Image wird aus dem Fork selbst gebaut und in
der **bestehenden** `docker-compose.yml` eingetragen.

> Nicht den Fork in einen neuen Ordner klonen und dort `docker compose up`
> starten: Compose hängt den Ordnernamen vor die Volumes, Bambuddy bekäme ein
> neues, leeres Daten-Volume.

1. **In den bisherigen Ordner** (dort, wo die `docker-compose.yml` liegt) und
   den Namen des Daten-Volumes herausfinden:
   ```bash
   cd /pfad/zu/bambuddy
   docker volume ls | grep bambuddy        # z. B. bambuddy_bambuddy_data
   ```
2. **Sichern** (Container dafür kurz stoppen, Volume-Namen aus Schritt 1):
   ```bash
   docker compose stop
   docker run --rm -v bambuddy_bambuddy_data:/data -v "$PWD":/backup alpine \
     tar czf /backup/bambuddy-data-$(date +%F).tgz -C /data .
   cp docker-compose.yml docker-compose.yml.vorher
   ```
3. **Image aus dem Fork bauen** (eigener Ordner nur für den Quellcode; dauert
   beim ersten Mal einige Minuten, auf einem Raspberry Pi deutlich länger):
   ```bash
   git clone https://github.com/jktzhitrl/bambuddy.git ~/druckuebersicht-src
   cd ~/druckuebersicht-src
   docker build -t druckuebersicht:latest .
   ```
4. **In der bisherigen `docker-compose.yml`** beim Dienst `bambuddy` die
   Image-Zeile ändern und verhindern, dass das Original nachgeladen wird:
   ```yaml
       image: druckuebersicht:latest
       pull_policy: never
   ```
   (Eine vorhandene Zeile `build: .` entfernen. Alles andere – Ports,
   Volumes, Umgebungsvariablen, `network_mode` – bleibt, wie es ist.)
5. **Starten und Log ansehen:**
   ```bash
   cd /pfad/zu/bambuddy
   docker compose up -d
   docker compose logs -f               # auf Fehler achten, Strg+C beendet
   ```
   Bambuddy legt die neuen Tabellen beim Start selbst an.
6. **Zurück zum Original**, falls etwas nicht passt:
   `cp docker-compose.yml.vorher docker-compose.yml && docker compose up -d`.
   Die zusätzlichen Tabellen stören das Original nicht; notfalls das Backup
   aus Schritt 2 zurückspielen.

**Spätere Updates** (eigene Änderungen oder übernommene Bambuddy-Updates):
```bash
cd ~/druckuebersicht-src && git pull && docker build -t druckuebersicht:latest .
cd /pfad/zu/bambuddy && docker compose up -d
```

Empfohlene Reihenfolge am ersten Abend: einrichten, Regeln anlegen, **Autodruck
noch aus lassen** und „Jetzt prüfen“ drücken. Solange der Autodruck aus ist,
zeigt die Übersicht eine **Vorschau**, was eingeplant würde – ohne etwas in die
Warteschlange zu stellen und ohne KI-Kosten. Erst wenn das stimmt, einschalten –
anfangs am besten mit Modus „Immer freigeben“.

## Einrichten

1. Diesen Fork statt des Original-Bambuddy installieren, wie oben unter
   „Auf den Server bringen“ beschrieben.
2. In Bambuddy links **Lager-Autodruck → Einstellungen** öffnen:
   - Supabase-Adresse, Anon-Schlüssel, E-Mail und Passwort des Drucker-Kontos
     eintragen. Das sind dieselben Werte wie bisher `DRUCK_SUPABASE_*` in Vercel.
   - Optional: Anthropic-API-Schlüssel eintragen und „KI-Einschätzung verwenden“
     einschalten. Ohne das gelten die festen Regeln, kostenlos.
   - „Druckende optimieren“ prüfen (Standard: fertig spätestens 22:00, frühestens
     07:00, 15 Minuten Puffer).
   - Unter *Benachrichtigungen* die Kanäle auswählen, speichern und
     „Testnachricht senden“.
   - **Speichern**, dann **Verbindung testen**.
3. Unter **Regeln je Teil** für jedes Teil eine Regel anlegen. Beim Speichern
   legt Bambuddy im Lager die passende `druck_zuordnung` an.
4. **Autodruck eingeschaltet** aktivieren.
5. Wenn **Auch Handdrucke verbuchen** an ist (Standard): den alten
   „Druck fertig“-Webhook in Bambuddy unter *Benachrichtigungen* entfernen,
   sonst wird doppelt gebucht.
6. Danach im Lager-Programm abschalten: `bambuddy-poller`, Vercel-Cron
   `auto-druck-vorschlaege`, `api/druck-fertig.js`.

## Updates vom Original übernehmen

Das Modul liegt fast vollständig in eigenen Dateien:

- `backend/app/models/lager_autodruck.py`
- `backend/app/services/lager_autodruck/`
- `backend/app/api/routes/lager_autodruck.py`
- `frontend/src/pages/LagerAutodruckPage.tsx`, `frontend/src/api/lagerAutodruck.ts`
- Tests: `backend/tests/unit/test_lager_autodruck.py`,
  `backend/tests/integration/test_lager_autodruck_api.py`

Im bestehenden Code sind nur wenige Zeilen ergänzt, alle mit
`Fork: Lager-Autodruck` markiert:

- `backend/app/main.py`: Start/Stopp, Aufruf nach Druckende, Router
- `backend/app/core/database.py`: Modell registrieren
- `frontend/src/App.tsx`, `frontend/src/components/Layout.tsx`: Route und Menüpunkt
- `requirements.txt`: `anthropic`
- `frontend/src/__tests__/pages/SettingsPage.test.tsx`: Menüpunkt in der
  erwarteten Reihenfolge
- `backend/tests/unit/test_outbound_url_ssrf_guards.py`: Supabase-Adresse als
  geprüfte URL eingetragen

Für Name und Logo „Druckübersicht“ zusätzlich (ebenfalls markiert bzw. nur
Texte/Bilder): `frontend/index.html`, `frontend/public/manifest.json`,
`frontend/public/img/` (Logo, Favicons, App-Symbole), `Layout.tsx`,
`LoginPage.tsx`, `SetupPage.tsx`, `CameraPage.tsx`, `StreamOverlayPage.tsx`
(nur Seitentitel), die Texte in `i18n/locales/de.ts` und `en.ts`,
`app_name` in `notification_service.py` und `email_service.py`, zwei
Frontend-Tests, die den Namen prüfen, und `container_name` in
`docker-compose.yml`. Bei Konflikten in `de.ts`/`en.ts` einfach die Version
vom Original nehmen und „Bambuddy“ in den Texten wieder ersetzen.

Update einspielen:

```bash
git remote add upstream https://github.com/maziggy/bambuddy.git   # einmalig
git fetch upstream
git merge upstream/main
```

Konflikte kann es höchstens an den markierten Stellen geben.
