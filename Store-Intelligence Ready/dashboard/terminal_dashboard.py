"""
dashboard/terminal_dashboard.py — Live terminal dashboard using rich.live.Live

Polls the Store Intelligence API and displays real-time metrics in the terminal.
Handles API unavailability gracefully — never crashes.

Run with:
    python dashboard/terminal_dashboard.py
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

try:
    import httpx
    from rich import box
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Install rich and httpx: pip install rich httpx")
    exit(1)

BASE_URL = "http://localhost:8000"
STORE_ID = "STORE_BLR_002"
METRICS_POLL_SECONDS = 3
ANOMALIES_POLL_SECONDS = 10
HEALTH_POLL_SECONDS = 15

console = Console()


class DashboardState:
    def __init__(self):
        self.metrics = None
        self.anomalies = []
        self.health = None
        self.api_online = False
        self.last_metrics_update = None
        self.last_anomalies_poll = 0.0
        self.last_health_poll = 0.0
        self.error_message = ""


def fetch_json(url: str) -> Optional[dict]:
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


def build_layout(state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="metrics", ratio=2),
        Layout(name="anomalies", ratio=1),
    )

    # --- Header ---
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if state.api_online:
        header_text = Text(
            f"  🏪 Apex Retail — Store Intelligence Dashboard  ·  {STORE_ID}  ·  {now_str}",
            style="bold white on dark_blue",
        )
    else:
        header_text = Text(
            f"  ⚠  API OFFLINE — retrying...  ·  {now_str}",
            style="bold white on red",
        )
    layout["header"].update(Panel(header_text, style="dark_blue"))

    # --- Metrics Panel ---
    metrics_table = Table(box=box.ROUNDED, expand=True, show_header=True, header_style="bold cyan")
    metrics_table.add_column("Metric", style="bold")
    metrics_table.add_column("Value", justify="right")

    if state.metrics:
        m = state.metrics
        metrics_table.add_row("👤 Unique Visitors", str(m.get("unique_visitors", "—")))
        conv = m.get("conversion_rate", 0)
        metrics_table.add_row("💰 Conversion Rate", f"{conv * 100:.1f}%")
        metrics_table.add_row("🧾 Queue Depth", str(m.get("queue_depth", "—")))
        aband = m.get("abandonment_rate", 0)
        metrics_table.add_row("🚪 Abandonment Rate", f"{aband * 100:.1f}%")
        metrics_table.add_row("", "")

        dwell = m.get("avg_dwell_per_zone", {})
        if dwell:
            metrics_table.add_row("📍 Zone Dwell (avg ms)", "")
            for zone_id, avg_ms in sorted(dwell.items()):
                metrics_table.add_row(f"  └ {zone_id}", f"{int(avg_ms):,} ms")
    else:
        metrics_table.add_row("Status", "Waiting for data..." if state.api_online else "API Offline")

    layout["metrics"].update(Panel(metrics_table, title="[bold]Live Metrics[/bold]", border_style="cyan"))

    # --- Anomalies Panel ---
    anomaly_table = Table(box=box.SIMPLE, expand=True, show_header=True, header_style="bold magenta")
    anomaly_table.add_column("Severity", width=10)
    anomaly_table.add_column("Type")
    anomaly_table.add_column("Description")

    if state.anomalies:
        for anomaly in state.anomalies:
            severity = anomaly.get("severity", "INFO")
            color = {"INFO": "blue", "WARN": "yellow", "CRITICAL": "red"}.get(severity, "white")
            anomaly_table.add_row(
                Text(severity, style=f"bold {color}"),
                anomaly.get("anomaly_type", ""),
                anomaly.get("description", ""),
            )
    else:
        anomaly_table.add_row(Text("✓", style="green"), "No anomalies", "All systems normal")

    layout["anomalies"].update(Panel(anomaly_table, title="[bold]Active Anomalies[/bold]", border_style="magenta"))

    # --- Footer ---
    if state.health:
        h = state.health
        db_ok = h.get("db_status") == "connected"
        status = h.get("status", "unknown")
        uptime = h.get("uptime_seconds", 0)
        footer_style = "bold green" if status == "healthy" else "bold red"
        status_icon = "🟢" if status == "healthy" else "🔴"
        footer_text = Text(
            f"  {status_icon} API: {status.upper()}  ·  DB: {'✓' if db_ok else '✗'}  ·  "
            f"Uptime: {int(uptime)}s  ·  v{h.get('version', '?')}",
            style=footer_style,
        )
    elif state.api_online:
        footer_text = Text("  Loading health status...", style="dim")
    else:
        footer_text = Text("  🔴 API OFFLINE — dashboard will reconnect automatically", style="bold red")

    layout["footer"].update(Panel(footer_text, style="dim"))
    return layout


def main():
    state = DashboardState()

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            now = time.time()

            # Poll metrics every 3 seconds
            metrics = fetch_json(f"{BASE_URL}/stores/{STORE_ID}/metrics")
            if metrics:
                state.metrics = metrics
                state.api_online = True
                state.last_metrics_update = now
            else:
                state.api_online = False

            # Poll anomalies every 10 seconds
            if now - state.last_anomalies_poll >= ANOMALIES_POLL_SECONDS:
                anomaly_data = fetch_json(f"{BASE_URL}/stores/{STORE_ID}/anomalies")
                if anomaly_data:
                    state.anomalies = anomaly_data.get("anomalies", [])
                state.last_anomalies_poll = now

            # Poll health every 15 seconds
            if now - state.last_health_poll >= HEALTH_POLL_SECONDS:
                health_data = fetch_json(f"{BASE_URL}/health")
                if health_data:
                    state.health = health_data
                state.last_health_poll = now

            live.update(build_layout(state))
            time.sleep(METRICS_POLL_SECONDS)


if __name__ == "__main__":
    main()
