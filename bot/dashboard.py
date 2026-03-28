"""
Rich terminal dashboard.

Renders a live-updating layout with:
  ┌──────────────────────────────────────────────────────────────────┐
  │  HEADER: mode, kill-switch status, uptime                       │
  ├────────────┬─────────────────────────────┬───────────────────────┤
  │  PORTFOLIO │  OPEN POSITIONS             │  MARKET PRICES        │
  ├────────────┴─────────────────────────────┴───────────────────────┤
  │  LAST 10 TRADES                                                  │
  └──────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.binance_feed import BinanceFeed
from bot.kalshi_client import KalshiClient
from bot.position_manager import PortfolioManager


class Dashboard:
    REFRESH_RATE = 1.0  # seconds between redraws

    def __init__(
        self,
        portfolio: PortfolioManager,
        binance: BinanceFeed,
        kalshi: KalshiClient,
        is_paper: bool,
    ) -> None:
        self._portfolio = portfolio
        self._binance = binance
        self._kalshi = kalshi
        self._is_paper = is_paper
        self._start_time = time.monotonic()
        self._console = Console()
        self._recent_trades: list[dict] = []
        self._live: Optional[Live] = None
        self._task: Optional[asyncio.Task] = None

    def set_recent_trades(self, trades: list[dict]) -> None:
        self._recent_trades = trades

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="dashboard")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._live:
            self._live.stop()

    # ------------------------------------------------------------------
    # Internal render loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        with Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=int(1 / self.REFRESH_RATE),
            screen=True,
        ) as live:
            self._live = live
            while True:
                try:
                    await asyncio.sleep(self.REFRESH_RATE)
                    live.update(self._build_layout())
                except asyncio.CancelledError:
                    return
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Layout builders
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header_panel(), name="header", size=3),
            Layout(name="middle", ratio=1),
            Layout(self._trades_panel(), name="trades", size=14),
            Layout(self._footer(), name="footer", size=1),
        )
        layout["middle"].split_row(
            Layout(self._portfolio_panel(), name="portfolio", ratio=1),
            Layout(self._positions_panel(), name="positions", ratio=2),
            Layout(self._prices_panel(), name="prices", ratio=1),
        )
        return layout

    def _header_panel(self) -> Panel:
        mode_text = Text()
        mode_text.append("  KALSHI LATENCY ARB BOT  ", style="bold white on blue")
        if self._is_paper:
            mode_text.append("  PAPER TRADING  ", style="bold black on yellow")
        else:
            mode_text.append("  LIVE TRADING  ", style="bold white on red")

        if self._portfolio.kill_switch_active:
            mode_text.append("  ⛔ KILL SWITCH ACTIVE  ", style="bold white on red blink")
        if self._portfolio.profit_lock_active:
            mode_text.append("  🔒 PROFIT LOCKED  ", style="bold black on green")

        uptime_secs = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime_secs, 3600)
        m, s = divmod(rem, 60)
        mode_text.append(
            f"    Uptime: {h:02d}:{m:02d}:{s:02d}    ",
            style="dim white",
        )
        mode_text.append(
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            style="dim cyan",
        )
        return Panel(Align.center(mode_text), style="bold")

    def _portfolio_panel(self) -> Panel:
        pm = self._portfolio
        pnl = pm.daily_pnl
        pnl_color = "green" if pnl >= 0 else "red"
        dd = pm.daily_drawdown
        dd_color = "red" if dd >= 0.15 else "yellow" if dd >= 0.10 else "green"
        wins, total = pm._total_wins, pm._total_trades
        wr = pm.win_rate

        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", justify="right")
        table.add_column(justify="left")

        table.add_row("Balance", f"[bold white]${pm.balance:,.2f}[/]")
        table.add_row("Total Equity", f"[bold cyan]${pm.total_equity:,.2f}[/]")
        table.add_row(
            "Day P&L",
            f"[bold {pnl_color}]{'+'if pnl>=0 else ''}{pnl:+.2f}[/]",
        )
        table.add_row("Unrealised", f"[cyan]${pm.unrealised_pnl:+.4f}[/]")
        table.add_row(
            "Daily DD",
            f"[{dd_color}]{dd:.1%}[/]",
        )
        table.add_row("Win Rate", f"[white]{wr:.1%}[/]  ({wins}/{total})")
        table.add_row("Open Pos", f"[yellow]{len(pm.open_positions)}[/]")

        # Streak info
        kelly_mult = pm.kelly_multiplier
        kelly_color = "green" if kelly_mult > 1.0 else "red" if kelly_mult < 1.0 else "white"
        if pm.consecutive_wins > 1:
            streak_str = f"[green]W{pm.consecutive_wins}[/]"
        elif pm.consecutive_losses > 1:
            streak_str = f"[red]L{pm.consecutive_losses}[/]"
        else:
            streak_str = "[dim]—[/]"
        table.add_row("Streak", streak_str)
        table.add_row("Kelly ×", f"[{kelly_color}]{kelly_mult:.2f}x[/]")
        if pm.profit_lock_active:
            table.add_row("Status", "[bold green]PROFIT LOCKED[/]")

        return Panel(table, title="[bold]Portfolio[/]", border_style="blue")

    def _positions_panel(self) -> Panel:
        positions = list(self._portfolio.open_positions.values())
        table = Table(
            "ID", "Market", "Side", "Size", "Entry", "Current", "UPNL",
            show_header=True,
            header_style="bold magenta",
            row_styles=["", "dim"],
            expand=True,
        )
        if not positions:
            table.add_row("—", "[dim]No open positions[/]", "", "", "", "", "")
        else:
            for pos in positions[:8]:  # show max 8
                upnl = pos.unrealised_pnl
                upnl_str = f"[{'green' if upnl >= 0 else 'red'}]{upnl:+.4f}[/]"
                table.add_row(
                    str(pos.trade_id),
                    pos.market_ticker[-20:],  # truncate long tickers
                    f"[cyan]{pos.side}[/]",
                    str(pos.size),
                    f"{pos.entry_price:.4f}",
                    f"{pos.current_price:.4f}",
                    upnl_str,
                )
        return Panel(table, title="[bold]Open Positions[/]", border_style="magenta")

    def _prices_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", width=6)
        table.add_column(justify="right", width=12)
        table.add_column(justify="right", width=8, style="dim")

        for asset in ("BTC", "ETH"):
            state = self._binance.get_state(asset)
            if state and state.last_tick:
                price = state.last_tick.price
                mom = state.momentum()
                mom_str = f"{'▲' if mom and mom > 0 else '▼' if mom and mom < 0 else '—'} {abs(mom):.4%}" if mom is not None else "—"
                color = "green" if mom and mom > 0 else "red" if mom and mom < 0 else "white"
                table.add_row(asset, f"[bold {color}]${price:,.2f}[/]", f"[{color}]{mom_str}[/]")
            else:
                table.add_row(asset, "[dim]connecting...[/]", "")

        # Kalshi active markets count
        n_markets = len(self._kalshi._active_tickers)
        n_quotes = len(self._kalshi.quotes)
        table.add_row("", "", "")
        table.add_row(
            "[dim]Mkts[/]",
            f"[white]{n_quotes}[/][dim]/{n_markets}[/]",
            "[dim]live/total[/]",
        )

        return Panel(table, title="[bold]Prices[/]", border_style="cyan")

    def _trades_panel(self) -> Panel:
        table = Table(
            "ID", "Market", "Asset", "Dur", "Side", "Entry", "Exit", "P&L", "Conf", "Edge", "Time",
            show_header=True,
            header_style="bold blue",
            row_styles=["", "dim"],
            expand=True,
        )
        trades = self._recent_trades[:10]
        if not trades:
            table.add_row("—", "[dim]No trades yet[/]", *[""] * 9)
        else:
            for t in trades:
                pnl = t.get("pnl")
                pnl_str = f"[{'green' if pnl and pnl >= 0 else 'red'}]{pnl:+.4f}[/]" if pnl is not None else "[dim]open[/]"
                status_color = {"OPEN": "yellow", "CLOSED": "green", "EXPIRED": "dim", "CANCELLED": "red"}.get(t.get("status", ""), "white")
                opened = t.get("opened_at", "")[:19] if t.get("opened_at") else ""
                table.add_row(
                    str(t.get("id", "")),
                    (t.get("market_ticker") or "")[-20:],
                    t.get("asset", ""),
                    t.get("contract_type", "")[:6],
                    f"[cyan]{t.get('side','')[:3]}[/]",
                    f"{t.get('entry_price', 0):.4f}",
                    f"{t.get('exit_price', 0):.4f}" if t.get("exit_price") else "[dim]—[/]",
                    pnl_str,
                    f"{t.get('confidence', 0):.0%}",
                    f"{t.get('edge_pct', 0):.1%}",
                    opened,
                )
        return Panel(table, title="[bold]Last 10 Trades[/]", border_style="green")

    def _footer(self) -> Text:
        t = Text(justify="center", style="dim")
        t.append("  q[/]uit  ", style="bold white")
        t.append("  [bold]k[/]ill switch  ", style="bold white")
        t.append("  [bold]r[/]efresh markets  ", style="bold white")
        return t
