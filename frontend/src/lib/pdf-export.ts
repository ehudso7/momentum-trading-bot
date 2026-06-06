import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";
import type { BotStatus, Position, SignalReport } from "@/types";
import { formatCurrency } from "./utils";

export function exportDashboardPDF({
  status,
  report,
  positions,
}: {
  status: BotStatus | null;
  report: SignalReport | null;
  positions: Position[];
}) {
  const doc = new jsPDF();
  const date = new Date().toLocaleDateString();

  doc.setFontSize(20);
  doc.setTextColor(30, 30, 30);
  doc.text("MomentumForge AI — Daily Report", 14, 22);

  doc.setFontSize(10);
  doc.setTextColor(100, 100, 100);
  doc.text(`Generated: ${date}`, 14, 30);

  if (status) {
    doc.setFontSize(14);
    doc.setTextColor(30, 30, 30);
    doc.text("Portfolio Summary", 14, 42);

    autoTable(doc, {
      startY: 46,
      head: [["Metric", "Value"]],
      body: [
        ["Equity", formatCurrency(status.equity)],
        ["Daily P&L", formatCurrency(status.daily_pnl)],
        ["Daily Return", `${status.daily_return_pct.toFixed(2)}%`],
        ["Open Positions", status.open_positions_count.toString()],
        ["Mode", status.run_mode.toUpperCase()],
        ["Regime", status.regime],
      ],
      theme: "striped",
      headStyles: { fillColor: [6, 182, 212] },
    });
  }

  if (report?.signals?.length) {
    const y = (doc as jsPDF & { lastAutoTable: { finalY: number } }).lastAutoTable
      ?.finalY ?? 90;

    doc.setFontSize(14);
    doc.text("Signal Scanner", 14, y + 14);

    autoTable(doc, {
      startY: y + 18,
      head: [["Symbol", "Direction", "Confidence", "Entry"]],
      body: report.signals.map((s) => [
        s.symbol,
        s.direction,
        `${(s.confidence * 100).toFixed(0)}%`,
        s.entry != null ? `$${s.entry.toFixed(2)}` : "—",
      ]),
      theme: "striped",
      headStyles: { fillColor: [6, 182, 212] },
    });
  }

  if (positions.length) {
    const y = (doc as jsPDF & { lastAutoTable: { finalY: number } }).lastAutoTable
      ?.finalY ?? 140;

    doc.setFontSize(14);
    doc.text("Open Positions", 14, y + 14);

    autoTable(doc, {
      startY: y + 18,
      head: [["Symbol", "Side", "Qty", "Entry", "P&L"]],
      body: positions.map((p) => [
        p.symbol,
        p.side,
        p.qty.toString(),
        formatCurrency(p.entry_price),
        p.unrealized_pnl != null ? formatCurrency(p.unrealized_pnl) : "—",
      ]),
      theme: "striped",
      headStyles: { fillColor: [6, 182, 212] },
    });
  }

  doc.save(`momentumforge-report-${date.replace(/\//g, "-")}.pdf`);
}