// Fork: Lager-Autodruck - Seite zum Einrichten und Beobachten der Anbindung
// an das Werkstattlager. Texte bewusst direkt auf Deutsch statt ueber die
// i18n-Dateien, damit Bambuddy-Updates ohne Konflikte uebernommen werden.
import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Loader2, Pencil, Play, Plus, RefreshCw, Send, Trash2, X } from 'lucide-react';
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

const JOB_TEXT: Record<string, { text: string; farbe: string }> = {
  wartet_auf_freigabe: { text: 'Wartet auf Freigabe', farbe: 'text-yellow-400' },
  geplant: { text: 'In der Warteschlange', farbe: 'text-blue-400' },
  druckt: { text: 'Druckt', farbe: 'text-bambu-green' },
  fertig: { text: 'Fertig', farbe: 'text-bambu-green' },
  fehldruck: { text: 'Fehldruck', farbe: 'text-red-400' },
  abgebrochen: { text: 'Abgebrochen', farbe: 'text-bambu-gray' },
  verworfen: { text: 'Verworfen', farbe: 'text-bambu-gray' },
};

const DRINGLICHKEIT_FARBE: Record<string, string> = {
  hoch: 'bg-red-500/20 text-red-300',
  mittel: 'bg-yellow-500/20 text-yellow-300',
  niedrig: 'bg-bambu-green/20 text-bambu-green',
};

function zeit(iso: string | null | undefined) {
  if (!iso) return '–';
  return new Date(iso).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' });
}

