"""CLI e servidor do robô Sitrax."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"Data inválida: {value} (use AAAA-MM-DD ou DD/MM/AAAA)")


def cmd_report(args: argparse.Namespace) -> None:
    from app.bot.sitrax import SitraxBot

    d_ini = parse_date(args.data_ini) or date.today()
    d_fim = parse_date(args.data_fim) or d_ini
    headless = not args.show_browser

    with SitraxBot(headless=headless) as bot:
        bot.login()
        if args.todos:
            text = bot.report_all_vehicles(data_ini=d_ini, data_fim=d_fim)
        else:
            if not args.placa:
                raise SystemExit("Informe --placa ABC1D23 ou use --todos")
            text, _ = bot.report_for_plate(args.placa, d_ini, d_fim)

    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n[salvo em {args.out}]", file=sys.stderr)


def cmd_report_pdf(args: argparse.Namespace) -> None:
    """Gera resumo por cidade a partir do PDF baixado do Sitrax (nuvem de download)."""
    from app.bot.pdf_parser import positions_from_pdf
    from app.bot.report import build_narrative_report

    path = Path(args.pdf)
    if not path.exists():
        raise SystemExit(f"PDF não encontrado: {path}")

    placa, positions = positions_from_pdf(path)
    if args.placa:
        placa = args.placa.upper().strip()
    data_ref = args.data or date.today().strftime("%d/%m/%Y")
    text = build_narrative_report(placa, positions, data_ref=data_ref)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n[salvo em {args.out}]", file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn
    from app.config import settings

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port or settings.port,
        reload=args.reload,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Robô de relatórios Sitrax / Recipe Tracker"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rep = sub.add_parser("report", help="Gera relatório via terminal (automação)")
    p_rep.add_argument("--placa", help="Placa do veículo, ex: PCE7B03")
    p_rep.add_argument("--todos", action="store_true", help="Todos os veículos")
    p_rep.add_argument("--data-ini", dest="data_ini", help="Data início")
    p_rep.add_argument("--data-fim", dest="data_fim", help="Data fim")
    p_rep.add_argument(
        "--show-browser",
        action="store_true",
        help="Mostra o Chrome (útil para depurar)",
    )
    p_rep.add_argument("--out", help="Salvar relatório em arquivo .txt")
    p_rep.set_defaults(func=cmd_report)

    p_pdf = sub.add_parser(
        "report-pdf",
        help="Gera resumo (cidade/horário) a partir do PDF baixado do Sitrax",
    )
    p_pdf.add_argument("pdf", help="Caminho do PDF HistoricoPosicoes_....pdf")
    p_pdf.add_argument("--placa", help="Forçar placa no título")
    p_pdf.add_argument("--data", help="Data do relatório, ex: 10/07/2026")
    p_pdf.add_argument("--out", help="Salvar resumo em .txt")
    p_pdf.set_defaults(func=cmd_report_pdf)

    p_srv = sub.add_parser("serve", help="Sobe o site (interface web)")
    p_srv.add_argument("--host", default="0.0.0.0")
    p_srv.add_argument("--port", type=int, default=0)
    p_srv.add_argument("--reload", action="store_true")
    p_srv.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
