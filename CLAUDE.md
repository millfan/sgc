# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This project builds a personal stock trading assistant. It has two phases:

1. **Learn** from two books and their companion code:
   - *Machine Learning for Algorithmic Trading* (Third Edition), abbreviated `ml4t`
   - *Build AI Agents for Finance*, abbreviated `baa4f`
2. **Build** a multi-agent pipeline that turns market data into a stock portfolio with weights.

## Scope: stocks only

The assistant deals only with individual stocks. Do not add support for options, futures, bonds, crypto, FX, funds, or other instruments, and do not branch into other finance areas such as personal budgeting, tax, or credit. When adapting book examples that cover other asset classes, keep only the parts that apply to stocks.

## Planned architecture

The pipeline is a sequence of stages, and **each stage is its own agent** with a single responsibility. A stage takes the previous stage's output as input. The stages planned so far:

1. **Market regime assessment**: classify the current market state, for example trend, volatility, or risk-on versus risk-off.
2. **Stock and weight proposal**: given the regime, propose candidate stocks and initial portfolio weights.
3. **Adversarial debate**: agents argue for and against the proposal to expose weak picks and hidden risks.
4. **Weight adjustment**: revise the weights based on the debate's outcome.

More stages may follow ("etc." in the original brief). Keep each agent separate and give stages explicit, typed inputs and outputs, so that a stage can be tested, replaced, or rerun on its own.

## Project status

There is no source code yet: the only tracked files are this one and `.gitignore`. No language, dependency manager, build, lint, or test commands have been chosen. Once they exist, update this file with the commands (including how to run a single test) and the actual module layout.

## Reference material is local only

`.gitignore` excludes `ml4t/`, `ml4t-code/`, `baa4f/`, and `baa4f-code/`. These directories hold the books and their companion code on the developer's machine. They are never committed, so they won't be in a fresh clone. When they are present, use them as reference, but write the project's own code outside them and never copy them into tracked paths.