function dauer(sekunden: number) {
  const h = Math.floor(sekunden / 3600);
  const m = Math.round((sekunden % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
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

function Uebersicht({ darfAendern }: { darfAendern: boolean }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const darfFreigeben = hasPermission('queue:update_all');

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
        ? `Freigegeben - wegen Nachtruhe Start frühestens ${zeit(e.geplanter_start)}`
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
  const wartend = (jobs.data ?? []).filter(j => j.status === 'wartet_auf_freigabe');
  const uebrige = (jobs.data ?? []).filter(j => j.status !== 'wartet_auf_freigabe');

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="flex flex-col md:flex-row md:items-center gap-4 justify-between">
          <div className="space-y-1 text-sm">
            <div className="flex items-center gap-2">
              <span className={`inline-block w-2.5 h-2.5 rounded-full ${s?.aktiv && s.eingerichtet ? 'bg-bambu-green' : 'bg-bambu-gray'}`} />
              <span className="text-white font-medium">
                {!s ? 'Lade …' : !s.eingerichtet ? 'Noch nicht mit dem Lager verbunden' : s.aktiv ? 'Autodruck ist eingeschaltet' : 'Autodruck ist ausgeschaltet (Buchungen laufen weiter)'}
              </span>
            </div>
            {s && (
              <div className="text-bambu-gray">
                Letzte Prüfung: {zeit(s.letzter_lauf)} · alle {s.intervall_minuten} Min.
                {' · '}Dringlichkeit {s.ki_aktiv ? 'per KI' : 'nach fester Regel'}
                {s.offene_buchungen > 0 && <> · <span className="text-yellow-400">{s.offene_buchungen} Buchung(en) noch nicht im Lager</span></>}
              </div>
            )}
            {s?.letzter_fehler && <div className="text-red-400">Fehler: {s.letzter_fehler}</div>}
          </div>
          {darfAendern && (
            <Button onClick={() => pruefen.mutate()} disabled={pruefen.isPending || !s?.eingerichtet}>
              {pruefen.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
              Jetzt prüfen
            </Button>
          )}
        </CardContent>
      </Card>

      {wartend.length > 0 && (
        <Card>
          <CardHeader><h2 className="text-white font-semibold">Wartet auf deine Freigabe ({wartend.length})</h2></CardHeader>
          <CardContent className="space-y-3">
            {wartend.map(j => (
              <div key={j.id} className="flex flex-col md:flex-row md:items-center gap-3 justify-between p-3 rounded-lg bg-bambu-dark">
                <JobBeschreibung job={j} />
                {darfFreigeben && (
                  <div className="flex gap-2 shrink-0">
                    <Button size="sm" onClick={() => freigeben.mutate(j.id)} disabled={freigeben.isPending}>
                      <Play className="w-4 h-4" /> Freigeben
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => verwerfen.mutate(j.id)} disabled={verwerfen.isPending}>
                      <X className="w-4 h-4" /> Verwerfen
                    </Button>
                  </div>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader><h2 className="text-white font-semibold">Bestand der Teile mit Regel</h2></CardHeader>
        <CardContent className="overflow-x-auto">
          {!s?.uebersicht.length ? (
            <p className="text-sm text-bambu-gray">
              Noch keine Daten - lege unter „Regeln je Teil“ fest, welche Teile automatisch nachgedruckt werden, und klicke „Jetzt prüfen“.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-bambu-gray text-left">
                <tr>
                  <th className="py-2 pr-4">Teil</th>
                  <th className="py-2 pr-4 text-right">Bestand</th>
                  <th className="py-2 pr-4 text-right">Mindest</th>
                  <th className="py-2 pr-4 text-right">In Arbeit</th>
                  <th className="py-2 pr-4 text-right">Offene Aufträge</th>
                  <th className="py-2">Hinweis</th>
                </tr>
              </thead>
              <tbody>
                {s.uebersicht.map(z => {
                  const knapp = z.bestand !== null && z.mindestbestand !== null && z.bestand + z.in_arbeit - z.nachfrage <= z.mindestbestand;
                  return (
                    <tr key={z.regel_id} className="border-t border-bambu-dark-tertiary">
                      <td className="py-2 pr-4 text-white">{z.name ?? z.part_id}</td>
                      <td className={`py-2 pr-4 text-right ${knapp ? 'text-yellow-400' : 'text-white'}`}>{zahl(z.bestand)}</td>
                      <td className="py-2 pr-4 text-right text-bambu-gray">{zahl(z.mindestbestand)}</td>
                      <td className="py-2 pr-4 text-right text-blue-400">{zahl(z.in_arbeit)}</td>
                      <td className="py-2 pr-4 text-right text-bambu-gray">{zahl(z.nachfrage)}</td>
                      <td className="py-2 text-bambu-gray">{z.hinweis ?? ''}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><h2 className="text-white font-semibold">Letzte Druckaufträge</h2></CardHeader>
        <CardContent className="space-y-2">
          {!uebrige.length && <p className="text-sm text-bambu-gray">Noch keine Druckaufträge angelegt.</p>}
          {uebrige.slice(0, 30).map(j => (
            <div key={j.id} className="p-3 rounded-lg bg-bambu-dark"><JobBeschreibung job={j} /></div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

function JobBeschreibung({ job }: { job: Job }) {
  const st = JOB_TEXT[job.status] ?? { text: job.status, farbe: 'text-white' };
  return (
    <div className="text-sm space-y-1 min-w-0">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-white font-medium">{job.stueck}× {job.part_name ?? job.part_id}</span>
        {job.dringlichkeit && (
          <span className={`px-2 py-0.5 rounded text-xs ${DRINGLICHKEIT_FARBE[job.dringlichkeit] ?? ''}`}>{job.dringlichkeit}</span>
        )}
        <span className={st.farbe}>{st.text}</span>
      </div>
      {job.begruendung && <div className="text-bambu-gray">{job.begruendung}</div>}
      <div className="text-xs text-bambu-gray">
        Angelegt {zeit(job.created_at)}
        {job.freigegeben_von && <> · freigegeben von {job.freigegeben_von}</>}
        {job.finished_at && <> · beendet {zeit(job.finished_at)}</>}
        {job.queue_item_id && <> · Warteschlange #{job.queue_item_id}</>}
        {job.druckdauer_s ? <> · Druckdauer {dauer(job.druckdauer_s)}</> : null}
      </div>
      {job.geplanter_start && new Date(job.geplanter_start) > new Date() && ['geplant', 'wartet_auf_freigabe'].includes(job.status) && (
        <div className="text-xs text-blue-300">
          Nachtruhe: startet frühestens {zeit(job.geplanter_start)}, damit er nicht in der Nacht fertig wird
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
  const archive = useQuery({ queryKey: ['lager-autodruck', 'archive', suche], queryFn: () => lagerAutodruckApi.archive(suche) });
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

  const gewaehltesArchiv = archive.data?.find(a => a.id === r.archive_id);

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

        <Feld titel="Druckdatei aus dem Bambuddy-Archiv" hilfe="Diese Datei wird für das Teil gedruckt.">
          <input className={EINGABE} placeholder="Suchen …" value={suche} onChange={e => setSuche(e.target.value)} />
          <select
            className={EINGABE}
            value={r.archive_id ?? ''}
            onChange={e => {
              const id = e.target.value ? Number(e.target.value) : null;
              const a = archive.data?.find(x => x.id === id);
              setR(alt => ({
                ...alt,
                archive_id: id,
                dateiname: !alt.dateiname && a ? a.name : alt.dateiname,
                target_model: alt.target_model ?? a?.modell ?? null,
              }));
            }}
          >
            <option value="">– Datei wählen –</option>
            {r.archive_id && !gewaehltesArchiv && <option value={r.archive_id}>Archiv #{r.archive_id}</option>}
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
            <input type="number" min={1} className={EINGABE} value={r.stueck_je_druck} onChange={e => setze('stueck_je_druck', Math.max(1, Number(e.target.value) || 1))} />
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
          <Feld titel="Höchstens Drucke pro Tag" hilfe="Leer = kein Limit. Wann gestartet wird, regelt die Nachtruhe in den Einstellungen.">
            <input type="number" min={0} className={EINGABE} value={r.max_drucke_pro_tag ?? ''} onChange={e => setze('max_drucke_pro_tag', e.target.value === '' ? null : Math.max(0, Number(e.target.value)))} />
          </Feld>
          {gewaehltesArchiv?.druckzeit_s ? (
            <div className="text-sm text-bambu-gray self-center">Druckdauer laut Datei: {dauer(gewaehltesArchiv.druckzeit_s)}</div>
          ) : null}
        </div>

        <div className="flex gap-2 justify-end">
          <Button variant="secondary" onClick={onFertig}>Abbrechen</Button>
          <Button onClick={() => speichern.mutate()} disabled={speichern.isPending || !r.part_id || !r.archive_id}>
            {speichern.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            Speichern
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// --- Buchungen -------------------------------------------------------------------

const BUCHUNG_OK = ['gebucht', 'gebucht_fehldruck', 'schon_gebucht'];

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
                  <td className={`py-2 pr-4 ${b.status === 'fertig' ? 'text-bambu-green' : 'text-red-400'}`}>
                    {b.status === 'fertig' ? 'Fertig' : 'Fehldruck'}{b.gramm ? ` · ${Math.round(b.gramm)} g` : ''}
                  </td>
                  <td className="py-2 pr-4">
                    {b.zustand === 'gesendet' ? (
                      <span className={BUCHUNG_OK.includes(b.ergebnis ?? '') ? 'text-bambu-green' : 'text-yellow-400'}>{b.ergebnis}</span>
                    ) : (
                      <span className="text-yellow-400" title={b.fehler ?? ''}>
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
            <input type="number" min={1} className={EINGABE} value={k.intervall_minuten} disabled={!darfAendern} onChange={e => setze('intervall_minuten', Math.max(1, Number(e.target.value) || 1))} />
          </Feld>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="text-white font-semibold">Nachtruhe</h2>
          <p className="text-xs text-bambu-gray mt-1">
            Automatische Drucke werden so gestartet, dass sie vor dem Schlafengehen oder nach dem Aufstehen fertig sind – nie mitten in der Nacht.
            Würde ein Druck in der Nacht fertig, startet er später, sodass er zur Aufstehzeit fertig ist. Die Druckdauer kommt aus der Druckdatei.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div className="text-white font-medium">Nachtruhe beachten</div>
            <Toggle checked={k.nachtruhe_aktiv} onChange={v => setze('nachtruhe_aktiv', v)} disabled={!darfAendern} />
          </div>
          <div className="grid md:grid-cols-3 gap-4">
            <Feld titel="Schlafen gehen">
              <input type="time" className={EINGABE} value={k.schlafen} disabled={!darfAendern || !k.nachtruhe_aktiv} onChange={e => setze('schlafen', e.target.value)} />
            </Feld>
            <Feld titel="Aufstehen">
              <input type="time" className={EINGABE} value={k.aufstehen} disabled={!darfAendern || !k.nachtruhe_aktiv} onChange={e => setze('aufstehen', e.target.value)} />
            </Feld>
            <Feld titel="Puffer (Minuten)" hilfe="Für Aufheizen und falls der Druck länger dauert als geschätzt">
              <input type="number" min={0} className={EINGABE} value={k.puffer_minuten} disabled={!darfAendern || !k.nachtruhe_aktiv} onChange={e => setze('puffer_minuten', Math.max(0, Number(e.target.value) || 0))} />
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
          Nutzt die Kanäle aus Bambuddy (ntfy, Telegram, E-Mail …), die du unter Einstellungen → Benachrichtigungen anlegst. Ruhezeiten der Kanäle gelten auch hier.
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
        </div>
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
