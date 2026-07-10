"""Fichier volontairement neutre.

Anciennement, ce fichier restaurait l'ancien etat_pression.json lorsque la
source live échouait. Cela pouvait masquer les changements de generate_state.py
et conserver une vieille erreur BrowserFallback dans le JSON publié.

On le garde vide/neutre pour éviter toute interception automatique.
"""
