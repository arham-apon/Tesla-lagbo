import jsxA11y from 'eslint-plugin-jsx-a11y';
import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

import tokens from './eslint-rules/tokens.mjs';

const config = [
  ...nextVitals,
  ...nextTs,
  { ignores: ['.next/**', 'node_modules/**', 'next-env.d.ts', 'playwright-report/**', 'test-results/**'] },
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { tokens },
    rules: {
      ...jsxA11y.flatConfigs.recommended.rules,
      'tokens/no-arbitrary-tailwind': 'error',
      'tokens/no-inline-style': 'error',
      'tokens/no-raw-color': 'error',
    },
  },
  { files: ['src/design/tokens.ts'], rules: { 'tokens/no-raw-color': 'off' } },
];

export default config;
