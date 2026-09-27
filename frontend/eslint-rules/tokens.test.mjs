import { RuleTester } from 'eslint';
import { describe, it } from 'vitest';

import tokens from './tokens.mjs';

RuleTester.describe = describe;
RuleTester.it = it;

const tester = new RuleTester({
  languageOptions: { ecmaVersion: 2022, sourceType: 'module', parserOptions: { ecmaFeatures: { jsx: true } } },
});

tester.run('no-arbitrary-tailwind', tokens.rules['no-arbitrary-tailwind'], {
  valid: [
    { code: '<div className="p-4 text-fg-primary md:grid-cols-2 data-open:bg-surface-elevated" />' },
    { code: 'document.querySelector("[role=radio]")' },
    { code: 'const label = "Seat 3 was claimed 340ms ago";' },
  ],
  invalid: [
    { code: '<div className="p-[13px]" />', errors: [{ messageId: 'arbitrary' }] },
    { code: '<div className="md:text-[#334155]" />', errors: [{ messageId: 'arbitrary' }] },
    { code: 'cx("flex", "[mask-type:alpha]")', errors: [{ messageId: 'arbitrary' }] },
    { code: 'const c = `gap-2 data-[state=open]:bg-mint`;', errors: [{ messageId: 'arbitrary' }] },
  ],
});

tester.run('no-inline-style', tokens.rules['no-inline-style'], {
  valid: [{ code: '<motion.div style={{ x }} />' }, { code: '<div className="m-4" />' }],
  invalid: [{ code: '<div style={{ margin: 13 }} />', errors: [{ messageId: 'inline' }] }],
});

tester.run('no-raw-color', tokens.rules['no-raw-color'], {
  valid: [{ code: 'const c = "bg-mint";' }, { code: 'const id = "#seat-3";' }],
  invalid: [
    { code: 'const c = "#10B981";', errors: [{ messageId: 'raw' }] },
    { code: 'const c = "rgba(0, 0, 0, 0.5)";', errors: [{ messageId: 'raw' }] },
  ],
});
