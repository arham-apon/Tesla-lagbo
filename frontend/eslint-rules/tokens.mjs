/**
 * Token-discipline rules (guideline "Architectural Verification Matrix → Token Discipline").
 *
 *  tokens/no-arbitrary-tailwind  p-[13px], text-[#334155], [mask-type:alpha], data-[state=open]:…  → error
 *  tokens/no-inline-style        <div style={{ margin: 13 }}>                                     → error
 *  tokens/no-raw-color           '#10B981', 'rgba(…)' outside src/design/tokens.ts                → error
 */

// A class token that uses Tailwind's arbitrary syntax: a utility/variant with -[…], or an [prop:value] property.
const ARBITRARY = /(^|:)!?-?[a-z][a-z0-9-]*-\[[^\]]+\]|(^|:)\[[a-z-]+:[^\]]+\]/;
const RAW_COLOR = /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\b|\b(?:rgba?|hsla?)\(/;

function eachString(context, check) {
  const visit = (node, text) => check(node, text);
  return {
    Literal(node) {
      if (typeof node.value === 'string') visit(node, node.value);
    },
    TemplateElement(node) {
      visit(node, node.value.cooked ?? node.value.raw);
    },
  };
}

const noArbitraryTailwind = {
  meta: {
    type: 'problem',
    docs: { description: 'Disallow Tailwind arbitrary values; use design tokens from tailwind.config.ts' },
    messages: { arbitrary: '"{{token}}" is an arbitrary Tailwind value. Use a design token instead.' },
    schema: [],
  },
  create(context) {
    return eachString(context, (node, text) => {
      for (const token of text.split(/\s+/)) {
        if (ARBITRARY.test(token)) context.report({ node, messageId: 'arbitrary', data: { token } });
      }
    });
  },
};

const noInlineStyle = {
  meta: {
    type: 'problem',
    docs: { description: 'Disallow style={{…}} on DOM elements; Framer Motion components may bind motion values' },
    messages: { inline: 'Inline styles bypass the token system. Use token classes (or a motion.* component).' },
    schema: [],
  },
  create(context) {
    return {
      JSXAttribute(node) {
        if (node.name.name !== 'style') return;
        const el = node.parent.name;
        const isMotion = el.type === 'JSXMemberExpression' && el.object.name === 'motion';
        if (!isMotion) context.report({ node, messageId: 'inline' });
      },
    };
  },
};

const noRawColor = {
  meta: {
    type: 'problem',
    docs: { description: 'Colour literals live only in src/design/tokens.ts' },
    messages: { raw: 'Raw colour "{{text}}" — reference a semantic token instead.' },
    schema: [],
  },
  create(context) {
    return eachString(context, (node, text) => {
      const m = text.match(RAW_COLOR);
      if (m) context.report({ node, messageId: 'raw', data: { text: m[0] } });
    });
  },
};

const plugin = {
  meta: { name: 'tokens' },
  rules: {
    'no-arbitrary-tailwind': noArbitraryTailwind,
    'no-inline-style': noInlineStyle,
    'no-raw-color': noRawColor,
  },
};

export default plugin;
