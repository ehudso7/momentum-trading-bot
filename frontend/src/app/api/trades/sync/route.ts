import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export async function POST(request: NextRequest) {
  if (
    !process.env.NEXT_PUBLIC_SUPABASE_URL ||
    !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  ) {
    return NextResponse.json(
      { error: "Supabase not configured" },
      { status: 503 }
    );
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { trades } = await request.json();

  if (!Array.isArray(trades) || trades.length === 0) {
    return NextResponse.json({ synced: 0 });
  }

  const records = trades.map(
    (trade: {
      symbol: string;
      side: string;
      qty: number;
      price: number;
      pnl?: number;
      timestamp: string;
    }) => ({
      user_id: user.id,
      symbol: trade.symbol,
      side: trade.side,
      qty: trade.qty,
      price: trade.price,
      pnl: trade.pnl ?? null,
      executed_at: trade.timestamp,
    })
  );

  const { error } = await supabase.from("trades").upsert(records, {
    onConflict: "user_id,symbol,executed_at",
    ignoreDuplicates: true,
  });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ synced: records.length });
}