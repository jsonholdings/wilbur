# Wilbur

A local coding agent for the terminal — tools, subagents, a plan it keeps —
driven entirely by models on your own GPU.

This is the public home for **Wilbur**: the `wilbur` CLI and its VS Code
extension. Nothing it does sends your code or prompts to a cloud model —
everything runs against [Ollama](https://ollama.com) on your own machine.

## Install

CLI:

```sh
pipx install git+https://github.com/jsonholdings/wilbur.git
# or
pip install git+https://github.com/jsonholdings/wilbur.git
```

VS Code extension: install from the Marketplace (search "Wilbur") or Open
VSX, or package it yourself: `cd vscode && npx @vscode/vsce package`. See
[`vscode/`](vscode/).

## Requirements

- [Ollama](https://ollama.com) running locally, with a tool-capable model
  pulled.
- Python >= 3.10 for the CLI.

## First run

On first run, if the model in your config isn't installed, `wilbur` lists
the tool-capable models you already have installed with a VRAM fit mark for
your GPU, and lets you pick one. If nothing tool-capable is installed yet,
it suggests an `ollama pull` sized to your free VRAM instead.

## Other docs

- [`CHANGELOG.md`](CHANGELOG.md) — release history.
- [`BRAND.md`](BRAND.md) — name, tagline, colours, glyph.
- [`LICENSE`](LICENSE) — Apache-2.0.

## Status

The CLI (`wilbur/`, `tests/`) and the VS Code extension (`vscode/`) are both
public here, Apache-2.0.
