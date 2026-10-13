// Fork: Lager-Autodruck - Seite zum Einrichten und Beobachten der Anbindung
// an das Werkstattlager. Texte bewusst direkt auf Deutsch statt ueber die
// i18n-Dateien, damit Bambuddy-Updates ohne Konflikte uebernommen werden.
import { useMemo, useState } from 'react';
import type React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, CheckCheck, Loader2, MoreHorizontal, PackageCheck, Pencil, Play, Plus, RefreshCw, Send, Trash2, X } from 'lucide-react';
import { Card, CardContent, CardHeader } from '../components/Card';
import { Button } from '../components/Button';
import { Toggle } from '../components/Toggle';
import { ConfirmModal } from '../components/ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { lagerAutodruckApi } from '../api/lagerAutodruck';
import type { Job, Konfig, Modus, Regel } from '../api/lagerAutodruck';

type Reiter = 'uebersicht' | 'regeln' | 'buchungen' | 'einstellungen';

const EINGABE =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';

const MODI: { wert: Modus; titel: string; text: string }[] = [
  { wert: 'automatisch', titel: 'Automatisch', text: 'Druckt ohne Nachfrage, sobald der Bestand zu niedrig ist.' },
  { wert: 'ki', titel: 'Nach Dringlichkeit', text: 'Ohne Nachfrage nur bei niedriger Dringlichkeit, sonst erst freigeben. Dringlichkeit nach fester Regel oder per KI (Einstellungen).' },
  { wert: 'freigabe', titel: 'Immer freigeben', text: 'Plant den Druck ein, gestartet wird erst nach deiner Freigabe.' },
  { wert: 'aus', titel: 'Pausiert', text: 'Regel bleibt gespeichert, es wird nichts eingeplant.' },
];

// Status und Dringlichkeit als ruhige Kästchen (Chips). Farben mit
// hell/dunkel-Variante, damit sie auf beiden Hintergründen lesbar sind;
// "fertig" ist bewusst grün und nicht die Akzentfarbe.
const CHIP = 'inline-flex items-center h-6 px-2.5 rounded-full text-xs font-semibold whitespace-nowrap shrink-0';
const TON: Record<string, string> = {
  neutral: 'bg-bambu-dark-tertiary text-bambu-gray-light',
  info: 'bg-blue-500/15 text-blue-700 dark:text-blue-300',
  gut: 'bg-green-500/15 text-green-700 dark:text-green-300',
  warn: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
  krit: 'bg-red-500/15 text-red-700 dark:text-red-300',
};

const JOB_TEXT: Record<string, { text: string; ton: keyof typeof TON }> = {
  wartet_auf_freigabe: { text: 'Wartet auf Freigabe', ton: 'warn' },
  geplant: { text: 'In der Warteschlange', ton: 'info' },
  druckt: { text: 'Druckt', ton: 'info' },
  fertig: { text: 'Fertig', ton: 'gut' },
  fehldruck: { text: 'Fehldruck', ton: 'krit' },
  abgebrochen: { text: 'Abgebrochen', ton: 'neutral' },
  startfehler: { text: 'Start fehlgeschlagen', ton: 'krit' },
  verworfen: { text: 'Verworfen', ton: 'neutral' },
};

const DRINGLICHKEIT_TON: Record<string, keyof typeof TON> = {
  hoch: 'krit',
  mittel: 'warn',
  niedrig: 'neutral',
};

function Chip({ ton, children }: { ton: keyof typeof TON; children: React.ReactNode }) {
  return <span className={`${CHIP} ${TON[ton]}`}>{children}</span>;
}

function DringlichkeitChip({ wert }: { wert: string | null | undefined }) {
  if (!wert) return null;
  return <Chip ton={DRINGLICHKEIT_TON[wert] ?? 'neutral'}>{wert}</Chip>;
}

function StatusChip({ status }: { status: string }) {
  const st = JOB_TEXT[status] ?? { text: status, ton: 'neutral' as const };
  return <Chip ton={st.ton}>{st.text}</Chip>;
}

function zeit(iso: string | null | undefined) {
  if (!iso) return '–';
  return new Date(iso).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' });
}

// Zahlenfeld, das sich leeren laesst: waehrend des Tippens bleibt der Text
// stehen (auch leer), erst beim Verlassen wird auf min begrenzt. Sonst
// springt die Zahl auf dem Tablet sofort zurueck, wenn man sie loescht.
export function ZahlFeld({
  wert, min, leerErlaubt = false, onWert, className, disabled,
}: {
  wert: number | null;
  min: number;
  leerErlaubt?: boolean;
  onWert: (n: number | null) => void;
  className?: string;
  disabled?: boolean;
}) {
  const [text, setText] = useState<string | null>(null);
  return (
    <input
      type="number"
      inputMode="numeric"
      min={min}
      className={className}
      disabled={disabled}
      value={text ?? (wert ?? '')}
      onChange={e => {
        const t = e.target.value;
        setText(t);
        if (t !== '' && Number.isFinite(Number(t))) onWert(Math.max(min, Number(t)));
      }}
      onBlur={() => {
        if (text === null) return;
        if (text === '' || !Number.isFinite(Number(text))) onWert(leerErlaubt ? null : min);
        setText(null);
      }}
    />
  );
}

