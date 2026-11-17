// Offline Playwright configuration for static web template (INV-03, INV-13, AC-2.5)
export default {
  testDir: "./tests",
  timeout: 15000,
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    headless: true,
    offline: true,
    viewport: { width: 1280, height: 720 },
    actionTimeout: 5000
  }
};
