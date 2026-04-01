module.exports = [
  {
    ignores: ["**/node_modules/**", "**/dist/**", "**/build/**"],
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "module",
      globals: {
        window: "readonly",
        document: "readonly",
        process: "readonly",
        module: "readonly",
        require: "readonly",
      },
    },
    extends: ["eslint:recommended"],
    rules: {
      "no-unused-vars": "warn",
      "no-console": "off",
    },
  },
];
