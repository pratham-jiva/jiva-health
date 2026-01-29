# Jiva Health

AI Health Assistant - Intelligent medical consultation powered by Jiva.

**Backend**: Claude Code CLI (no API key needed).

**Disclaimer**: This is for informational purposes only, NOT medical diagnosis.

## Architecture

```
User (Telegram) --> Telegram Bot --> Orchestrator --> Claude Code CLI --> Report
                                                                     --> Web View
```

Orchestrator calls `claude -p "prompt"` via subprocess for each agent step.
No Anthropic API key required - uses the Claude Code subscription directly.

### Agents
| Agent | Role |
|-------|------|
| Consultant | Patient intake & handoff |
| Research | Medical literature search |
| Evaluator | Status assessment & ABCEF treatment eval |
| Causes | Causal chain analysis |
| Solutions | Treatment options & recommendations |

### 8-Step Workflow
0. Emergency Check (keyword scan - no AI needed)
1. Intake (extract patient profile)
2. Research (medical literature)
3. Status Assessment
4. Cause Analysis
4.5. Treatment Evaluation (ABCEF)
5. Solutions
6. Synthesis (bilingual report)
7. Handoff (summary + teach-back)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with Telegram bot token
# Ensure 'claude' CLI is installed and in PATH
```

## Run

```bash
# Telegram bot
python telegram_bot.py

# Web report viewer
python web.py
```

## Hard Boundaries

- NEVER diagnose specific conditions
- NEVER prescribe medications
- NEVER advise emergency treatment
- NEVER advise skipping doctor visits
- ALWAYS include disclaimers
- ALWAYS recommend consulting a doctor
- Emergency keywords -> immediate redirect to 115/911
