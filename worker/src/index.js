import { Container, getContainer } from "@cloudflare/containers";

// Only secrets/variables named here reach the container (values are applied
// when the container starts). Add new env names here before deploying.
const ENV_KEYS = [
  "DATABASE_URL",
  "PUBLIC_BASE_URL",
  "JWT_SECRET",
  "ACCESS_TOKEN_EXPIRE_MINUTES",
  "REFRESH_TOKEN_EXPIRE_DAYS",
  "CORS_ORIGINS",
  "GOOGLE_CLIENT_ID",
  "GOOGLE_CLIENT_SECRET",
  "GOOGLE_REDIRECT_URI",
  "FRONTEND_URL",
  "RESET_TOKEN_EXPIRE_MINUTES",
  "FRONTEND_RESET_URL",
  "EXPOSE_RESET_TOKEN",
  "ENABLE_DOCS",
  "SMTP_HOST",
  "SMTP_PORT",
  "SMTP_USER",
  "SMTP_PASSWORD",
  "SMTP_FROM",
  "SMTP_USE_TLS",
  "SUPER_ADMIN_EMAIL",
  "SUPER_ADMIN_PASSWORD",
  "SUPER_ADMIN_NAME",
  "R2_ACCOUNT_ID",
  "R2_ACCESS_KEY_ID",
  "R2_SECRET_ACCESS_KEY",
  "R2_BUCKET_NAME",
  "RAZORPAY_KEY_ID",
  "RAZORPAY_KEY_SECRET",
  "RAZORPAY_WEBHOOK_SECRET",
  "FIELD_ENCRYPTION_KEY",
  "PUSH_NOTIFICATIONS_ENABLED",
  "FIREBASE_PROJECT_ID",
  "FIREBASE_SERVICE_ACCOUNT_JSON",
  // Phone OTP login (Twilio Verify) + real client IP for rate limiting
  "TWILIO_ACCOUNT_SID",
  "TWILIO_AUTH_TOKEN",
  "TWILIO_VERIFY_SERVICE_SID",
  "CLIENT_IP_HEADER",
  // Email delivery: smtp (default) or an HTTPS provider (resend / brevo)
  "EMAIL_PROVIDER",
  "EMAIL_API_KEY",
  "EMAIL_FROM",
  // Performance / diagnostics (all optional, backend has safe defaults)
  "LOG_LEVEL",
  "PERF_LOG",
  "RUN_STARTUP_MIGRATIONS",
  "DB_POOL_PRE_PING",
  "DB_IDLE_PING_SECONDS",
  "DB_POOL_SIZE",
  "DB_MAX_OVERFLOW",
  "DB_POOL_RECYCLE",
  // Follow-up reminders
  "FOLLOW_UP_REMINDERS_ENABLED",
  "FOLLOW_UP_REMINDER_INTERVAL_SECONDS",
  "FOLLOW_UP_DUE_LEAD_MINUTES",
  "FOLLOW_UP_OVERDUE_AFTER_MINUTES",
  "FOLLOW_UP_OVERDUE_LOOKBACK_DAYS",
  // Release tag + request time limit
  "BUILD_TAG",
  "READ_REQUEST_TIMEOUT_SECONDS"
];

export class CrmSaasBackend extends Container {
  defaultPort = 8000;      // uvicorn port (Dockerfile)
  sleepAfter = "30m";      // idle time before the container sleeps
  pingEndpoint = "health"; // FastAPI GET /health

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = {};
    for (const key of ENV_KEYS) {
      if (env[key] !== undefined) this.envVars[key] = env[key];
    }
  }
}

export default {
  async fetch(request, env) {
    return getContainer(env.CRM_BACKEND).fetch(request);
  },
};
