import type { DhakaZone, PassengerName } from '@/types/mobility';

/**
 * The PRD story cast. Ids and phone numbers match the identity service seed
 * (services/identity/app/seed.py) so a later live-API adapter lines up with the same people.
 */

export const BULLET = {
  vehicleIdentifier: 'Bullet',
  driverName: 'Jashim',
  totalSeatCapacity: 3,
  plate: 'DHAKA-TESLA-11',
  kind: '3-SEATER EV',
  description: 'Three-wheeled battery-electric pool vehicle',
} as const;

export const JASHIM_ID = '11111111-1111-4111-8111-111111111111';

export interface PassengerProfile {
  readonly id: string;
  readonly name: PassengerName;
  readonly role: string;
  readonly pickupZone: DhakaZone;
  readonly dropoffZone: DhakaZone;
  readonly pickupLandmark: string;
  readonly dropoffLandmark: string;
}

export const PASSENGERS: Readonly<Record<PassengerName, PassengerProfile>> = {
  Nusrat: {
    id: '22222222-2222-4222-8222-222222222222',
    name: 'Nusrat',
    role: 'Lead commuter',
    pickupZone: 'Banani',
    dropoffZone: 'Mohakhali',
    pickupLandmark: 'Banani Road 11',
    dropoffLandmark: 'Mohakhali Flyover',
  },
  Rafiq: {
    id: '33333333-3333-4333-8333-333333333333',
    name: 'Rafiq',
    role: 'Pooled co-passenger',
    pickupZone: 'Banani',
    dropoffZone: 'Gulshan 1',
    pickupLandmark: 'Banani Star Kabab',
    dropoffLandmark: 'Gulshan 1 Circle',
  },
  Shirin: {
    id: '44444444-4444-4444-8444-444444444444',
    name: 'Shirin',
    role: 'Final-seat contender',
    pickupZone: 'Banani',
    dropoffZone: 'Gulshan 2',
    pickupLandmark: 'Banani Kamal Ataturk Avenue',
    dropoffLandmark: 'Gulshan 2 Circle',
  },
};

export const PASSENGER_NAMES = ['Nusrat', 'Rafiq', 'Shirin'] as const satisfies readonly PassengerName[];
