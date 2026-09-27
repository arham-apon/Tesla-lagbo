import type { DhakaZone } from '@/types/mobility';

export interface ZoneInfo {
  readonly name: DhakaZone;
  /** The backend's zone code (matching service), e.g. GULSHAN_1. */
  readonly code: string;
  readonly lat: number;
  readonly lng: number;
  /** The landmark drivers navigate to when nobody named a closer one. */
  readonly landmark: string;
}

// Coordinates and codes match services/matching/migrations/versions/0002_seed_zones.py.
export const ZONES: readonly ZoneInfo[] = [
  { name: 'Banani', code: 'BANANI', lat: 23.7937, lng: 90.4066, landmark: 'Banani Road 11' },
  { name: 'Gulshan 1', code: 'GULSHAN_1', lat: 23.7806, lng: 90.4163, landmark: 'Gulshan 1 Circle' },
  { name: 'Gulshan 2', code: 'GULSHAN_2', lat: 23.7925, lng: 90.4144, landmark: 'Gulshan 2 Circle' },
  { name: 'Mohakhali', code: 'MOHAKHALI', lat: 23.778, lng: 90.405, landmark: 'Mohakhali Flyover' },
  { name: 'Farmgate', code: 'FARMGATE', lat: 23.758, lng: 90.3897, landmark: 'Farmgate Khamarbari' },
  { name: 'Dhanmondi', code: 'DHANMONDI', lat: 23.7465, lng: 90.376, landmark: 'Dhanmondi 27' },
  { name: 'Mirpur', code: 'MIRPUR', lat: 23.8069, lng: 90.3687, landmark: 'Mirpur 10 Circle' },
  { name: 'Uttara', code: 'UTTARA', lat: 23.8759, lng: 90.3795, landmark: 'Uttara House Building' },
  { name: 'Bashundhara', code: 'BASHUNDHARA', lat: 23.8193, lng: 90.4526, landmark: 'Bashundhara Gate' },
];

const BY_NAME = new Map(ZONES.map((z) => [z.name, z]));

export function zoneInfo(name: DhakaZone): ZoneInfo {
  const z = BY_NAME.get(name);
  if (!z) throw new Error(`Unknown zone ${name}`);
  return z;
}

// Hand-set road distances (both directions), identical to the matching service's overrides.
const OVERRIDES = new Map<string, number>([
  ['Banani|Mohakhali', 3500],
  ['Banani|Gulshan 1', 2000],
  ['Gulshan 1|Mohakhali', 2000],
]);

const ROAD_FACTOR = 1.3;

function haversineM(a: ZoneInfo, b: ZoneInfo): number {
  const r = 6_371_000;
  const rad = Math.PI / 180;
  const dp = (b.lat - a.lat) * rad;
  const dl = (b.lng - a.lng) * rad;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(h));
}

/** Road distance in whole metres — same rule as the backend's DistanceTable.get. */
export function roadDistanceM(from: DhakaZone, to: DhakaZone): number {
  if (from === to) return 0;
  const o = OVERRIDES.get(`${from}|${to}`) ?? OVERRIDES.get(`${to}|${from}`);
  if (o !== undefined) return o;
  return Math.round((haversineM(zoneInfo(from), zoneInfo(to)) * ROAD_FACTOR) / 100) * 100;
}

export function formatKm(metres: number): string {
  return `${Math.trunc(metres / 1000)}.${Math.trunc((metres % 1000) / 100)} km`;
}
