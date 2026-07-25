import { NextRequest, NextResponse } from "next/server";
import { authorizeOwner } from "@/lib/owner-access";
import { isPrivateMode } from "@/lib/access-policy";

const BACKEND_URL =
  process.env.TRADING_BACKEND_URL ||
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "https://momentum-trading-bot-production.up.railway.app";

const DASHBOARD_URL =
  process.env.TRADING_DASHBOARD_URL ||
  process.env.NEXT_PUBLIC_DASHBOARD_URL ||
  BACKEND_URL;

async function proxyRequest(
  request: NextRequest,
  params: { path: string[] },
  baseUrl: string,
  credential: "backend" | "dashboard"
) {
  const owner = await authorizeOwner();
  if (!owner.ok) {
    return NextResponse.json(
      { error: owner.error },
      { status: owner.status }
    );
  }

  const path = params.path.join("/");
  const url = new URL(`/${path}`, baseUrl);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  const headers = new Headers();
  const isDashboard = credential === "dashboard";
  const serverKey = isDashboard
    ? process.env.TRADING_DASHBOARD_API_KEY
    : process.env.TRADING_BACKEND_API_KEY;
  if (isPrivateMode() && !serverKey) {
    return NextResponse.json(
      { error: `${isDashboard ? "Dashboard" : "Backend"} private key is not configured.` },
      { status: 503 }
    );
  }
  if (serverKey) headers.set("Authorization", `Bearer ${serverKey}`);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  headers.set("Accept", "application/json");

  const init: RequestInit = {
    method: request.method,
    headers,
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }

  let response: Response;
  try {
    response = await fetch(url.toString(), { ...init, cache: "no-store" });
  } catch {
    return NextResponse.json(
      { error: "Private trading service is unreachable." },
      { status: 502 }
    );
  }
  const body = await response.text();

  return new NextResponse(body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-store, private",
    },
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const resolved = await params;
  const base = resolved.path[0] === "dashboard" ? DASHBOARD_URL : BACKEND_URL;
  const path = resolved.path[0] === "dashboard" ? resolved.path.slice(1) : resolved.path;
  return proxyRequest(
    request,
    { path },
    base,
    resolved.path[0] === "dashboard" ? "dashboard" : "backend"
  );
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const resolved = await params;
  const base = resolved.path[0] === "dashboard" ? DASHBOARD_URL : BACKEND_URL;
  const path = resolved.path[0] === "dashboard" ? resolved.path.slice(1) : resolved.path;
  return proxyRequest(
    request,
    { path },
    base,
    resolved.path[0] === "dashboard" ? "dashboard" : "backend"
  );
}
