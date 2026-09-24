# Benutzername in der gemeinsamen E-Mail-Signatur

Die Signatur und E-Mail-Vorlagen unterstuetzen `${User.Name}`,
`${User.FirstName}` und `${User.LastName}`. Auswahl unter Schreibender Benutzer;
in den Signatur-Einstellungen werden ausschliesslich diese Felder angeboten.
Der Einstellungseditor zeigt weiterhin die gemeinsame Quelle, nicht den Namen
des gerade angemeldeten Administrators.

Die Identitaet kommt aus dem authentifizierten Mailbox-Akteur, nie aus dem
Absenderkonto oder einem Kundenkontakt. Explizite Vorlagenwerte koennen die
Benutzerwerte nicht ueberschreiben. Namen werden HTML-escaped eingesetzt.
Ohne aktiven Hub-Benutzer gilt fuer den Gesamtname Ihr Kosmos Team; fehlende
Vor-/Nachnamen bleiben leer. Vorhandene Benutzer ohne Namensangaben verwenden
wie sonst im Hub ihren Anzeigenamen (Fallback: Anmeldename).

Aufloesung erfolgt beim Laden der Vorlage oder Erstellen einer Antwort, auch
innerhalb von `${Company.EmailSignature}` und dem alten `${userSignature}`.
Gespeicherte Entwuerfe und geplante E-Mails behalten den sichtbaren Text,
einschliesslich manueller Anpassungen. Zitate werden nicht umgeschrieben.
Es wird keine zusaetzliche Signatur in bisher signaturlose Inhalte eingefuegt.

352 gezielte Tests bestanden: Personalisierung, HTML-Sicherheit, Kontextbindung,
Vorlagen, Antworten, Entwurfserhalt, simulierter zeitversetzter Versand,
Mailbox-/Account-Rechte und Architekturkontrolle. Kein neuer Gesamtlauf.
Die Architekturkontrolle verlangte Review der Accounts-Import-/Kontextaenderung:
nur statische Platzhalterdefinitionen hinzugefuegt, keine neue Route, keine
Aenderung an Authentifizierung, CSRF, gemeinsamen Schreibwegen oder Audit.

Live-Helfer `tools/email-signature-live.cjs` testet Desktop und Mobil sowie
personalisierte Vorlagenvorschau. Optionales `--configure` ersetzt ausschliesslich
eine exakt vorhandene Zeile Ihr Kosmos Team durch `${User.Name}` ueber die
normale authentifizierte Settings-Route, mit CSRF und vorheriger HTML-Sicherung.
Kein E-Mail-Versand und keine Aenderung bestehender E-Mail-Vorlagen.

## Live-Stand

- Release `f351d30`, 24.09.2026: vollstaendiges App-Archiv, SHA geprueft;
  Vergleich aller391 Laufzeitdateien zeigte nur die sechs vorgesehenen Aenderungen.
- Keine aktiven Wartungs-/Refresh-/WordPress-/PDF-Jobs beim Neustart, keine
  Migration. Dienst aktiv, internes und oeffentliches healthz erfolgreich.
  Sicherung: `app-before-user-signature-20260924-161305`.
- Desktop1263/Mobil390: alle drei Platzhalter gesucht und lokal eingefuegt.
- Vorhandene Abschlusszeile Ihr Kosmos Team einmalig durch `${User.Name}`
  ersetzt, ausschliesslich ueber die CSRF-geschuetzte und auditierte Settings-Route.
  Urspruengliche Signatur lokal unter ignoriertem `server/outputs/` gesichert.
- Bestehende Vorlagenvorschau separat mit Admin- und Mitarbeiterkonto geprueft:
  jeweils korrekter hinterlegter Name, keine offenen Benutzer-Platzhalter.
  Keine E-Mails gesendet, keine Entwuerfe oder Vorlagen fuer Tests gespeichert.
