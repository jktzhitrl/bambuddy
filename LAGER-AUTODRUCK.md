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
- **Dringlichkeit**: Jeder Nachdruck bekommt die Dringlichkeit niedrig, mittel
  oder hoch. „hoch“ rückt in der Warteschlange nach vorn. Standardmäßig nach
  fester, kostenloser Regel: *hoch* = Bestand 0 und offene Aufträge brauchen
  das Teil, *mittel* = verfügbar höchstens halber Mindestbestand, sonst
  *niedrig*. Optional (Schalter „KI-Einschätzung verwenden“, standardmäßig
  aus) bewertet stattdessen Claude, mit Begründung; das kostet pro Aufruf
  etwas über den eigenen Anthropic-Schlüssel.
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
- **Freigeben**: Aufträge, die eine Freigabe brauchen, stehen mit „manueller
  Start“ in der Bambuddy-Warteschlange. Freigeben geht über **Start** in der
  Warteschlange oder über **Freigeben** auf der Seite „Lager-Autodruck“.
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

## Einrichten

1. Diesen Fork statt des Original-Bambuddy installieren, z. B. per Docker mit
   eigenem Build (`docker build -t bambuddy-lager .`). Die neuen Tabellen legt
   Bambuddy beim Start selbst an.
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

Update einspielen:

```bash
git remote add upstream https://github.com/maziggy/bambuddy.git   # einmalig
git fetch upstream
git merge upstream/main
```

Konflikte kann es höchstens an den markierten Stellen geben.
