import typer
from rich.console import Console

from opsfleet_agent.core.config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def doctor() -> None:
    settings = get_settings()
    console.print(
        f"env={settings.env} project={settings.gcp_project} profile={settings.model_profile}"
    )


@app.command()
def version() -> None:
    console.print("opsfleet-agent 0.1.0")
