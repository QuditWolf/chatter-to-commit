/**
 * PM2 ecosystem config for Fleet Tracker production deployment.
 *
 * Usage:
 *   pm2 start ecosystem.config.js
 *   pm2 save
 *   pm2 startup  (to auto-start on boot)
 *
 * Processes:
 *   fleet-api       — FastAPI backend (Python)
 *   fleet-wa        — WhatsApp Baileys listener (Node.js)
 */

module.exports = {
  apps: [
    {
      name: "fleet-api",
      script: "uvicorn",
      args: "fleet_pipeline.api.main:app --host 0.0.0.0 --port 8000",
      interpreter: "python3",
      cwd: __dirname,
      env: {
        FLEET_API_HOST: "0.0.0.0",
        FLEET_API_PORT: "8000",
      },
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      out_file: `${__dirname}/logs/fleet-api.log`,
      error_file: `${__dirname}/logs/fleet-api-err.log`,
    },
    {
      name: "fleet-wa",
      script: "index.js",
      cwd: `${__dirname}/fleet_pipeline/wa_listener`,
      interpreter: "node",
      env_file: `${__dirname}/fleet_pipeline/wa_listener/.env`,
      watch: false,
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
      exponential_backoff_restart_delay: 100,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      out_file: `${__dirname}/logs/fleet-wa.log`,
      error_file: `${__dirname}/logs/fleet-wa-err.log`,
    },
  ],
};
