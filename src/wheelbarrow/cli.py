from typing import Annotated

import typer

app = typer.Typer(rich_markup_mode="markdown")


@app.command(
    name="wheelbarrow",
    help="Convert OS binaries into PyPI-installable python wheels",
)
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show verbose output")
    ] = False,
) -> None:

    return


if __name__ == "__main__":
    app()
