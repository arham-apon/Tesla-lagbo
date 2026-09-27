/** Read models: turn the dispatch world into the guideline's contract types for the views. */
import { BULLET, PASSENGERS } from '@/domain/cast';
import type { BulletVehicleTelemetry, CommuterPassenger, PassengerName, RideLifecycleStatus, VehicleSeatSlot } from '@/types/mobility';

import { currentRide, poolMembers, SEAT_INDICES, type Ride, type World } from './world';

export function passengerStatus(w: World, p: PassengerName): RideLifecycleStatus {
  return currentRide(w, p)?.status ?? 'IDLE';
}

export function toCommuter(ride: Ride): CommuterPassenger {
  return {
    id: PASSENGERS[ride.passenger].id,
    name: ride.passenger,
    pickupZone: ride.pickupZone,
    dropoffZone: ride.dropoffZone,
    farePoysha: ride.fare.netPoysha,
    status: ride.status,
  };
}

export function seatSlots(w: World): readonly [VehicleSeatSlot, VehicleSeatSlot, VehicleSeatSlot] {
  const slot = (i: 1 | 2 | 3): VehicleSeatSlot => {
    const s = w.seats[i];
    const ride = s.rideId ? w.rides[s.rideId] : undefined;
    return { seatIndex: i, isOccupied: !!ride, passenger: ride ? toCommuter(ride) : null, reservationLockTime: s.lockedAt };
  };
  return [slot(1), slot(2), slot(3)];
}

export function telemetry(w: World): BulletVehicleTelemetry {
  return {
    vehicleIdentifier: BULLET.vehicleIdentifier,
    driverName: BULLET.driverName,
    totalSeatCapacity: BULLET.totalSeatCapacity,
    isOnline: w.vehicle.online,
    batteryLevelPercent: w.vehicle.batteryPercent,
    activeSeats: seatSlots(w),
    currentZone: w.vehicle.currentZone,
  };
}

/** Seats with a confirmed rider on board or matched (holds for pending requests don't count). */
export function occupiedSeatCount(w: World): number {
  return SEAT_INDICES.filter((i) => {
    const id = w.seats[i].rideId;
    const status = id ? w.rides[id]?.status : undefined;
    return status === 'MATCHED' || status === 'DRIVER_ARRIVED' || status === 'IN_TRANSIT';
  }).length;
}

export function seatRide(w: World, seat: 1 | 2 | 3): Ride | null {
  const id = w.seats[seat].rideId;
  return id ? (w.rides[id] ?? null) : null;
}

export function earningsTotal(w: World): number {
  return w.ledger.reduce((acc, e) => acc + e.netPoysha, 0);
}

export function manifest(w: World): Ride[] {
  return poolMembers(w).filter((r) => r.status !== 'COMPLETED');
}

/** Seats nobody holds, in order. */
export function openSeats(w: World): (1 | 2 | 3)[] {
  return SEAT_INDICES.filter((i) => !w.seats[i].rideId);
}
