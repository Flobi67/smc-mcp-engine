# smc-mcp-engine

Moteur déterministe de Smart Money Concepts (SMC) et de microstructure de marché
(FVG, Order Blocks, BOS/CHoCH, Liquidity Pools), exposé via un serveur MCP
(Model Context Protocol) natif.

Le dépôt ne contient pas encore de code : pas de langage, de dépendances, de
tests ni de linter choisis. Mettre à jour ce fichier dès que ces choix sont faits.

## Préférences

- Répondre en français.
- « Déterministe » : mêmes données d'entrée → mêmes résultats. Pas d'aléatoire,
  pas de dépendance à l'heure courante ou à l'ordre d'itération non garanti.

## Environnement Claude Code (sessions cloud)

- Le conteneur est éphémère : tout ce qui n'est pas commité et poussé est perdu.
  Ce qui doit persister va dans le dépôt (ce fichier, `.claude/`).
- `.claude/hooks/session-start.sh` (déclaré dans `.claude/settings.json`) installe
  [Agent Reach](https://github.com/Panniantong/Agent-Reach) au démarrage de chaque
  session cloud. Ne jamais l'installer depuis PyPI : le paquet `agent-reach` de
  PyPI est un autre projet.
- Skills du projet dans `.claude/skills/`, installés avec `npx skills add` et
  suivis dans `skills-lock.json`. Pour en ajouter ou retirer :
  `npx skills add <owner/repo> -a claude-code -y`, `npx skills remove <nom>`.
  Lire un skill tiers avant de le commiter.
- `/plugin` n'est pas disponible en session cloud, et les outils installés au
  niveau utilisateur (`~/.claude`, ex. claude-mem) ne survivent pas à la session.

## Git

- Le dépôt GitHub est `Flobi67/smc-mcp-engine`, branche par défaut `main`.
- Pas de force-push : intégrer `main` dans une branche par un commit de merge.
