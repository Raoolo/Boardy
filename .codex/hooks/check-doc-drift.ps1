# Boardy — Stop hook: doc-drift safety net.
#
# Fires when Claude Code is about to end a turn. If architectural files
# were modified in the working tree but none of the doc files (CLAUDE.md /
# TODO.md / LEARNINGS.md / README.md) were touched, prints a warning to
# stderr — visible in the transcript. Non-blocking (exit 0); pair this
# with the `feedback-update-docs-on-architectural-changes` auto-memory
# which is the primary mechanism. This hook is the suspenders.
#
# False-positive policy: cosmetic / internal-only changes to arch files
# will still trigger the warning. That's intentional — the model is
# instructed to either update the docs OR justify explicitly why no doc
# update is needed in the final reply. Better one extra sentence than a
# silent drift.

$ErrorActionPreference = 'SilentlyContinue'

# Files whose changes typically warrant a doc update.
$arch = @(
  'app/main.py', 'app/chat.py', 'app/tools.py', 'app/llm.py', 'app/schema.py',
  'app/db.py', 'app/conversations.py', 'app/audit.py',
  'app/games_semantic.py', 'app/rulebooks.py',
  'pyproject.toml', 'uv.lock',
  'Dockerfile', 'docker-compose.yml', '.env.example'
)
$docs = @('CLAUDE.md', 'TODO.md', 'LEARNINGS.md', 'README.md')

# Combine working tree + staged changes — we want to catch the change
# whether or not the agent has staged it yet.
$diffWt = git diff --name-only HEAD 2>$null
$diffStaged = git diff --cached --name-only 2>$null
$changedAll = @($diffWt, $diffStaged) | ForEach-Object { $_ -split "`n" } | Where-Object { $_ } | Sort-Object -Unique

$archChanged = $changedAll | Where-Object { $arch -contains $_ }
if (-not $archChanged) { exit 0 }

$docsChanged = $changedAll | Where-Object { $docs -contains $_ }
if ($docsChanged) { exit 0 }

[Console]::Error.WriteLine("")
[Console]::Error.WriteLine("=== [doc-drift] ====================================================")
[Console]::Error.WriteLine("Modificati: $($archChanged -join ', ')")
[Console]::Error.WriteLine("Nessun cambio in: CLAUDE.md / TODO.md / LEARNINGS.md / README.md")
[Console]::Error.WriteLine("Verifica se serve un aggiornamento doc (oppure documenta perche' no).")
[Console]::Error.WriteLine("=====================================================================")
exit 0
