
const ALLOWED_LOCAL_ORIGINS = new Set([
  "http://localhost:3000",
  "http://127.0.0.1:3000",
]);

function corsHeaders(origin, env) {
  const allowed = new Set(ALLOWED_LOCAL_ORIGINS);

  if (env.FRONTEND_ORIGIN) {
    allowed.add(env.FRONTEND_ORIGIN);
  }

  const headers = new Headers();

  if (origin && allowed.has(origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Vary", "Origin");
    headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    headers.set(
      "Access-Control-Allow-Headers",
      "Content-Type, X-API-Key"
    );
    headers.set("Access-Control-Max-Age", "86400");
  }

  return { headers, allowed };
}

function isAllowedPath(path) {
  return (
    path === "/api/health" ||
    path === "/api/generate" ||
    path === "/api/analyze-audio" ||
    /^\/api\/generate\/[a-fA-F0-9-]+$/.test(path) ||
    /^\/api\/generate\/[a-fA-F0-9-]+\/video$/.test(path) ||
    /^\/api\/recover\/[a-fA-F0-9-]+$/.test(path)
  );
}

async function applyRateLimit(request, env, path) {
  const clientIp =
    request.headers.get("CF-Connecting-IP") || "unknown";

  const key = `${path}:${clientIp}`;

  return env.GENERATION_LIMITER.limit({ key });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    const { headers: cors, allowed } = corsHeaders(origin, env);

    if (origin && !allowed.has(origin)) {
      return new Response("Origin not allowed", {
        status: 403,
        headers: cors,
      });
    }

    if (!isAllowedPath(url.pathname)) {
      return new Response("Not found", {
        status: 404,
        headers: cors,
      });
    }

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: cors,
      });
    }

    if (!["GET", "POST"].includes(request.method)) {
      return new Response("Method not allowed", {
        status: 405,
        headers: cors,
      });
    }

    if (!env.BACKEND_URL || !env.FLOAT_API_KEY) {
      return new Response("Gateway configuration incomplete", {
        status: 503,
        headers: cors,
      });
    }

    // Rate-limit expensive generation and audio-analysis requests.
    // Status polling, recovery and video streaming remain unrestricted.
    const needsRateLimit =
      request.method === "POST" &&
      (
        url.pathname === "/api/generate" ||
        url.pathname === "/api/analyze-audio"
      );

    if (needsRateLimit) {
      if (!env.GENERATION_LIMITER) {
        return new Response("Rate limiter binding is missing", {
          status: 503,
          headers: cors,
        });
      }

      try {
        const limitResult = await applyRateLimit(
          request,
          env,
          url.pathname
        );

        if (!limitResult.success) {
          const headers = new Headers(cors);

          headers.set("Retry-After", "60");
          headers.set("Cache-Control", "no-store");
          headers.set(
            "Content-Type",
            "application/json; charset=utf-8"
          );

          return new Response(
            JSON.stringify({
              detail:
                "Too many requests. Please wait up to 60 seconds before trying again.",
            }),
            {
              status: 429,
              headers,
            }
          );
        }
      } catch (error) {
        console.error("Rate limiter error:", error);

        return new Response("Rate limiter unavailable", {
          status: 503,
          headers: cors,
        });
      }
    }

    try {
      const baseUrl = env.BACKEND_URL.endsWith("/")
        ? env.BACKEND_URL
        : `${env.BACKEND_URL}/`;

      const backendUrl = new URL(
        url.pathname + url.search,
        baseUrl
      );

      const backendHeaders = new Headers(request.headers);

      backendHeaders.delete("Host");
      backendHeaders.delete("Content-Length");
      backendHeaders.delete("CF-Connecting-IP");

      // Inject the secret only on the server side.
      backendHeaders.set("X-API-Key", env.FLOAT_API_KEY);

      const backendRequest = new Request(
        backendUrl.toString(),
        {
          method: request.method,
          headers: backendHeaders,
          body: request.method === "GET"
            ? null
            : request.body,
          redirect: "manual",
        }
      );

      const backendResponse = await fetch(backendRequest);

      const responseHeaders = new Headers(
        backendResponse.headers
      );

      cors.forEach((value, key) => {
        responseHeaders.set(key, value);
      });

      responseHeaders.set("Cache-Control", "no-store");
      responseHeaders.delete("Set-Cookie");

      return new Response(backendResponse.body, {
        status: backendResponse.status,
        statusText: backendResponse.statusText,
        headers: responseHeaders,
      });
    } catch (error) {
      console.error("Gateway error:", error);

      return new Response("Backend unavailable", {
        status: 502,
        headers: cors,
      });
    }
  },
};
