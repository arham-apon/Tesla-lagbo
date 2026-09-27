// Domain model and mobility contracts (guideline "Production Implementation Artifacts").
// The guideline's interfaces are kept verbatim; the extra types below are what the consoles need on top.

export type DhakaZone =
  | 'Banani'
  | 'Gulshan 1'
  | 'Gulshan 2'
  | 'Mohakhali'
  | 'Dhanmondi'
  | 'Mirpur'
  | 'Uttara'
  | 'Farmgate'
  | 'Bashundhara';

export type RideLifecycleStatus =
  | 'IDLE'
  | 'REQUESTED'
  | 'MATCHED'
  | 'DRIVER_ARRIVED'
  | 'IN_TRANSIT'
  | 'COMPLETED'
  | 'CANCELLED';

export interface CommuterPassenger {
  readonly id: string;
  readonly name: 'Nusrat' | 'Rafiq' | 'Shirin' | string;
  readonly pickupZone: DhakaZone;
  readonly dropoffZone: DhakaZone;
  readonly farePoysha: number; // Integer poysha prevents floating-point drift
  readonly status: RideLifecycleStatus;
}

export interface VehicleSeatSlot {
  readonly seatIndex: 1 | 2 | 3;
  readonly isOccupied: boolean;
  readonly passenger: CommuterPassenger | null;
  readonly reservationLockTime?: number | null;
}

export interface BulletVehicleTelemetry {
  readonly vehicleIdentifier: 'Bullet';
  readonly driverName: 'Jashim';
  readonly totalSeatCapacity: 3;
  readonly isOnline: boolean;
  readonly batteryLevelPercent: number;
  readonly activeSeats: readonly [VehicleSeatSlot, VehicleSeatSlot, VehicleSeatSlot];
  readonly currentZone: DhakaZone;
}

export interface VerifiedFareStructure {
  readonly baseFarePoysha: number;
  readonly distanceRatePoysha: number;
  readonly discountPercentage: number;
  readonly calculatedNetPoysha: number;
  readonly formattedTakaString: string;
}

// ---- additions --------------------------------------------------------------------------------------------------

export type PassengerName = 'Nusrat' | 'Rafiq' | 'Shirin';
export type SeatIndex = VehicleSeatSlot['seatIndex'];
export type Actor = 'PASSENGER' | 'DRIVER' | 'SYSTEM';
export type ConsoleView = 'passenger' | 'driver';
