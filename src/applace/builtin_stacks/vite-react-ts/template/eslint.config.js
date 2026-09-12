import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    rules: {
      // Reported, not fatal -- and `noUnusedLocals` is off in tsconfig.json for
      // the same reason. An import left behind is untidy; it is not a broken
      // page, and measured against a mid-sized model it cost round trip after
      // round trip while the page itself was already correct. Everything that
      // decides whether the app works stays an error.
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
)
