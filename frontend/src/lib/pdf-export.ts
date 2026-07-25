import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";
import type { BotStatus, Position, ScannerCandidateReport } from "@/types";
import { formatCurrency } from "./utils";

export function exportDashboardPDF({
  status,
  report,
  positions,
}: {
  status: BotStatus | null;
  report: ScannerCandidateReport | null;
  positions: Position[];
}) {
  const doc = new jsPDF();
  const date = new Date().toLocaleDateString();

  doc.setFontSize(20);
  doc.setTextColor(30, 30, 30);
  doc.text("MomentumForge — Private Paper Report", 14, 22);

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

  if (report?.candidates?.length) {
    const y = (doc as jsPDF & { lastAutoTable: { finalY: number } }).lastAutoTable
      ?.finalY ?? 90;

    doc.setFontSize(14);
    doc.text("Scanner Candidates", 14, y + 14);

    autoTable(doc, {
      startY: y + 18,
      head: [["Symbol", "Price", "Gap", "RVol", "Scanner Rank"]],
      body: report.candidates.map((candidate) => [
        candidate.symbol,
        `$${candidate.price.toFixed(2)}`,
        `${candidate.gap_pct.toFixed(1)}%`,
        `${candidate.relative_volume.toFixed(1)}x`,
        `${(candidate.scanner_score * 100).toFixed(0)}`,
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
        "LONG",
        p.shares_remaining.toString(),
        formatCurrency(p.entry_price),
        formatCurrency(p.pnl_unrealized),
      ]),
      theme: "striped",
      headStyles: { fillColor: [6, 182, 212] },
    });
  }

  doc.save(`momentumforge-report-${date.replace(/\//g, "-")}.pdf`);
}
