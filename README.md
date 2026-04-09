# AutoPlanner

AutoPlanner is a simple tool designed to orchestrate AI agents collaborating to write technical design documents and implementation plans.

## Overview

The tool provides an autonomous loop where:
1. **Claude** acts as the author, drafting and revising Markdown documents based on the task description.
2. **Codex** acts as the senior reviewer, analyzing the author's work for completeness, clarity, consistencies, feasibility, and missing edge cases.
3. The orchestration loops until the reviewer approves the document (with "LGTM") or the maximum number of iterations is reached.

## Features

- Multi-agent collaboration (Claude for writing, Codex for reviewing)
- Real-time steering: You can type instructions into the terminal while agents are working, and those instructions will be appended to the next prompt as "steering" context.

## Usage

Run the program with `python -m autoplanner.main` or `python autoplanner/main.py`.

## Prompts

Prompts for the agents are extracted into the `autoplanner/prompts/` directory for ease of modification. Adjust them to tweak how the author drafts/revises or how the reviewer evaluates the document.
