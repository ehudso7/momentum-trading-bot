import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "https://momentum-trading-bot-production.up.railway.app";

const DASHBOARD_URL =
  process.env.NEXT_PUBLIC_DASHBOARD_URL || BACKEND_URL;

async function proxyRequest(
  request: NextRequest,
  params: { path: string[] },
  baseUrl: string
) {
  const path = params.path.join("/");
  const url = new URL(`/${path}`, baseUrl);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  const headers = new Headers();
  const auth = request.headers.get("authorization");
  if (auth) headers.set("Authorization", auth);
  headers.set("Content-Type", "application/json");

  const init: RequestInit = {
    method: request.method,
    headers,
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }

  const response = await fetch(url.toString(), init);
  const body = await response.text();

  return new NextResponse(body, {
    status: response.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const resolved = await params;
  const base = resolved.path[0] === "dashboard" ? DASHBOARD_URL : BACKEND_URL;
  const path = resolved.path[0] === "dashboard" ? resolved.path.slice(1) : resolved.path;
  return proxyRequest(request, { path }, base);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const resolved = await params;
  const base = resolved.path[0] === "dashboard" ? DASHBOARD_URL : BACKEND_URL;
  const path = resolved.path[0] === "dashboard" ? resolved.path.slice(1) : resolved.path;
  return proxyRequest(request, { path }, base);
}