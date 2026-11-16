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

// ---------------------------------------------------------------------------
// Container errors -> clean 503 with CORS headers, and one retry for safe requests
//
// When the container is starting, restarting or overloaded, the containers library does not throw:
// it RETURNS a plain-text 500 ("Error proxying request to container...", "Container suddenly
// disconnected...", "Failed to start container...") or a 503 ("There is no Container instance...").
// Those responses carry no CORS headers, so the browser shows a CORS error instead of the real
// problem. Here we (1) retry GET/HEAD/OPTIONS once, and (2) otherwise answer with a JSON 503
// that has CORS headers, so the frontend can show "server busy" and retry.
// Real answers from the app itself (including its own 4xx/5xx JSON errors) pass through untouched.
// ---------------------------------------------------------------------------
const CONTAINER_ERROR_PREFIXES = [
  "Error proxying request to container",
  "Container suddenly disconnected",
  "Failed to start container",
  "There is no Container instance available",
];
const RETRYABLE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const LOCAL_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
const RETRY_DELAY_MS = 1500;

function originAllowed(origin, env) {
  if (!origin) return false;
  if (LOCAL_ORIGIN.test(origin)) return true;
  const list = String(env.CORS_ORIGINS || "https://crm-saas.asynk.in")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return list.includes(origin) || list.includes("*");
}

async function isContainerError(res) {
  if (res.status !== 500 && res.status !== 503) return false;
  const type = res.headers.get("content-type") || "";
  if (!type.startsWith("text/plain")) return false;
  try {
    const text = await res.clone().text();
    return CONTAINER_ERROR_PREFIXES.some((p) => text.startsWith(p));
  } catch {
    return false;
  }
}

function busyResponse(request, env) {
  const headers = new Headers({
    "Content-Type": "application/json",
    "Retry-After": "5",
    "Cache-Control": "no-store",
    Vary: "Origin",
  });
  const origin = request.headers.get("Origin");
  if (originAllowed(origin, env)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Credentials", "true");
  }
  const body = { detail: "Server is busy or restarting. Please retry in a few seconds.", code: "container_unavailable" };
  return new Response(JSON.stringify(body), { status: 503, headers });
}

async function proxyToContainer(request, env) {
  const container = getContainer(env.CRM_BACKEND);
  const canRetry = RETRYABLE_METHODS.has(request.method);
  const spare = canRetry ? request.clone() : null;
  const path = new URL(request.url).pathname;

  let res = null;
  try {
    res = await container.fetch(request);
  } catch (e) {
    console.log("container-error: fetch threw", request.method, path, String(e));
  }
  if (res && !(await isContainerError(res))) return res;
  if (res) console.log("container-error: library response", res.status, request.method, path);

  if (canRetry) {
    await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
    try {
      const retry = await container.fetch(spare);
      if (!(await isContainerError(retry))) {
        console.log("container-error: retry ok", request.method, path);
        return retry;
      }
    } catch (e) {
      console.log("container-error: retry threw", request.method, path, String(e));
    }
  }

  console.log("container-error: returning 503", request.method, path);
  return busyResponse(request, env);
}

export default {
  async fetch(request, env) {
    return proxyToContainer(request, env);
  },

  // Keep-warm: container idle sleep mein na jaye, taaki pehli request fail na ho.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      getContainer(env.CRM_BACKEND)
        .fetch(new Request("https://keepwarm.internal/health"))
        .then((r) => console.log("keep-warm", r.status))
        .catch((e) => console.log("keep-warm failed", String(e)))
    );
  },
};