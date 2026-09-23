# How to run the project

## Run now on your Mac

In Finder, open this project folder and double-click **Run Paper Demo.command**.
It opens a Terminal window, runs the offline demonstration, shows a readable
summary, and saves a new folder under `reports/`. Press Enter when finished.

Alternatively, open Terminal in the project folder and run:

```sh
PYTHONPATH=src python3 -m xau_trader run-paper-demo
```

Requirements: Python 3.9 or newer. No extra packages, MT5 login, password,
subscription, or deposit are required. The command works without opening MT5.

Each run writes `summary.txt`, `report.json`, and `scenario.json` in its own new
directory. Previous runs are not overwritten. The report can be reproduced from
the saved scenario with `simulate-paper`; see [the simulator guide](PAPER_EXECUTION.md).

**This is a finite synthetic test, not an always-running trading bot.** It opens
and closes scripted hypothetical trades, then exits. The displayed balance is
not your Exness balance, and the result says nothing about strategy profitability.
This launcher does not run our supply/demand strategy. A separate
[strategy paper replay command](STRATEGY_PAPER.md) now connects provisional strategy
signals to a coherent synthetic quote tape for engineering tests. It is not a
real-market backtest or a connected trading bot.

## Recommended path to demo-account operation

For this Python codebase, our engineering recommendation is:

1. **Now:** develop and test the offline code on your existing Mac.
2. **After the strategy/execution connection is implemented:** use a Windows
   computer running both Python and desktop MT5, signed into the intended demo
   account. First verify read-only account/symbol data, then test guarded demo
   orders and reconciliation. No such connector exists in this repository yet.
3. **After supervised demo testing:** consider a full Windows VPS—an always-on
   cloud computer—running the same two programs. This avoids depending on a
   sleeping laptop. Do not buy hosting merely to run the current simulator.

The Windows recommendation follows MetaQuotes' supported distribution: the
current official Python package publishes Windows x86-64 wheels, with no macOS
wheel or source distribution. Installing the Mac MT5 application does not make
that package natively usable by this project's Mac Python interpreter.
[Official package files](https://pypi.org/project/metatrader5/),
[MetaQuotes Python installation guide](https://www.mql5.com/en/book/advanced/python/python_install).

A custom MQL5 Expert Advisor or a separately validated bridge could offer other
deployment routes, but neither has been built here. This project is Python code,
not an `.ex5` robot that can already be dragged onto an MT5 chart.

Use MT5 on your phone to monitor the same demo account after an actual connector
exists; the phone/browser does not host the trading robot. Exness documents that
Expert Advisors require desktop MT4/MT5, not the mobile or web terminal.
[Exness platform guidance](https://get.exness.help/hc/en-us/articles/360019530859-Using-Expert-Advisors-EA).

Do not confuse a full Windows VPS with MT5's built-in virtual hosting migration.
That service migrates supported terminal environments and EAs; scripts are not
transferred. It is not the deployment route selected for our standalone Python
program. [MetaQuotes migration limits](https://www.metatrader5.com/en/terminal/help/virtual_hosting/virtual_hosting_migration).

## What is still required before demo orders

- Validate the new synthetic strategy-to-fill adapter against real data and
  real receipt timing. Its provisional contract requires an exact source-boundary
  quote received strictly after the signal; equal-time ambiguity and missing
  boundary prices reject the attempt. This is not a live-execution guarantee.
- Validate real quote data, sessions, symbol specifications, costs, and account
  sizing; synthetic fixture values are not Exness specifications.
- Validate the configured [offline loss controls](LOSS_LIMITS.md) against broker
  requirements and implement a demo-only connector that checks
  the account, reconciles order state, prevents duplicates, survives restarts,
  and has a tested emergency stop.

Logging into MT5 connects the terminal, not this repository. Leave automated
trading disabled while running these offline commands. Do not put trading
passwords into this project or send them through chat.
