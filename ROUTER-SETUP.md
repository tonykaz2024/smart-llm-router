# Smart LLM Router v2.0
**Location**: `F:\llm-router`
**Creat**: 2026-03-31

## Ce face
Ruteaza automat request-urile catre cel mai ieftin model capabil.
Combina 2 backend-uri existente pe masina ta:

| Tier | Model | Backend | Cost |
|------|-------|---------|------|
| T0 FREE | Gemini 2.5 Flash | Vertex AI (DOT PC 192.168.10.38:4000) | $0 ($300 GCP credits) |
| T1 CHEAP | Claude Haiku 4.5 / GPT-5.4-mini | CCS CLIProxy (localhost:8317) | $ (subscription) |
| T2 MEDIUM | Claude Sonnet 4.6 | CCS CLIProxy | $$ (subscription) |
| T3 PREMIUM | Claude Opus 4.6 | CCS CLIProxy | $$$ (subscription) |

## Arhitectura
```
Client -> Smart Router (:4001) --> CCS CLIProxy (:8317) --> Claude/GPT (subscription)
                                \-> DOT PC LiteLLM (192.168.10.38:4000) --> Gemini (Vertex AI FREE)
```

## Pornire
```bash
# 1. Verifica CCS proxy
ccs cliproxy status

# 2. Porneste routerul
python F:\llm-router\smart-router.py --port 4001

# 3. Testeaza
python F:\llm-router\test-real-tasks.py
```

## Rezultate testate (2026-03-31)
- **14/14 task-uri reale** ruteate corect (freight-bol, telegram-claude, feishin-crazy)
- **35% din request-uri** merg pe Gemini = **ZERO cost quota Claude**
- **72% mai putin Opus** -- Opus doar pentru arhitectura/security/research
- Latenta medie: **3.0s/task**, Gemini FREE: **1.0s** (de 4x mai rapid ca Opus)

### Distributia reala:
```
FREE    :  5 (35%) #################   $0 (Vertex credits)
MEDIUM  :  5 (35%) #################   $$ (Sonnet subscription)
PREMIUM :  4 (28%) ##############      $$$ (Opus subscription)
```

## Aliasuri model
| In request | Ce se intampla |
|------------|----------------|
| `model=auto` | Detecteaza complexitatea automat |
| `model=free` | Forteaza Gemini 2.5 Flash (FREE) |
| `model=cheap` / `haiku` | Forteaza Haiku ($) |
| `model=medium` / `sonnet` | Forteaza Sonnet ($$) |
| `model=expensive` / `opus` | Forteaza Opus ($$$) |
| `model=claude-sonnet-4-6` | Passthrough direct |

## Logica routing
- **FREE** (Gemini): explain, rename, translate, format, what is, < 15 cuvinte
- **MEDIUM** (Sonnet): debug, refactor, implement, write tests, fix bug, write function
- **PREMIUM** (Opus): architect, design system, security audit, research, trade-off, comprehensive

## Stats & Monitoring
```bash
curl http://localhost:4001/stats    # JSON cu statistici
curl http://localhost:4001/health   # Health check
```

## Fisiere
```
F:\llm-router\
  smart-router.py          <- routerul principal
  test-real-tasks.py       <- 14 teste pe proiectele tale reale
  start-router.bat         <- script de pornire Windows
  ROUTER-SETUP.md          <- acest fisier
  router-stats.json        <- statistici persistente (auto-generat)
```
