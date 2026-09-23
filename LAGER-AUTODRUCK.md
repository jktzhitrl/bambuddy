# Lager-Autodruck (Erweiterung in diesem Fork)

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
  `verfügbar = Bestand + in Arbeit − Bedarf aus offenen Aufträgen` (Sets werden in
  ihre Einzelteile zerlegt). Liegt das bei oder unter dem Mindestbestand, wird
  auf den doppelten Mindestbestand aufgefüllt, in ganzen Drucken.
  „In Arbeit“ sind Drucke, die in der Warteschlange stehen, gerade laufen oder
  fertig sind, deren Buchung aber noch nicht im Lager angekommen ist. So wird
  nichts doppelt gedruckt.
- **KI-Einschätzung (Claude)**: Jeder Nachdruck bekommt die Dringlichkeit
  niedrig, mittel oder hoch, mit Begründung. „hoch“ rückt in der Warteschlange
  nach vorn. Ohne API-Schlüssel gelten feste Regeln.
- **Regel je Teil**:
  - *Automatisch*: druckt ohne Nachfrage.
  - *KI entscheidet*: ohne Nachfrage nur bei Dringlichkeit „niedrig“, sonst Freigabe.
  - *Immer freigeben*: wartet immer auf Freigabe.
  - *Pausiert*: plant nichts ein.

  Dazu gehören pro Regel: die Druckdatei aus dem Archiv, Stück pro Druck, ein
  fester Drucker oder „irgendein freier Drucker vom Modell X“ (optional
  Standort), höchstens N Drucke pro Tag und ein Zeitfenster.
- **Freigeben**: Aufträge, die eine Freigabe brauchen, stehen mit „manueller
  Start“ in der Bambuddy-Warteschlange. Freigeben geht über **Start** in der
  Warteschlange oder über **Freigeben** auf der Seite „Lager-Autodruck“.
- **Verbuchen**: Nach jedem Druckende ruft Bambuddy im Lager die bestehende
  Funktion `druck_verbuchen` auf, dieselbe, die vorher der Webhook benutzt hat.
  Meldungen werden zuerst lokal gespeichert und bei Netzproblemen automatisch
  erneut gesendet. Drucke, die in der Warteschlange abgebrochen wurden, werden
  nicht verbucht.
- **Sicherung**: Meldet das Lager auf eine Buchung „unbekannt“ (die
  Druck-Zuordnung fehlt), hält die Regel an, damit nicht endlos nachgedruckt
  wird. Freigeben: Zuordnung im Lager prüfen, dann die Regel in Bambuddy einmal
  speichern.

## Einrichten

1. Diesen Fork statt des Original-Bambuddy installieren, z. B. per Docker mit
   eigenem Build (`docker build -t bambuddy-lager .`). Die neuen Tabellen legt
   Bambuddy beim Start selbst an.
2. In Bambuddy links **Lager-Autodruck → Einstellungen** öffnen:
   - Supabase-Adresse, Anon-Schlüssel, E-Mail und Passwort des Drucker-Kontos
     eintragen. Das sind dieselben Werte wie bisher `DRUCK_SUPABASE_*` in Vercel.
   - Optional den Anthropic-API-Schlüssel für die KI-Einschätzung eintragen.
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

Update einspielen:

```bash
git remote add upstream https://github.com/maziggy/bambuddy.git   # einmalig
git fetch upstream
git merge upstream/main
```

Konflikte kann es höchstens an den markierten Stellen geben.
