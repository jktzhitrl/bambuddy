// Fork: Lager-Autodruck - API-Zugriff fuer /api/v1/lager-autodruck.
// Eigene Datei statt Erweiterung von client.ts, damit Updates von Bambuddy
// ohne Konflikte uebernommen werden koennen.
import { getAuthToken } from './client';

const BASIS = '/api/v1/lager-autodruck';

async function anfrage<T>(pfad: string, optionen: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = getAuthToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const antwort = await fetch(`${BASIS}${pfad}`, {
    ...optionen,
    cache: 'no-store',
    credentials: 'include',
    headers,
  });
  if (!antwort.ok) {
    const fehler = await antwort.json().catch(() => ({}));
    const detail = fehler.detail;
    const text = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail.map((d: { msg?: string }) => (d.msg ?? '').replace(/^Value error,\s*/i, '')).join('; ')
        : `Fehler ${antwort.status}`;
    throw new Error(text);
  }
  return antwort.json() as Promise<T>;
}

export type Modus = 'automatisch' | 'ki' | 'freigabe' | 'aus';

export interface Konfig {
  aktiv: boolean;
  supabase_url: string;
  supabase_anon_key: string;
  email: string;
  passwort: string;
  anthropic_api_key: string;
  ki_modell: string;
  intervall_minuten: number;
  alle_drucke_verbuchen: boolean;
  eingerichtet?: boolean;
}

export interface Regel {
  id?: number;
  part_id: string;
  part_name: string | null;
  archive_id: number | null;
  plate_id: number | null;
  dateiname: string;
  stueck_je_druck: number;
  modus: Modus;
  printer_id: number | null;
  target_model: string | null;
  target_location: string | null;
  max_drucke_pro_tag: number | null;
  zeit_von: string | null;
  zeit_bis: string | null;
  warnung?: string | null;
}

export interface UebersichtZeile {
  regel_id: number;
  part_id: string;
  name: string | null;
  bestand: number | null;
  mindestbestand: number | null;
  nachfrage: number;
  in_arbeit: number;
  hinweis: string | null;
}

export interface Status {
  aktiv: boolean;
  eingerichtet: boolean;
  ki_aktiv: boolean;
  intervall_minuten: number;
  letzter_lauf: string | null;
  letztes_ergebnis: Record<string, unknown> | null;
  letzter_fehler: string | null;
  uebersicht: UebersichtZeile[];
  offene_buchungen: number;
}

export interface Job {
  id: number;
  regel_id: number | null;
  part_id: string;
  part_name: string | null;
  queue_item_id: number | null;
  stueck: number;
  status: string;
  dringlichkeit: string | null;
  begruendung: string | null;
  freigegeben_von: string | null;
  created_at: string | null;
  finished_at: string | null;
}

export interface Buchung {
  id: number;
  job_id: number | null;
  drucker: string;
  dateiname: string;
  gramm: number | null;
  status: string;
  zustand: string;
  versuche: number;
  ergebnis: string | null;
  fehler: string | null;
  created_at: string | null;
  gesendet_at: string | null;
}

export interface LagerTeil {
  id: string;
  name: string;
  bestand: number | null;
  mindestbestand: number | null;
}

export interface ArchivAuswahl {
  id: number;
  name: string;
  filename: string;
  modell: string | null;
  gramm: number | null;
  druckzeit_s: number | null;
}

export interface DruckerAuswahl {
  id: number;
  name: string;
  modell: string | null;
  standort: string | null;
}

const json = (daten: unknown) => JSON.stringify(daten);

export const lagerAutodruckApi = {
  konfig: () => anfrage<Konfig>('/konfig'),
  konfigSpeichern: (k: Konfig) => anfrage<Konfig>('/konfig', { method: 'PUT', body: json(k) }),
  verbindungTesten: () => anfrage<{ ok: boolean; meldung: string }>('/verbindung-testen', { method: 'POST' }),
  status: () => anfrage<Status>('/status'),
  pruefen: () => anfrage<Record<string, unknown>>('/pruefen', { method: 'POST' }),
  teile: () => anfrage<LagerTeil[]>('/teile'),
  archive: (q: string) => anfrage<ArchivAuswahl[]>(`/archive?q=${encodeURIComponent(q)}`),
  drucker: () => anfrage<DruckerAuswahl[]>('/drucker'),
  regeln: () => anfrage<Regel[]>('/regeln'),
  regelSpeichern: (r: Regel) =>
    r.id
      ? anfrage<Regel>(`/regeln/${r.id}`, { method: 'PUT', body: json(r) })
      : anfrage<Regel>('/regeln', { method: 'POST', body: json(r) }),
  regelLoeschen: (id: number) => anfrage<{ ok: boolean }>(`/regeln/${id}`, { method: 'DELETE' }),
  jobs: () => anfrage<Job[]>('/jobs'),
  jobFreigeben: (id: number) => anfrage<{ ok: boolean }>(`/jobs/${id}/freigeben`, { method: 'POST' }),
  jobVerwerfen: (id: number) => anfrage<{ ok: boolean }>(`/jobs/${id}/verwerfen`, { method: 'POST' }),
  buchungen: () => anfrage<Buchung[]>('/buchungen'),
  buchungErneut: (id: number) => anfrage<{ ok: boolean }>(`/buchungen/${id}/erneut`, { method: 'POST' }),
};