function dauer(sekunden: number) {
  const h = Math.floor(sekunden / 3600);
  const m = Math.round((sekunden % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

// "2026-10-01" -> "01.10.2026"
function datum(tag: string | null | undefined) {
  if (!tag) return '';
  const [j, m, t] = tag.slice(0, 10).split('-');
  return `${t}.${m}.${j}`;
}

function zahl(n: number | null | undefined) {
  return n === null || n === undefined ? '–' : n.toLocaleString('de-DE');
}

export function LagerAutodruckPage() {
  const [reiter, setReiter] = useState<Reiter>('uebersicht');
  const { hasPermission } = useAuth();
  const darfAendern = hasPermission('settings:update');

  const reiterListe: { id: Reiter; titel: string }[] = [
    { id: 'uebersicht', titel: 'Übersicht' },
    { id: 'regeln', titel: 'Regeln je Teil' },
    { id: 'buchungen', titel: 'Buchungen' },
    { id: 'einstellungen', titel: 'Einstellungen' },
  ];

  return (
    <div className="p-4 md:p-6 pb-24 space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">Lager-Autodruck</h1>
        <p className="text-sm text-bambu-gray mt-1">
          Liest den Bestand aus dem Werkstattlager, druckt fehlende Teile nach und verbucht fertige Drucke zurück.
        </p>
      </div>
      <div className="flex gap-2 flex-wrap border-b border-bambu-dark-tertiary">
        {reiterListe.map(r => (
          <button
            key={r.id}
            onClick={() => setReiter(r.id)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              reiter === r.id
                ? 'border-bambu-green text-white'
                : 'border-transparent text-bambu-gray hover:text-white'
            }`}
          >
            {r.titel}
          </button>
        ))}
      </div>
      {reiter === 'uebersicht' && <Uebersicht darfAendern={darfAendern} />}
      {reiter === 'regeln' && <Regeln darfAendern={darfAendern} />}
      {reiter === 'buchungen' && <Buchungen darfAendern={darfAendern} />}
      {reiter === 'einstellungen' && <Einstellungen darfAendern={darfAendern} />}
    </div>
  );
}

// --- Übersicht -------------------------------------------------------------------
//
// Aufbau nach Dringlichkeit (wie in der Lager-Software): Statuszeile,
// vier Kennzahlen, "Wartet auf Freigabe" (je Druck ein Knopf, dazu
// "Alle freigeben"), "Kann gepackt werden", dann Bestand und Verlauf.

function Kennzahl({ titel, wert, unter, betont }: { titel: string; wert: number | string; unter?: string; betont?: boolean }) {
  return (
    <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl card-shadow p-4 flex flex-col gap-1 min-w-0">
      <span className="text-sm text-bambu-gray">{titel}</span>
      <span className={`text-3xl font-semibold leading-tight tabular-nums ${betont ? 'text-bambu-green' : 'text-white'}`}>{wert}</span>
      {unter && <span className="text-sm text-bambu-gray truncate">{unter}</span>}
    </div>
  );
}

function Bereich({ titel, anzahl, aktion, unter, children }: {
  titel: string; anzahl?: number; aktion?: React.ReactNode; unter?: string; children: React.ReactNode;
}) {
  return (
    <Card>
      <div className="px-5 py-3.5 border-b border-bambu-dark-tertiary flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-white font-semibold flex items-center gap-2">
            {titel}
            {!!anzahl && <span className="text-sm font-normal text-bambu-gray tabular-nums">{anzahl}</span>}
          </h2>
          {unter && <p className="text-xs text-bambu-gray mt-0.5">{unter}</p>}
        </div>
        {aktion}
      </div>
      {children}
    </Card>
  );
}

function Zeilen({ children }: { children: React.ReactNode }) {
  return <div className="divide-y divide-bambu-dark-tertiary">{children}</div>;
}

function Leer({ children }: { children: React.ReactNode }) {
  return <p className="px-5 py-4 text-sm text-bambu-gray">{children}</p>;
}

// Kleines "…"-Menü für seltene Aktionen (z. B. Verwerfen).
function MehrMenue({ eintraege, label }: { eintraege: { text: string; onClick: () => void; gefaehrlich?: boolean; icon?: React.ReactNode }[]; label: string }) {
  const [offen, setOffen] = useState(false);
  return (
    <div className="relative" onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setOffen(false); }}>
      <button
        type="button"
        aria-label={label}
        title={label}
        aria-expanded={offen}
        onClick={() => setOffen(o => !o)}
        className="w-9 h-9 min-h-[44px] md:min-h-0 inline-flex items-center justify-center rounded-lg border border-bambu-dark-tertiary text-bambu-gray-light hover:bg-bambu-dark-tertiary"
      >
        <MoreHorizontal className="w-4 h-4" />
      </button>
      {offen && (
        <div className="absolute right-0 top-[calc(100%+4px)] z-30 min-w-44 p-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary card-shadow">
          {eintraege.map(e => (
            <button
              key={e.text}
              type="button"
              onClick={() => { setOffen(false); e.onClick(); }}
              className={`w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left hover:bg-bambu-dark-tertiary ${e.gefaehrlich ? 'text-red-600 dark:text-red-400' : 'text-white'}`}
            >
              {e.icon}{e.text}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const VERLAUF_ANFANG = 8;

function Uebersicht({ darfAendern }: { darfAendern: boolean }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const darfFreigeben = hasPermission('queue:update_all');
  const [alleAnzeigen, setAlleAnzeigen] = useState(false);
  const [freigabeLaeuft, setFreigabeLaeuft] = useState(false);

  const status = useQuery({ queryKey: ['lager-autodruck', 'status'], queryFn: lagerAutodruckApi.status, refetchInterval: 15000 });
  const jobs = useQuery({ queryKey: ['lager-autodruck', 'jobs'], queryFn: lagerAutodruckApi.jobs, refetchInterval: 15000 });

  const neuLaden = () => queryClient.invalidateQueries({ queryKey: ['lager-autodruck'] });

  const pruefen = useMutation({
    mutationFn: lagerAutodruckApi.pruefen,
    onSuccess: (e) => {
      const angelegt = typeof e.angelegt === 'number' ? e.angelegt : 0;
      showToast(angelegt ? `${angelegt} Druck(e) eingeplant` : `Geprüft: ${String(e.ergebnis ?? 'ok')}`);
      neuLaden();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const freigeben = useMutation({
    mutationFn: lagerAutodruckApi.jobFreigeben,
    onSuccess: (e) => {
      showToast(e.geplanter_start
        ? `Freigegeben - Start ${zeit(e.geplanter_start)}, damit das Druckende passt`
        : 'Freigegeben - startet, sobald ein Drucker frei ist');
      neuLaden();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const verwerfen = useMutation({
    mutationFn: lagerAutodruckApi.jobVerwerfen,
    onSuccess: () => { showToast('Verworfen'); neuLaden(); },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const s = status.data;
  const alleJobs = jobs.data ?? [];
  // Dringendste zuerst: hoch vor mittel vor niedrig, dann älteste zuerst.
  const rang: Record<string, number> = { hoch: 0, mittel: 1, niedrig: 2 };
  const wartend = alleJobs
    .filter(j => j.status === 'wartet_auf_freigabe')
    .sort((a, b) => (rang[a.dringlichkeit ?? ''] ?? 3) - (rang[b.dringlichkeit ?? ''] ?? 3)
      || String(a.created_at).localeCompare(String(b.created_at)));
  const uebrige = alleJobs.filter(j => j.status !== 'wartet_auf_freigabe');
  const laufend = alleJobs.filter(j => j.status === 'druckt' || j.status === 'geplant');
  const druckt = alleJobs.filter(j => j.status === 'druckt').length;
  const sichtbar = alleAnzeigen ? uebrige : uebrige.slice(0, VERLAUF_ANFANG);

  const verfuegbar = (z: NonNullable<typeof s>['uebersicht'][number]) =>
    z.bestand === null ? null : z.bestand + z.in_arbeit - z.nachfrage;
  const knapp = (z: NonNullable<typeof s>['uebersicht'][number]) => {
    const v = verfuegbar(z);
    return v !== null && z.mindestbestand !== null && v <= z.mindestbestand;
  };
  const bestand = [...(s?.uebersicht ?? [])].sort((a, b) =>
    Number(knapp(b)) - Number(knapp(a)) || (a.name ?? '').localeCompare(b.name ?? ''));
  const knappAnzahl = bestand.filter(knapp).length;

  async function alleFreigeben() {
    setFreigabeLaeuft(true);
    let ok = 0;
    for (const j of wartend) {
      try { await lagerAutodruckApi.jobFreigeben(j.id); ok++; } catch (e) { showToast((e as Error).message, 'error'); break; }
    }
    setFreigabeLaeuft(false);
    if (ok) showToast(`${ok} Druck${ok === 1 ? '' : 'e'} freigegeben`);
    neuLaden();
  }

  const statusText = !s ? 'Lade …'
    : !s.eingerichtet ? 'Noch nicht mit dem Lager verbunden'
    : s.aktiv ? 'Autodruck ist eingeschaltet'
    : 'Autodruck ist ausgeschaltet (Buchungen laufen weiter)';

  return (
    <div className="space-y-4">
      <Card>
        <div className="px-5 py-3.5 flex flex-col md:flex-row md:items-center gap-3 justify-between">
          <div className="space-y-0.5 text-sm min-w-0">
            <div className="flex items-center gap-2">
              <span className={`inline-block w-2.5 h-2.5 rounded-full ${s?.letzter_fehler ? 'bg-red-500' : s?.aktiv && s.eingerichtet ? 'bg-green-500' : 'bg-bambu-gray'}`} />
              <span className="text-white font-medium">{statusText}</span>
            </div>
            {s && (
              <div className="text-bambu-gray">
                Letzte Prüfung {zeit(s.letzter_lauf)} · alle {s.intervall_minuten} Min.
                {' · '}Dringlichkeit {s.ki_aktiv ? 'per KI' : 'nach fester Regel'}
                {s.offene_buchungen > 0 && <> · <span className="text-amber-700 dark:text-amber-300 font-medium">{s.offene_buchungen} Buchung{s.offene_buchungen === 1 ? '' : 'en'} noch nicht im Lager</span></>}
              </div>
            )}
            {s?.letzter_fehler && <div className="text-red-600 dark:text-red-400">Fehler: {s.letzter_fehler}</div>}
          </div>
          {darfAendern && (
            <Button variant="secondary" onClick={() => pruefen.mutate()} disabled={pruefen.isPending || !s?.eingerichtet}>
              {pruefen.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
              Jetzt prüfen
            </Button>
          )}
        </div>
      </Card>

      {s?.eingerichtet && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Kennzahl titel="Wartet auf Freigabe" wert={wartend.length} betont={wartend.length > 0}
            unter={wartend.length ? `${wartend.filter(j => j.dringlichkeit === 'hoch').length} davon dringend` : 'nichts freizugeben'} />
          <Kennzahl titel="Läuft gerade" wert={laufend.length}
            unter={laufend.length ? `${druckt} druckt · ${laufend.length - druckt} in der Warteschlange` : 'kein Nachdruck aktiv'} />
          <Kennzahl titel="Unter Mindestbestand" wert={knappAnzahl}
            unter={`von ${bestand.length} Teilen mit Regel`} />
          <Kennzahl titel="Kann gepackt werden" wert={s.packbar?.length ?? 0}
            unter={s.packbar?.length ? 'alles im Lager' : 'keine Bestellung komplett'} />
        </div>
      )}

      {s?.eingerichtet && !s.aktiv && (
        <Bereich titel="Vorschau: das würde jetzt eingeplant" anzahl={s.vorschau.length}
          unter="Der Autodruck ist aus – es wird nichts in die Warteschlange gestellt. So kannst du Regeln und Mindestbestände prüfen.">
          {!s.vorschau.length ? <Leer>Im Moment würde nichts gedruckt. („Jetzt prüfen“ rechnet neu.)</Leer> : (
            <Zeilen>
              {s.vorschau.map(v => (
                <div key={v.part_id} className="px-5 py-3 text-sm space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-white font-medium">{v.druecke} Druck{v.druecke === 1 ? '' : 'e'} {v.name} ({v.stueck} Stück)</span>
                    <DringlichkeitChip wert={v.dringlichkeit} />
                    <Chip ton={v.ohne_freigabe ? 'info' : 'warn'}>{v.ohne_freigabe ? 'würde automatisch starten' : 'würde auf Freigabe warten'}</Chip>
                    {v.termin && <span className="text-bambu-gray">Versand bis {datum(v.termin)}</span>}
                  </div>
                  <div className="text-bambu-gray">{v.begruendung}</div>
                </div>
              ))}
            </Zeilen>
          )}
        </Bereich>
      )}

      {wartend.length > 0 && (
        <Bereich
          titel="Wartet auf deine Freigabe"
          anzahl={wartend.length}
          unter="Dringendste zuerst."
          aktion={darfFreigeben && wartend.length > 1 && (
            <Button size="sm" variant="secondary" onClick={alleFreigeben} disabled={freigabeLaeuft || freigeben.isPending}>
              {freigabeLaeuft ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCheck className="w-4 h-4" />}
              Alle {wartend.length} freigeben
            </Button>
          )}
        >
          <Zeilen>
            {wartend.map(j => (
              <div key={j.id} className="px-5 py-3 flex flex-col md:flex-row md:items-center gap-3 justify-between">
                <JobBeschreibung job={j} ohneStatus />
                {darfFreigeben && (
                  <div className="flex gap-2 shrink-0 self-end md:self-auto">
                    <Button size="sm" onClick={() => freigeben.mutate(j.id)} disabled={freigeben.isPending || freigabeLaeuft} className="min-w-32">
                      <Play className="w-4 h-4" /> Freigeben
                    </Button>
                    <MehrMenue label={`Weitere Aktionen für ${j.part_name ?? 'Druck'}`} eintraege={[
                      { text: 'Verwerfen', gefaehrlich: true, icon: <X className="w-4 h-4" />, onClick: () => verwerfen.mutate(j.id) },
                    ]} />
                  </div>
                )}
              </div>
            ))}
          </Zeilen>
        </Bereich>
      )}

      {!!s?.packbar?.length && (
        <Bereich titel="Kann gepackt werden" anzahl={s.packbar.length}
          unter="Für diese Bestellungen liegt alles im Lager – frühester Versand zuerst.">
          <Zeilen>
            {s.packbar.map(p => (
              <div key={p.order_id} className="px-5 py-3 text-sm">
                <div className="flex items-center gap-2 flex-wrap">
                  <PackageCheck className="w-4 h-4 text-green-600 dark:text-green-400 shrink-0" />
                  <span className="text-white font-medium">{p.kunde}</span>
                  {p.versand_bis && <span className="text-bambu-gray">Versand bis {datum(p.versand_bis)}</span>}
                </div>
                <pre className="mt-1 ml-6 text-bambu-gray whitespace-pre-wrap font-sans">{p.text.split('\n').slice(1).join('\n') || p.text}</pre>
              </div>
            ))}
          </Zeilen>
        </Bereich>
      )}

      <Bereich titel="Bestand der Teile mit Regel" anzahl={bestand.length} unter={bestand.length ? 'Knappe Teile zuerst. Verfügbar = Bestand + in Arbeit − offene Aufträge.' : undefined}>
        {!bestand.length ? (
          <Leer>Noch keine Daten – lege unter „Regeln je Teil“ fest, welche Teile automatisch nachgedruckt werden, und klicke „Jetzt prüfen“.</Leer>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[640px]">
              <thead className="text-bambu-gray text-left text-xs uppercase tracking-wide">
                <tr>
                  <th className="py-2.5 pl-5 pr-4 font-medium">Teil</th>
                  <th className="py-2.5 pr-4 font-medium text-right">Bestand</th>
                  <th className="py-2.5 pr-4 font-medium text-right">In Arbeit</th>
                  <th className="py-2.5 pr-4 font-medium text-right">Aufträge</th>
                  <th className="py-2.5 pr-4 font-medium text-right">Verfügbar / Mindest</th>
                  <th className="py-2.5 pr-5 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {bestand.map(z => {
                  const v = verfuegbar(z);
                  const istKnapp = knapp(z);
                  return (
                    <tr key={z.regel_id} className="border-t border-bambu-dark-tertiary">
                      <td className="py-2.5 pl-5 pr-4">
                        <div className="text-white font-medium">{z.name ?? z.part_id}</div>
                        {(z.hinweis || z.termin) && (
                          <div className="text-xs text-bambu-gray">{[z.hinweis, z.termin && `Versand bis ${datum(z.termin)}`].filter(Boolean).join(' · ')}</div>
                        )}
                      </td>
                      <td className="py-2.5 pr-4 text-right tabular-nums text-white">
                        {zahl(z.bestand)}
                        {z.gesperrt > 0 && <span className="text-bambu-gray" title="In gesperrten Lagerorten – zählt nicht als verfügbar"> (+{zahl(z.gesperrt)} gesperrt)</span>}
                      </td>
                      <td className="py-2.5 pr-4 text-right tabular-nums text-bambu-gray">{z.in_arbeit ? `+${zahl(z.in_arbeit)}` : '–'}</td>
                      <td className="py-2.5 pr-4 text-right tabular-nums text-bambu-gray">{z.nachfrage ? `−${zahl(z.nachfrage)}` : '–'}</td>
                      <td className="py-2.5 pr-4 text-right tabular-nums">
                        <span className={`font-semibold ${istKnapp ? 'text-bambu-green' : 'text-white'}`}>{zahl(v)}</span>
                        <span className="text-bambu-gray"> / {zahl(z.mindestbestand)}</span>
                      </td>
                      <td className="py-2.5 pr-5">
                        {istKnapp
                          ? <Chip ton={z.in_arbeit > 0 ? 'info' : 'warn'}>{z.in_arbeit > 0 ? 'wird nachgedruckt' : 'knapp'}</Chip>
                          : <Chip ton="neutral">ok</Chip>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Bereich>

      <Bereich titel="Letzte Druckaufträge" anzahl={uebrige.length}>
        {!uebrige.length ? <Leer>Noch keine Druckaufträge angelegt.</Leer> : (
          <>
            <Zeilen>
              {sichtbar.map(j => (
                <div key={j.id} className="px-5 py-3"><JobBeschreibung job={j} /></div>
              ))}
            </Zeilen>
            {uebrige.length > VERLAUF_ANFANG && (
              <button
                type="button"
                onClick={() => setAlleAnzeigen(a => !a)}
                className="w-full py-3 border-t border-bambu-dark-tertiary text-sm font-medium text-bambu-green hover:bg-bambu-dark"
              >
                {alleAnzeigen ? 'Weniger anzeigen' : `Alle ${uebrige.length} anzeigen`}
              </button>
            )}
          </>
        )}
      </Bereich>
    </div>
  );
}

function JobBeschreibung({ job, ohneStatus }: { job: Job; ohneStatus?: boolean }) {
  const meta = [
    `angelegt ${zeit(job.created_at)}`,
    job.freigegeben_von && `freigegeben von ${job.freigegeben_von}`,
    job.finished_at && `beendet ${zeit(job.finished_at)}`,
    job.druckdauer_s ? `Druckdauer ${dauer(job.druckdauer_s)}` : null,
    job.queue_item_id && `Warteschlange #${job.queue_item_id}`,
  ].filter(Boolean).join(' · ');
  return (
    <div className="text-sm min-w-0 flex-1">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-white font-medium">{job.stueck}× {job.part_name ?? job.part_id}</span>
        {!ohneStatus && <StatusChip status={job.status} />}
        <DringlichkeitChip wert={job.dringlichkeit} />
      </div>
      {job.begruendung && <div className="text-bambu-gray mt-0.5">{job.begruendung}</div>}
      <div className="text-xs text-bambu-gray mt-0.5">{meta}</div>
      {job.geplanter_start && new Date(job.geplanter_start) > new Date() && ['geplant', 'wartet_auf_freigabe'].includes(job.status) && (
        <div className="text-xs text-blue-700 dark:text-blue-300 mt-0.5">
          Druckende optimiert: startet {zeit(job.geplanter_start)}, damit er nicht in der Nacht fertig wird
        </div>
      )}
    </div>
  );
}

// --- Regeln ----------------------------------------------------------------------

const LEERE_REGEL: Regel = {
  part_id: '',
  part_name: null,
  archive_id: null,
  library_file_id: null,
  plate_id: null,
  dateiname: '',
  stueck_je_druck: 1,
  modus: 'ki',
  printer_id: null,
  target_model: null,
  target_location: null,
  max_drucke_pro_tag: null,
};

function Regeln({ darfAendern }: { darfAendern: boolean }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const regeln = useQuery({ queryKey: ['lager-autodruck', 'regeln'], queryFn: lagerAutodruckApi.regeln });
  const drucker = useQuery({ queryKey: ['lager-autodruck', 'drucker'], queryFn: lagerAutodruckApi.drucker });
  const [bearbeiten, setBearbeiten] = useState<Regel | null>(null);
  const [loeschen, setLoeschen] = useState<Regel | null>(null);

  const loeschenMut = useMutation({
    mutationFn: (id: number) => lagerAutodruckApi.regelLoeschen(id),
    onSuccess: () => {
      showToast('Regel gelöscht');
      setLoeschen(null);
      queryClient.invalidateQueries({ queryKey: ['lager-autodruck'] });
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const druckerName = (r: Regel) => {
    if (r.printer_id) return drucker.data?.find(d => d.id === r.printer_id)?.name ?? `Drucker #${r.printer_id}`;
    if (r.target_model) return `Freier ${r.target_model}${r.target_location ? ` (${r.target_location})` : ''}`;
    return 'Modell aus der Druckdatei';
  };

  if (bearbeiten) {
    return <RegelFormular regel={bearbeiten} onFertig={() => setBearbeiten(null)} />;
  }

  return (
    <div className="space-y-4">
      {darfAendern && (
        <Button onClick={() => setBearbeiten({ ...LEERE_REGEL })}>
          <Plus className="w-4 h-4" /> Neue Regel
        </Button>
      )}
      {regeln.isLoading && <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />}
      {regeln.data?.length === 0 && (
        <Card><CardContent className="text-sm text-bambu-gray">
          Noch keine Regeln. Eine Regel legt fest, welche Druckdatei für ein Lager-Teil gedruckt wird, wie viele Stück pro Druck herauskommen und ob automatisch gedruckt werden darf.
        </CardContent></Card>
      )}
      <div className="grid gap-3">
        {regeln.data?.map(r => (
          <Card key={r.id}>
            <CardContent className="flex flex-col md:flex-row md:items-center justify-between gap-3">
              <div className="text-sm space-y-1">
                <div className="text-white font-medium">{r.part_name ?? r.part_id}</div>
                <div className="text-bambu-gray">
                  {MODI.find(m => m.wert === r.modus)?.titel} · „{r.dateiname}“ · {r.stueck_je_druck} Stück/Druck · {druckerName(r)}
                </div>
                <div className="text-xs text-bambu-gray">
                  {r.max_drucke_pro_tag !== null ? `max. ${r.max_drucke_pro_tag} Drucke/Tag` : 'kein Tageslimit'}
                </div>
              </div>
              {darfAendern && (
                <div className="flex gap-2 shrink-0">
                  <Button size="sm" variant="secondary" onClick={() => setBearbeiten({ ...r })}>
                    <Pencil className="w-4 h-4" /> Bearbeiten
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setLoeschen(r)}>
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
      {loeschen && (
        <ConfirmModal
          title="Regel löschen?"
          message={`Für „${loeschen.part_name ?? loeschen.part_id}“ wird danach nichts mehr automatisch nachgedruckt. Bereits eingeplante Drucke bleiben in der Warteschlange.`}
          confirmText="Löschen"
          variant="danger"
          isLoading={loeschenMut.isPending}
          onConfirm={() => loeschen.id && loeschenMut.mutate(loeschen.id)}
          onCancel={() => setLoeschen(null)}
        />
      )}
    </div>
  );
}

function Feld({ titel, hilfe, children }: { titel: string; hilfe?: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-sm text-white">{titel}</span>
      {children}
      {hilfe && <span className="block text-xs text-bambu-gray">{hilfe}</span>}
    </label>
  );
}

function RegelFormular({ regel, onFertig }: { regel: Regel; onFertig: () => void }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [r, setR] = useState<Regel>(regel);
  const [suche, setSuche] = useState('');
  const [druckerArt, setDruckerArt] = useState<'fest' | 'modell'>(regel.printer_id ? 'fest' : 'modell');

  const teile = useQuery({ queryKey: ['lager-autodruck', 'teile'], queryFn: lagerAutodruckApi.teile, retry: false });
  // Druckdatei aus dem Dateimanager (nur hochgeladen) oder aus dem Archiv (schon gedruckt).
  const [quelle, setQuelle] = useState<'datei' | 'archiv'>(regel.archive_id ? 'archiv' : 'datei');
  const archive = useQuery({
    queryKey: ['lager-autodruck', quelle, suche],
    queryFn: () => (quelle === 'archiv' ? lagerAutodruckApi.archive(suche) : lagerAutodruckApi.dateien(suche)),
  });
  const gewaehlteId = quelle === 'archiv' ? r.archive_id : r.library_file_id;
  const drucker = useQuery({ queryKey: ['lager-autodruck', 'drucker'], queryFn: lagerAutodruckApi.drucker });

  const modelle = useMemo(
    () => Array.from(new Set((drucker.data ?? []).map(d => d.modell).filter((m): m is string => !!m))).sort(),
    [drucker.data],
  );
  const standorte = useMemo(
    () => Array.from(new Set((drucker.data ?? []).map(d => d.standort).filter((s): s is string => !!s))).sort(),
    [drucker.data],
  );
  const setze = <K extends keyof Regel>(k: K, v: Regel[K]) => setR(alt => ({ ...alt, [k]: v }));

  const speichern = useMutation({
    mutationFn: () =>
      lagerAutodruckApi.regelSpeichern({
        ...r,
        printer_id: druckerArt === 'fest' ? r.printer_id : null,
        target_model: druckerArt === 'modell' ? r.target_model : null,
        target_location: druckerArt === 'modell' ? r.target_location : null,
      }),
    onSuccess: (antwort) => {
      showToast(antwort.warnung ?? 'Regel gespeichert', antwort.warnung ? 'warning' : 'success');
      queryClient.invalidateQueries({ queryKey: ['lager-autodruck'] });
      onFertig();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const gewaehltesArchiv = archive.data?.find(a => a.id === gewaehlteId);

  return (
    <Card>
      <CardHeader><h2 className="text-white font-semibold">{r.id ? 'Regel bearbeiten' : 'Neue Regel'}</h2></CardHeader>
      <CardContent className="space-y-5">
        <Feld titel="Teil im Lager" hilfe={teile.error ? `Teile konnten nicht geladen werden: ${(teile.error as Error).message}` : undefined}>
          <select
            className={EINGABE}
            value={r.part_id}
            onChange={e => {
              const t = teile.data?.find(x => x.id === e.target.value);
              setR(alt => ({ ...alt, part_id: e.target.value, part_name: t?.name ?? null }));
            }}
          >
            <option value="">– Teil wählen –</option>
            {r.part_id && !teile.data?.some(t => t.id === r.part_id) && (
              <option value={r.part_id}>{r.part_name ?? r.part_id}</option>
            )}
            {teile.data?.map(t => (
              <option key={t.id} value={t.id}>
                {t.name} (Bestand {zahl(t.bestand)}, Mindest {zahl(t.mindestbestand)})
              </option>
            ))}
          </select>
        </Feld>

        <Feld
          titel="Druckdatei"
          hilfe={quelle === 'datei'
            ? 'Geslicte Dateien (.gcode.3mf) aus dem Dateimanager – müssen noch nie gedruckt worden sein.'
            : 'Dateien, die schon einmal gedruckt wurden.'}
        >
          <div className="flex gap-2">
            {([['datei', 'Aus dem Dateimanager'], ['archiv', 'Aus dem Archiv']] as const).map(([wert, titel]) => (
              <button
                key={wert}
                type="button"
                className={`px-3 py-1.5 rounded-lg text-sm border ${quelle === wert ? 'border-bambu-green text-white bg-bambu-green/10' : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'}`}
                onClick={() => {
                  setQuelle(wert);
                  setSuche('');
                  setR(alt => ({ ...alt, archive_id: null, library_file_id: null }));
                }}
              >
                {titel}
              </button>
            ))}
          </div>
          <input className={EINGABE} placeholder="Suchen …" value={suche} onChange={e => setSuche(e.target.value)} />
          <select
            className={EINGABE}
            value={gewaehlteId ?? ''}
            onChange={e => {
              const id = e.target.value ? Number(e.target.value) : null;
              const a = archive.data?.find(x => x.id === id);
              setR(alt => ({
                ...alt,
                archive_id: quelle === 'archiv' ? id : null,
                library_file_id: quelle === 'datei' ? id : null,
                dateiname: !alt.dateiname && a ? a.name : alt.dateiname,
                target_model: alt.target_model ?? a?.modell ?? null,
              }));
            }}
          >
            <option value="">{archive.data && archive.data.length === 0 && !suche ? (quelle === 'datei' ? '– keine geslicte Datei im Dateimanager –' : '– Archiv ist leer –') : '– Datei wählen –'}</option>
            {gewaehlteId && !gewaehltesArchiv && <option value={gewaehlteId}>{quelle === 'archiv' ? 'Archiv' : 'Datei'} #{gewaehlteId}</option>}
            {archive.data?.map(a => (
              <option key={a.id} value={a.id}>
                {a.name}{a.modell ? ` · ${a.modell}` : ''}{a.gramm ? ` · ${Math.round(a.gramm)} g` : ''}
              </option>
            ))}
          </select>
        </Feld>

        <div className="grid md:grid-cols-2 gap-4">
          <Feld titel="Name im Lager (Druck-Zuordnung)" hilfe="Unter diesem Namen verbucht das Lager fertige Drucke auf das Teil. Wird beim Speichern im Lager angelegt.">
            <input className={EINGABE} value={r.dateiname} onChange={e => setze('dateiname', e.target.value)} />
          </Feld>
          <Feld titel="Stück pro Druck">
            <ZahlFeld min={1} className={EINGABE} wert={r.stueck_je_druck} onWert={n => setze('stueck_je_druck', n ?? 1)} />
          </Feld>
        </div>

        <div className="space-y-2">
          <span className="text-sm text-white">Wie automatisch?</span>
          <div className="grid md:grid-cols-2 gap-2">
            {MODI.map(m => (
              <button
                key={m.wert}
                type="button"
                onClick={() => setze('modus', m.wert)}
                className={`text-left p-3 rounded-lg border transition-colors ${
                  r.modus === m.wert ? 'border-bambu-green bg-bambu-green/10' : 'border-bambu-dark-tertiary hover:border-bambu-gray'
                }`}
              >
                <div className="text-sm text-white font-medium">{m.titel}</div>
                <div className="text-xs text-bambu-gray">{m.text}</div>
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <span className="text-sm text-white">Drucker</span>
          <div className="flex gap-4 text-sm">
            <label className="flex items-center gap-2 text-white">
              <input type="radio" checked={druckerArt === 'modell'} onChange={() => setDruckerArt('modell')} /> Irgendein freier Drucker vom Modell
            </label>
            <label className="flex items-center gap-2 text-white">
              <input type="radio" checked={druckerArt === 'fest'} onChange={() => setDruckerArt('fest')} /> Fester Drucker
            </label>
          </div>
          {druckerArt === 'fest' ? (
            <select className={EINGABE} value={r.printer_id ?? ''} onChange={e => setze('printer_id', e.target.value ? Number(e.target.value) : null)}>
              <option value="">– Drucker wählen –</option>
              {drucker.data?.map(d => <option key={d.id} value={d.id}>{d.name}{d.modell ? ` (${d.modell})` : ''}</option>)}
            </select>
          ) : (
            <div className="grid md:grid-cols-2 gap-4">
              <select className={EINGABE} value={r.target_model ?? ''} onChange={e => setze('target_model', e.target.value || null)}>
                <option value="">Modell aus der Druckdatei</option>
                {modelle.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              <select className={EINGABE} value={r.target_location ?? ''} onChange={e => setze('target_location', e.target.value || null)}>
                <option value="">Alle Standorte</option>
                {standorte.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          )}
        </div>

        <div className="grid md:grid-cols-2 gap-4">
          <Feld titel="Höchstens Drucke pro Tag" hilfe="Leer = kein Limit. Wann gestartet wird, regelt „Druckende optimieren“ in den Einstellungen.">
            <ZahlFeld min={0} leerErlaubt className={EINGABE} wert={r.max_drucke_pro_tag ?? null} onWert={n => setze('max_drucke_pro_tag', n)} />
          </Feld>
          {gewaehltesArchiv?.druckzeit_s ? (
            <div className="text-sm text-bambu-gray self-center">Druckdauer laut Datei: {dauer(gewaehltesArchiv.druckzeit_s)}</div>
          ) : null}
        </div>

        <div className="flex gap-2 justify-end">
          <Button variant="secondary" onClick={onFertig}>Abbrechen</Button>
          <Button onClick={() => speichern.mutate()} disabled={speichern.isPending || !r.part_id || !(r.archive_id || r.library_file_id)}>
            {speichern.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            Speichern
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// --- Buchungen -------------------------------------------------------------------

const BUCHUNG_OK = ['gebucht', 'gebucht_fehldruck', 'schon_gebucht', 'zur_pruefung'];

function Buchungen({ darfAendern }: { darfAendern: boolean }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const buchungen = useQuery({ queryKey: ['lager-autodruck', 'buchungen'], queryFn: lagerAutodruckApi.buchungen, refetchInterval: 15000 });
  const erneut = useMutation({
    mutationFn: lagerAutodruckApi.buchungErneut,
    onSuccess: () => { showToast('Wird erneut gesendet'); queryClient.invalidateQueries({ queryKey: ['lager-autodruck'] }); },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <Card>
      <CardHeader>
        <h2 className="text-white font-semibold">Meldungen ans Lager</h2>
        <p className="text-xs text-bambu-gray mt-1">
          Jeder fertige Druck wird hier gespeichert und ans Lager gesendet. Ist das Lager nicht erreichbar, wird automatisch erneut gesendet.
          „unbekannt“ heißt: im Lager fehlt die Druck-Zuordnung für diesen Namen - dort zuordnen und nachbuchen.
        </p>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {!buchungen.data?.length ? (
          <p className="text-sm text-bambu-gray">Noch keine Buchungen.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-bambu-gray text-left">
              <tr>
                <th className="py-2 pr-4">Zeit</th>
                <th className="py-2 pr-4">Drucker</th>
                <th className="py-2 pr-4">Name</th>
                <th className="py-2 pr-4">Meldung</th>
                <th className="py-2 pr-4">Lager</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {buchungen.data.map(b => (
                <tr key={b.id} className="border-t border-bambu-dark-tertiary align-top">
                  <td className="py-2 pr-4 text-bambu-gray whitespace-nowrap">{zeit(b.created_at)}</td>
                  <td className="py-2 pr-4 text-white">{b.drucker}</td>
                  <td className="py-2 pr-4 text-white">{b.dateiname}</td>
                  <td className={`py-2 pr-4 ${b.status === 'fertig' ? 'text-green-700 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                    {b.status === 'fertig' ? 'Fertig' : 'Fehldruck'}{b.gramm ? ` · ${Math.round(b.gramm)} g` : ''}
                  </td>
                  <td className="py-2 pr-4">
                    {b.zustand === 'gesendet' ? (
                      <span className={BUCHUNG_OK.includes(b.ergebnis ?? '') ? 'text-green-700 dark:text-green-400' : 'text-amber-700 dark:text-amber-300'}>{b.ergebnis}</span>
                    ) : (
                      <span className="text-amber-700 dark:text-amber-300" title={b.fehler ?? ''}>
                        {b.zustand === 'offen' ? 'wird gesendet' : `Fehler (${b.versuche} Versuche)`}
                      </span>
                    )}
                  </td>
                  <td className="py-2 text-right">
                    {darfAendern && b.zustand === 'fehler' && (
                      <Button size="sm" variant="ghost" onClick={() => erneut.mutate(b.id)}>
                        <Send className="w-4 h-4" /> Erneut
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

// --- Einstellungen ---------------------------------------------------------------

function Einstellungen({ darfAendern }: { darfAendern: boolean }) {
  const konfig = useQuery({ queryKey: ['lager-autodruck', 'konfig'], queryFn: lagerAutodruckApi.konfig });
  if (!konfig.data) return <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />;
  return <EinstellungenFormular start={konfig.data} darfAendern={darfAendern} />;
}

function EinstellungenFormular({ start, darfAendern }: { start: Konfig; darfAendern: boolean }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [k, setK] = useState<Konfig>(start);
  const setze = <K extends keyof Konfig>(feld: K, wert: Konfig[K]) => setK(alt => ({ ...alt, [feld]: wert }));

  const speichern = useMutation({
    mutationFn: () => lagerAutodruckApi.konfigSpeichern(k),
    onSuccess: (neu) => {
      setK(neu);
      showToast('Einstellungen gespeichert');
      queryClient.invalidateQueries({ queryKey: ['lager-autodruck'] });
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const testen = useMutation({
    mutationFn: lagerAutodruckApi.verbindungTesten,
    onSuccess: (e) => showToast(e.meldung, e.ok ? 'success' : 'error'),
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-white font-medium">Autodruck eingeschaltet</div>
              <div className="text-xs text-bambu-gray">Aus = es wird nichts mehr eingeplant. Fertige Drucke werden trotzdem weiter verbucht.</div>
            </div>
            <Toggle checked={k.aktiv} onChange={v => setze('aktiv', v)} disabled={!darfAendern} />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-white font-medium">Auch Handdrucke verbuchen</div>
              <div className="text-xs text-bambu-gray">
                Meldet jeden fertigen Druck ans Lager, nicht nur die vom Autodruck. Ersetzt den alten „Druck fertig“-Webhook.
                Wenn an: den alten Webhook in den Bambuddy-Benachrichtigungen entfernen, sonst wird doppelt gebucht.
              </div>
            </div>
            <Toggle checked={k.alle_drucke_verbuchen} onChange={v => setze('alle_drucke_verbuchen', v)} disabled={!darfAendern} />
          </div>
          <Feld titel="Bestand prüfen alle … Minuten">
            <ZahlFeld min={1} className={EINGABE} wert={k.intervall_minuten} disabled={!darfAendern} onWert={n => setze('intervall_minuten', n ?? 1)} />
          </Feld>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="text-white font-semibold">Druckende optimieren</h2>
          <p className="text-xs text-bambu-gray mt-1">
            Gedruckt wird rund um die Uhr – es wird nichts gesperrt. Nur der Start wird so gelegt, dass ein Druck nicht zwischen den beiden Zeiten fertig wird.
            Würde er dort fertig, startet er später und ist genau zur „Fertig frühestens“-Zeit fertig. Die Druckdauer kommt aus der Druckdatei.
            Meldungen „wartet auf Freigabe“ und „eingeplant“ aus dieser Zeit kommen gesammelt zur „Fertig frühestens“-Zeit.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div className="text-white font-medium">Druckende optimieren</div>
            <Toggle checked={k.nachtruhe_aktiv} onChange={v => setze('nachtruhe_aktiv', v)} disabled={!darfAendern} />
          </div>
          <div className="grid md:grid-cols-3 gap-4">
            <Feld titel="Fertig spätestens um" hilfe="z. B. wann du schlafen gehst">
              <input type="time" className={EINGABE} value={k.schlafen} disabled={!darfAendern || !k.nachtruhe_aktiv} onChange={e => setze('schlafen', e.target.value)} />
            </Feld>
            <Feld titel="Fertig frühestens um" hilfe="z. B. wann du aufstehst">
              <input type="time" className={EINGABE} value={k.aufstehen} disabled={!darfAendern || !k.nachtruhe_aktiv} onChange={e => setze('aufstehen', e.target.value)} />
            </Feld>
            <Feld titel="Puffer (Minuten)" hilfe="Für Aufheizen und falls der Druck länger dauert als geschätzt">
              <ZahlFeld min={0} className={EINGABE} wert={k.puffer_minuten} disabled={!darfAendern || !k.nachtruhe_aktiv} onWert={n => setze('puffer_minuten', n ?? 0)} />
            </Feld>
          </div>
        </CardContent>
      </Card>

      <Benachrichtigungen k={k} setze={setze} darfAendern={darfAendern} />

      <Card>
        <CardHeader>
          <h2 className="text-white font-semibold">Verbindung zum Lager (Supabase)</h2>
          <p className="text-xs text-bambu-gray mt-1">Dieselben Werte wie bisher in Vercel (DRUCK_SUPABASE_*). Bambuddy meldet sich mit dem Drucker-Konto an.</p>
        </CardHeader>
        <CardContent className="space-y-4">
          <Feld titel="Supabase-Adresse" hilfe="z.B. https://abcdefgh.supabase.co">
            <input className={EINGABE} value={k.supabase_url} disabled={!darfAendern} onChange={e => setze('supabase_url', e.target.value)} />
          </Feld>
          <Feld titel="Anon-Schlüssel">
            <input className={EINGABE} value={k.supabase_anon_key} disabled={!darfAendern} onChange={e => setze('supabase_anon_key', e.target.value)} />
          </Feld>
          <div className="grid md:grid-cols-2 gap-4">
            <Feld titel="E-Mail des Drucker-Kontos">
              <input className={EINGABE} value={k.email} disabled={!darfAendern} onChange={e => setze('email', e.target.value)} />
            </Feld>
            <Feld titel="Passwort" hilfe="Leer lassen = unverändert">
              <input type="password" className={EINGABE} value={k.passwort} disabled={!darfAendern} onChange={e => setze('passwort', e.target.value)} />
            </Feld>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="text-white font-semibold">Dringlichkeit</h2>
          <p className="text-xs text-bambu-gray mt-1">
            Jeder Nachdruck bekommt eine Dringlichkeit. „hoch“ wird in der Warteschlange vorgezogen; im Modus „Nach Dringlichkeit“ druckt nur „niedrig“ ohne Freigabe.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="text-sm text-bambu-gray space-y-1">
            <div className="text-white">Feste Regel (kostenlos, gilt immer ohne KI):</div>
            <div>• <span className="text-red-300">hoch</span>: Bestand 0 und offene Aufträge brauchen das Teil</div>
            <div>• <span className="text-yellow-300">mittel</span>: verfügbar höchstens halber Mindestbestand</div>
            <div>• <span className="text-bambu-green">niedrig</span>: alles andere</div>
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-white font-medium">KI-Einschätzung verwenden (Claude)</div>
              <div className="text-xs text-bambu-gray">
                Statt der festen Regel bewertet Claude die Dringlichkeit und schreibt eine Begründung. Kostet bei jeder Prüfung, bei der etwas nachgedruckt werden muss, einen kleinen Betrag über deinen Anthropic-Schlüssel. Fällt die KI aus, gilt wieder die feste Regel.
              </div>
            </div>
            <Toggle checked={k.ki_verwenden} onChange={v => setze('ki_verwenden', v)} disabled={!darfAendern} />
          </div>
          <div className="grid md:grid-cols-2 gap-4">
          <Feld titel="Anthropic-API-Schlüssel">
            <input type="password" className={EINGABE} value={k.anthropic_api_key} disabled={!darfAendern} onChange={e => setze('anthropic_api_key', e.target.value)} />
          </Feld>
          <Feld titel="Modell">
            <input className={EINGABE} value={k.ki_modell} disabled={!darfAendern} onChange={e => setze('ki_modell', e.target.value)} />
          </Feld>
          </div>
        </CardContent>
      </Card>

      {darfAendern && (
        <div className="flex gap-2 justify-end">
          <Button variant="secondary" onClick={() => testen.mutate()} disabled={testen.isPending || !k.eingerichtet}>
            {testen.isPending && <Loader2 className="w-4 h-4 animate-spin" />} Verbindung testen
          </Button>
          <Button onClick={() => speichern.mutate()} disabled={speichern.isPending}>
            {speichern.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />} Speichern
          </Button>
        </div>
      )}
    </div>
  );
}

function Benachrichtigungen({ k, setze, darfAendern }: {
  k: Konfig;
  setze: <K extends keyof Konfig>(feld: K, wert: Konfig[K]) => void;
  darfAendern: boolean;
}) {
  const { showToast } = useToast();
  const daten = useQuery({ queryKey: ['lager-autodruck', 'benachrichtigung'], queryFn: lagerAutodruckApi.benachrichtigung });
  const testen = useMutation({
    mutationFn: lagerAutodruckApi.benachrichtigungTesten,
    onSuccess: (e) => showToast(e.meldung, e.ok ? 'success' : 'warning'),
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const umschalten = (liste: (string | number)[], wert: string | number) =>
    liste.includes(wert) ? liste.filter(x => x !== wert) : [...liste, wert];

  return (
    <Card>
      <CardHeader>
        <h2 className="text-white font-semibold">Benachrichtigungen</h2>
        <p className="text-xs text-bambu-gray mt-1">
          Nutzt die Kanäle aus Bambuddy (ntfy, Telegram, E-Mail …), die du unter Einstellungen → Benachrichtigungen anlegst.
          Den alten Webhook ans Lager hier nicht auswählen.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="text-sm text-white">An diese Kanäle senden</div>
          {!daten.data?.kanaele.length && (
            <div className="text-sm text-bambu-gray">Noch keine Kanäle angelegt – unter Einstellungen → Benachrichtigungen einen hinzufügen.</div>
          )}
          {daten.data?.kanaele.map(kanal => (
            <label key={kanal.id} className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={k.melden_an.includes(kanal.id)}
                disabled={!darfAendern}
                onChange={() => setze('melden_an', umschalten(k.melden_an, kanal.id) as number[])}
              />
              {kanal.name} <span className="text-bambu-gray">({kanal.typ}{kanal.aktiv ? '' : ', ausgeschaltet'})</span>
            </label>
          ))}
        </div>
        <div className="space-y-2">
          <div className="text-sm text-white">Bei diesen Ereignissen</div>
          {daten.data?.ereignisse.map(ereignis => (
            <label key={ereignis.id} className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={k.melden.includes(ereignis.id)}
                disabled={!darfAendern}
                onChange={() => setze('melden', umschalten(k.melden, ereignis.id) as string[])}
              />
              {ereignis.titel}
            </label>
          ))}
          {k.melden.includes('platte') && daten.data && !daten.data.platte_bestaetigen && (
            <div className="text-xs text-amber-700 dark:text-amber-300">
              „Platte abräumen“ kommt nur, wenn in Bambuddy unter Einstellungen → Workflow → Warteschlange
              „Druckplatte-Bestätigung erforderlich“ eingeschaltet ist. Sonst startet der nächste Druck ohne Nachfrage.
            </div>
          )}
        </div>
        {daten.data?.kanaele.some(kanal => kanal.typ === 'telegram') && (
          <div className="space-y-1">
            <label className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={k.telegram_knoepfe}
                disabled={!darfAendern}
                onChange={() => setze('telegram_knoepfe', !k.telegram_knoepfe)}
              />
              Knöpfe in Telegram
            </label>
            <p className="text-xs text-bambu-gray">
              Freigaben kommen mit „Freigeben“ und „Verwerfen“, fertige Drucke mit Kamerabild und „Platte ist frei“ –
              antippen genügt. Es zählt nur ein Tippen aus dem eingetragenen Chat. Aus, wenn derselbe Bot schon von einem
              anderen Programm abgefragt wird.
            </p>
          </div>
        )}
        {darfAendern && (
          <Button variant="secondary" size="sm" onClick={() => testen.mutate()} disabled={testen.isPending}>
            {testen.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            Testnachricht senden (vorher speichern)
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
