# Sweetneeds Monitor

Überwacht den kompletten Katalog von **sweetneeds.de** alle 30 Minuten und meldet:

| Ereignis | Bedeutung |
|---|---|
| 🆕 Neu | Produkt oder Variante neu im Shop |
| 📉 Preis gesenkt / 📈 Preis erhöht | Verkaufspreis hat sich geändert (mit altem Preis und Prozent) |
| 🏷️ Neuer Rabatt | Preis gleich, aber neuer bzw. geänderter Streichpreis |
| ✅ Wieder da / ⛔ Ausverkauft | Verfügbarkeit hat sich geändert |
| 🗑️ Entfernt | Produkt nicht mehr gelistet |

**So funktioniert es:** Eine GitHub Action lädt den öffentlichen Shopify-Katalog (`/products.json`),
vergleicht ihn mit dem letzten Stand und speichert die Ergebnisse in `docs/data/`.
GitHub Pages zeigt das Dashboard (`docs/index.html`) an, auch unter deiner eigenen Domain.
Du brauchst keinen Server, keine Datenbank und zahlst nichts.

```
GitHub Actions (alle 30 Min.)  ──►  scraper/monitor.py  ──►  docs/data/*.json  ──►  Dashboard (GitHub Pages)
                                           │
                                           └──►  Push (ntfy) / Telegram / E-Mail
```

---

## Einrichtung (ca. 10 Minuten)

### 1. Repository anlegen
1. Auf github.com ein neues Repository erstellen, z. B. `sweetneeds-monitor`.
   *Öffentlich* ist für GitHub Pages im Gratis-Tarif nötig. Bei *privat* brauchst du GitHub Pro,
   oder du nutzt Netlify bzw. Cloudflare Pages (siehe unten).
2. Diesen Ordner hochladen:
   ```bash
   cd sweetneeds-monitor
   git init && git add . && git commit -m "Sweetneeds Monitor"
   git branch -M main
   git remote add origin https://github.com/fortnitehacker0815/sweetneeds-monitor.git
   git push -u origin main
   ```

### 2. Dashboard veröffentlichen (GitHub Pages)
*Settings → Pages → Source: „Deploy from a branch“ → Branch `main`, Ordner `/docs` → Save.*
Nach etwa einer Minute ist das Dashboard unter `https://fortnitehacker0815.github.io/sweetneeds-monitor/` erreichbar.

### 3. Eigene Domain (optional)
1. Beim Domain-Anbieter einen **CNAME**-Eintrag anlegen:
   `monitor.deine-domain.de → fortnitehacker0815.github.io`
2. *Settings → Pages → Custom domain:* `monitor.deine-domain.de` eintragen und anschließend „Enforce HTTPS“ aktivieren.

### 4. Benachrichtigungen einrichten
Die Werte trägst du unter *Settings → Secrets and variables → Actions* ein.
Du kannst einen oder mehrere Kanäle nutzen.

**Push aufs Handy mit ntfy (empfohlen, kostenlos, ohne Account):**
1. Die App **ntfy** installieren (iOS/Android) und einen schwer zu erratenden Kanalnamen abonnieren,
   z. B. `sweetneeds-a8f3k29x`. Wer den Namen kennt, kann mitlesen, deshalb sollte er zufällig sein.
2. Unter **Secret** anlegen: `NTFY_TOPIC = sweetneeds-a8f3k29x`

**Telegram:** Mit @BotFather einen Bot erstellen. Dann die Secrets `TELEGRAM_BOT_TOKEN` und
`TELEGRAM_CHAT_ID` anlegen. Die Chat-ID findest du, indem du dem Bot schreibst und
`https://api.telegram.org/bot<TOKEN>/getUpdates` aufrufst.

**E-Mail (SMTP):** Die Secrets `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`,
`MAIL_FROM` und `MAIL_TO` anlegen. Mehrere Empfänger trennst du mit Komma.

**Optionale Variablen** (Reiter *Variables*, nicht *Secrets*):

| Variable | Beispiel | Wirkung |
|---|---|---|
| `NOTIFY_TYPES` | `new,price_drop` | Nur diese Ereignisse melden (Standard: `new,price_drop,price_up,discount,back_in_stock`) |
| `MIN_PRICE_CHANGE_PCT` | `5` | Preisänderungen unter 5 % nicht melden; sie erscheinen trotzdem im Dashboard |

### 5. Starten
*Actions → Sweetneeds Monitor → „Run workflow“.* Danach läuft der Monitor automatisch alle 30 Minuten.
Der erste Lauf legt nur den Ausgangsstand an und verschickt noch keine Benachrichtigungen.

---

**Push-Nachrichten:** Jede einzelne Änderung kommt als eigene Nachricht mit Produktbild, nie gesammelt. Ein Tipp darauf öffnet direkt die Produktseite. Bei vielen Änderungen auf einmal werden die Nachrichten in ruhigem Tempo nacheinander verschickt (ca. 1 pro Sekunde, ab der 50. ca. alle 5 Sekunden), damit ntfy keine verwirft. Die wichtigsten (größte Preissenkung, höchster Rabatt) kommen zuerst.

## Dashboard
- **Neuigkeiten:** Chronologischer Feed aller Änderungen, filterbar nach Typ. Einträge seit deinem letzten Besuch sind blau markiert.
- **Neu im Shop:** Produkte, die in den letzten 14 Tagen veröffentlicht oder neu erkannt wurden.
- **Top-Rabatte:** Alle verfügbaren Produkte mit Streichpreis, sortiert nach Rabatt.
- **Alle Produkte:** Durchsuchbare und sortierbare Tabelle mit Filter nach Kategorie und Verfügbarkeit.
- Ein Klick auf ein Produkt zeigt den **Preisverlauf** und den Link zum Shop.
- Die Seite lädt alle 5 Minuten automatisch neu und unterstützt den Dunkelmodus.

## Alternativen zum Hosting
- **Netlify / Cloudflare Pages:** Repository verbinden und als Publish-Verzeichnis `docs` angeben. Das funktioniert auch mit privaten Repositories. Die Prüfung läuft weiterhin über GitHub Actions.
- **Eigener Webspace mit Cron:** `python3 scraper/monitor.py` per Cronjob ausführen und `docs/` als Webroot verwenden.

## Lokal testen
```bash
python3 scraper/monitor.py
python3 -m http.server 8765 --directory docs   # http://localhost:8765
```

## Hinweise
- Es wird nur der öffentliche Produkt-Feed gelesen: zwei Anfragen pro Lauf, also sehr schonend.
- Wenn ein Abruf plötzlich weniger als die Hälfte der Produkte liefert, bricht das Skript ab.
  So vermeidet es Fehlalarme wie „300 Produkte entfernt“.
- GitHub pausiert geplante Workflows in öffentlichen Repositories nach 60 Tagen ohne Aktivität.
  Da hier bei jeder Änderung Daten committet werden, passiert das normalerweise nicht.
- Anderer Shopify-Shop? In `.github/workflows/monitor.yml` unter `env:` die Zeile `SHOP_URL: https://anderer-shop.de` ergänzen.
