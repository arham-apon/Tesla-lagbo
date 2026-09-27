import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const INTERACTIVE = 'button, a[href], [role="radio"], [role="switch"], [role="tab"], [role="combobox"]';

async function openDriver(page: Page) {
  await page.getByRole('tab', { name: /driver/i }).click();
  await expect(page.getByRole('heading', { name: /Next Stop: Banani Road 11/ })).toBeVisible();
}

/** Every visible control's bounding box, measured the way the browser lays it out. */
async function undersizedTargets(page: Page) {
  return page.$$eval(INTERACTIVE, (els) =>
    els
      .filter((e) => (e as HTMLElement).offsetParent !== null && !e.closest('.sr-only'))
      .map((e) => {
        const r = e.getBoundingClientRect();
        return { name: (e.getAttribute('aria-label') ?? e.textContent ?? '').trim().slice(0, 40), w: r.width, h: r.height };
      })
      .filter((t) => t.w < 48 || t.h < 48),
  );
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /Bullet cabin/ })).toBeVisible();
});

test('binds the interface to the PRD cast', async ({ page }) => {
  for (const name of ['Nusrat', 'Rafiq', 'Shirin']) await expect(page.getByRole('radio', { name: new RegExp(name) })).toBeVisible();
  await expect(page.getByText('Operator: Jashim', { exact: false })).toBeVisible();
  await expect(page.getByText(/User 1|Driver 1/)).toHaveCount(0);
});

test('every control meets the 48 × 48 touch target, on both consoles', async ({ page }) => {
  expect(await undersizedTargets(page)).toEqual([]);
  await openDriver(page);
  expect(await undersizedTargets(page)).toEqual([]);
});

test('no horizontal overflow', async ({ page }) => {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test('axe-core finds no WCAG 2.1 AA violations', async ({ page }) => {
  const scan = () => new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  expect((await scan()).violations).toEqual([]);
  await openDriver(page);
  expect((await scan()).violations).toEqual([]);
});

test('Shirin loses the final seat gracefully and keeps her route', async ({ page }) => {
  await page.getByRole('switch', { name: 'Simulate Shirin Concurrent Booking' }).click();
  await page.getByRole('button', { name: 'Reserve Seat 3' }).click();

  const alert = page.getByRole('alert').filter({ hasText: 'Lock conflict handled' });
  await expect(alert).toContainText('Seat 3 was claimed by another commuter 340ms ago. Searching for the next available pooled Tesla...');
  await expect(page.getByRole('group', { name: /Seat 3, assigned, Nusrat/ })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Pickup' })).toContainText('Banani');
  await expect(page.getByRole('combobox', { name: 'Drop-off' })).toContainText('Gulshan 2');
  await expect(page.getByRole('heading', { name: 'Finding your pooled Tesla' })).toBeVisible();
});

test("shows Rafiq's verified fare: ৳30.00 + ৳45.00 − 30% = ৳52.50 (5,250 poysha)", async ({ page }) => {
  await page.getByRole('radio', { name: /Rafiq/ }).click();
  const readout = page.getByLabel('52 taka 50 poysha');
  await expect(readout).toHaveText('৳52.50');
  expect(await readout.evaluate((e) => getComputedStyle(e).fontVariantNumeric)).toContain('tabular-nums');

  await page.getByRole('button', { name: 'View Verified Fare Breakdown' }).click();
  const table = page.getByRole('table');
  for (const text of ['৳30.00', '3,000 poysha', '৳45.00', '৳75.00', '−৳22.50', '5,250 poysha']) await expect(table).toContainText(text);
  await expect(table).toContainText('Dhaka Tesla Pool Discount (30%)');
});

test('Jashim runs the sequential pipeline to completion', async ({ page }) => {
  await openDriver(page);
  const stops: [string, string][] = [
    ['Banani Road 11', 'Nusrat'],
    ['Banani Star Kabab', 'Rafiq'],
  ];
  for (const [landmark] of stops) {
    await expect(page.getByRole('heading', { name: `Next Stop: ${landmark}` })).toBeVisible();
    await page.getByRole('button', { name: 'Confirm Arrival' }).click();
    const slide = page.getByRole('button', { name: /Confirm Passenger Boarding/ });
    await slide.focus();
    await page.keyboard.press('Enter');
  }
  await expect(page.getByRole('heading', { name: 'Next Stop: Gulshan 1 Circle' })).toBeVisible();
  await page.getByRole('button', { name: 'Complete Stop' }).click();
  await expect(page.getByRole('heading', { name: 'Next Stop: Mohakhali Flyover' })).toBeVisible();
  await page.getByRole('button', { name: 'Complete Ride' }).click();
  await expect(page.getByRole('heading', { name: 'Waiting for ride requests' })).toBeVisible();
  await expect(page.getByRole('tabpanel', { name: 'Ledger' })).toContainText('TP-4101');
});
