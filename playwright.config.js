const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/e2e",
  timeout: 30000,
  fullyParallel: false,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:18766",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: ".venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18766",
    url: "http://127.0.0.1:18766/health",
    reuseExistingServer: true,
    timeout: 60000,
  },
});
